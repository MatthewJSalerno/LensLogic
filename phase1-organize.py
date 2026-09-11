"""
Project: LensLogic (Phase 1: Core Engine)
Description: A backend engine for organizing large photo collections based on spec.

Runtime Arguments:
- --source <path>  (Optional) Path to unorganized source directory (default: "/data/source").
- --dest <path>    (Optional) Path for organized output directory (default: "/data/dest").
- --base <path>    (Optional) Base directory for app artifacts (default: "/data").
                              Creates/uses <base>/db/ for SQLite and <base>/logs/ for logs.
- --live           (Optional) Flag to execute physical Copy-Verify-Delete operations.
                              Defaults to Dry Run mode if omitted (no files moved/deleted).

System & Python Dependencies:
- System Binary:
    - ExifTool (must be installed on host system and available in PATH)
      Linux: sudo apt install exiftool
      macOS: brew install exiftool
      Windows: choco install exiftool
"""

import argparse
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import queue
import time
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor
from typing import Optional, Callable, Any

# --- Configuration & Constants ---
MAX_WORKER_PROCESSES = os.cpu_count() or 4
DB_QUEUE_SIZE = 1000
SHA1_CHUNK_SIZE = 65536
MAX_RETRIES = 3
INITIAL_RETRY_DELAY = 1.0  # Seconds

SUPPORTED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.heic', '.tiff', '.raw', '.dng', '.cr2', '.nef', '.arw', '.raf'}
PARTIAL_SUFFIX = ".organizing.partial"

# --- Dependency Check ---
try:
    import imagehash
    IMAGEHASH_SUPPORTED = True
except ImportError:
    IMAGEHASH_SUPPORTED = False

try:
    from PIL import Image
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    PIL_SUPPORTED = True
except ImportError:
    PIL_SUPPORTED = False

# --- Data Models ---
@dataclass
class ProcessingResult:
    """Container for data gathered by the Producer process."""
    file_path: str
    sha1_hash: str
    phash: str
    metadata: dict
    status: str
    dest_path: str
    collision_group: Optional[int] = None
    is_master: bool = False

# --- Producer-Consumer Queue ---
result_queue = queue.Queue(maxsize=DB_QUEUE_SIZE)

# --- Logging Initialization ---
def configure_logging(log_dir: Path):
    """Configures logging to output to console and base/logs/organizer.log."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "organizer.log"

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] (%(threadName)s) %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, mode="a", encoding="utf-8")
        ]
    )

logger = logging.getLogger("LensLogic")

# --- Resilient Network IO Wrapper ---
def retry_io_operation(action_description: str, func: Callable[..., Any], *args, **kwargs) -> Any:
    """Executes an IO function with exponential backoff to handle transient network share issues."""
    delay = INITIAL_RETRY_DELAY
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return func(*args, **kwargs)
        except (OSError, PermissionError, IOError) as e:
            if attempt == MAX_RETRIES:
                logger.error(f"IO Operation failed after {MAX_RETRIES} attempts [{action_description}]: {e}")
                raise e
            logger.warning(f"Transient IO error during [{action_description}]: {e}. Retrying in {delay:.1f}s (Attempt {attempt}/{MAX_RETRIES})...")
            time.sleep(delay)
            delay *= 2.0

# --- SQLite Connection Helper ---
def get_db_connection(db_path: str) -> sqlite3.Connection:
    """Creates a connection with WAL mode enabled and an extended busy timeout for concurrent safety."""
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn

# --- Database Consumer (Thread) ---
def db_writer_worker(db_path: str):
    """The Consumer: Only this thread interacts with the SQLite database during scanning."""
    conn = get_db_connection(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT UNIQUE,
            dest_path TEXT,
            sha1_hash TEXT,
            phash TEXT,
            collision_group INTEGER,
            is_master BOOLEAN DEFAULT 0,
            status TEXT,
            metadata_json TEXT
        )
    """)
    conn.commit()

    logger.info("Database worker thread started.")
    while True:
        result = result_queue.get()
        if result is None:
            break
        
        cursor.execute("SELECT id FROM photos WHERE sha1_hash = ?", (result.sha1_hash,))
        existing = cursor.fetchone()
        
        status = result.status
        if existing and status != "Failed":
            status = "Duplicate"

        cursor.execute(
            """INSERT INTO photos 
               (source_path, dest_path, sha1_hash, phash, collision_group, is_master, status, metadata_json) 
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                result.file_path, 
                result.dest_path, 
                result.sha1_hash, 
                result.phash, 
                result.collision_group,
                1 if result.is_master else 0,
                status, 
                json.dumps(result.metadata)
            )
        )
        conn.commit()
        result_queue.task_done()
        
    conn.close()
    logger.info("Database worker thread shut down cleanly.")

# --- Startup Recovery & Reconciliation ---
def reconcile_interrupted_state(db_path: Path):
    """Scans for leftover partials or uncommitted state from crashes prior to run."""
    if not db_path.exists():
        return

    logger.info("Checking database for interrupted tasks from previous runs...")
    conn = get_db_connection(str(db_path))
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='photos';")
        if not cursor.fetchone():
            conn.close()
            return

        cursor.execute("SELECT id, source_path, dest_path FROM photos WHERE status = 'Processing'")
        stuck_records = cursor.fetchall()

        for record_id, src_str, dst_str in stuck_records:
            src = Path(src_str)
            dst = Path(dst_str)
            partial = Path(dst_str + PARTIAL_SUFFIX)

            if partial.exists():
                logger.warning(f"Found orphaned partial file: {partial.name}. Removing.")
                partial.unlink()

            if dst.exists() and not src.exists():
                logger.info(f"Reconciled completed move for record {record_id}: {dst.name}")
                cursor.execute("UPDATE photos SET status = 'Completed' WHERE id = ?", (record_id,))
            else:
                logger.info(f"Resetting interrupted record {record_id} to Pending.")
                cursor.execute("UPDATE photos SET status = 'Pending' WHERE id = ?", (record_id,))
            conn.commit()
    except Exception as e:
        logger.error(f"Error during startup state reconciliation: {e}")
    finally:
        conn.close()

# --- Pre-Flight Space Validation ---
def verify_sufficient_disk_space(dest_path: Path, required_bytes: int, safety_margin_mb: int = 500) -> bool:
    """Checks if the destination directory volume has sufficient space available."""
    check_dir = dest_path if dest_path.exists() else dest_path.parent
    check_dir.mkdir(parents=True, exist_ok=True)

    stat = shutil.disk_usage(check_dir)
    buffer_bytes = safety_margin_mb * 1024 * 1024
    total_needed = required_bytes + buffer_bytes

    if stat.free < total_needed:
        required_gb = required_bytes / (1024**3)
        free_gb = stat.free / (1024**3)
        logger.error(
            f"Insufficient disk space on destination! "
            f"Required: {required_gb:.2f} GB (+{safety_margin_mb}MB safety buffer), "
            f"Available: {free_gb:.2f} GB."
        )
        return False
    return True

# --- Helper Functions ---
def parse_exif_date(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime(str(value)[:19], '%Y:%m:%d %H:%M:%S')
    except (ValueError, TypeError):
        return None

def get_date_from_exiftool(file_path: Path) -> Optional[datetime]:
    try:
        cmd = ['exiftool', '-s3', '-DateTimeOriginal', '-CreateDate', '-DateTime', str(file_path)]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
        if res.returncode == 0 and res.stdout.strip():
            raw_date_str = res.stdout.splitlines()[0].strip()
            parsed = parse_exif_date(raw_date_str)
            if parsed:
                return parsed
    except Exception as e:
        logger.debug(f"ExifTool execution failed for {file_path.name}: {e}")
    return None

def get_date_from_exif(file_path: Path) -> Optional[datetime]:
    dt = get_date_from_exiftool(file_path)
    if dt:
        return dt

    if PIL_SUPPORTED:
        try:
            with Image.open(file_path) as img:
                exif_data = img.getexif()
                if exif_data:
                    for tag in (36867, 36868, 306):
                        if tag in exif_data:
                            parsed = parse_exif_date(exif_data[tag])
                            if parsed: return parsed
        except Exception as e:
            logger.debug(f"PIL EXIF read failed on {file_path.name}: {e}")
            
    return None

def compute_sha1(file_path: str) -> str:
    def _hash():
        h = hashlib.sha1()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(SHA1_CHUNK_SIZE), b''):
                h.update(chunk)
        return h.hexdigest()
    return retry_io_operation(f"SHA1 Hash {file_path}", _hash)

def compute_phash(file_path: str) -> str:
    if not (IMAGEHASH_SUPPORTED and PIL_SUPPORTED):
        return "not_supported"
    try:
        with Image.open(file_path) as img:
            return str(imagehash.phash(img))
    except Exception:
        return "error"

def get_unique_dest_path(target_path: Path) -> Path:
    if not target_path.exists():
        return target_path
    counter = 1
    while target_path.exists():
        target_path = target_path.parent / f"{target_path.stem}_{counter}{target_path.suffix}"
        counter += 1
    return target_path

# --- Copy-Verify-Delete Core Protocol ---
def copy_verify_delete(source_str: str, dest_str: str) -> bool:
    source = Path(source_str)
    dest = Path(dest_str)
    partial_dest = Path(dest_str + PARTIAL_SUFFIX)

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)

        retry_io_operation(f"Copying {source.name}", shutil.copy2, source, partial_dest)

        src_sha1 = compute_sha1(str(source))
        partial_sha1 = compute_sha1(str(partial_dest))

        if src_sha1 != partial_sha1:
            logger.error(f"SHA1 mismatch during verification for {source.name}. Aborting move.")
            if partial_dest.exists():
                partial_dest.unlink()
            return False

        retry_io_operation(f"Rename partial {partial_dest.name}", partial_dest.rename, dest)
        retry_io_operation(f"Delete original {source.name}", source.unlink)
        logger.info(f"Successfully migrated: {source.name} -> {dest}")
        return True

    except Exception as e:
        logger.error(f"Failed transactional move for {source_str}: {e}")
        if partial_dest.exists():
            try:
                partial_dest.unlink()
            except Exception:
                pass
        return False

# --- Processing Worker ---
def process_file_task(file_path_str: str, dest_base_path: str) -> ProcessingResult:
    file_path = Path(file_path_str)

    sha1 = compute_sha1(str(file_path))
    phash = compute_phash(str(file_path))

    dt = get_date_from_exif(file_path)
    if not dt:
        dt = datetime.fromtimestamp(os.path.getmtime(file_path))

    year_dir = dt.strftime("%Y")
    month_dir = dt.strftime("%m")
    target_folder = Path(dest_base_path) / year_dir / month_dir
    
    initial_dest = target_folder / file_path.name
    final_dest = get_unique_dest_path(initial_dest)

    return ProcessingResult(
        file_path=str(file_path),
        sha1_hash=sha1,
        phash=phash,
        metadata={"date_taken": dt.isoformat()},
        status="Pending",
        dest_path=str(final_dest)
    )

# --- Main Execution ---
def main():
    parser = argparse.ArgumentParser(description="LensLogic - Photo Collection Organizer (Phase 1 Engine)")
    parser.add_argument("--source", default="/data/source", help="Path to source directory (default: /data/source).")
    parser.add_argument("--dest", default="/data/dest", help="Path to destination directory (default: /data/dest).")
    parser.add_argument("--base", default="/appdata", help="Base directory for DB and logs (default: /appdata).")
    parser.add_argument("--live", action="store_true", help="Execute physical migration (Copy-Verify-Delete). Default is Dry Run.")
    args = parser.parse_args()

    # 1. Resolve Base Path & Setup Subdirectories
    base_dir = Path(args.base).resolve()
    db_dir = base_dir / "db"
    log_dir = base_dir / "logs"

    db_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    db_path = db_dir / "photo_hashes.db"

    # 2. Configure Logging
    configure_logging(log_dir)

    source_path = Path(args.source).resolve()
    dest_path = Path(args.dest).resolve()

    if not source_path.exists():
        logger.error(f"Source path does not exist: {source_path}")
        return

    logger.info(f"Initializing LensLogic Engine. Base Directory: {base_dir}")
    logger.info(f"Source Directory: {source_path}")
    logger.info(f"Destination Directory: {dest_path}")
    logger.info(f"Database Path: {db_path}")

    # 3. Startup Recovery Protocol
    reconcile_interrupted_state(db_path)

    # 4. Start DB Writer Thread
    db_thread = threading.Thread(target=db_writer_worker, args=(str(db_path),), daemon=True)
    db_thread.start()

    files_to_process = [
        str(p) for p in source_path.rglob('*') 
        if p.is_file() and not p.is_symlink() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    ]
    logger.info(f"Discovered {len(files_to_process)} supported photo/image files to scan.")

    with ProcessPoolExecutor(max_workers=MAX_WORKER_PROCESSES) as executor:
        futures = [executor.submit(process_file_task, f, str(dest_path)) for f in files_to_process]
        for future in futures:
            res = future.result()
            result_queue.put(res)

    result_queue.join()
    result_queue.put(None)
    db_thread.join()

    logger.info("Scan and Dry-Run indexing completed successfully. Database updated.")

    if args.live:
        logger.info("Live Mode enabled. Initiating Pre-flight Space Checks...")
        conn = get_db_connection(str(db_path))
        cursor = conn.cursor()
        
        cursor.execute("SELECT id, source_path, dest_path FROM photos WHERE status = 'Pending'")
        pending_records = cursor.fetchall()

        total_bytes_needed = sum(
            Path(src[1]).stat().st_size for src in pending_records if Path(src[1]).exists()
        )

        if not verify_sufficient_disk_space(dest_path, total_bytes_needed):
            logger.error("Aborting migration due to insufficient space on destination drive.")
            conn.close()
            return

        logger.info(f"Disk space verified. Moving {len(pending_records)} items ({total_bytes_needed / (1024**2):.2f} MB)...")

        for record_id, src, dst in pending_records:
            cursor.execute("UPDATE photos SET status = 'Processing' WHERE id = ?", (record_id,))
            conn.commit()

            success = copy_verify_delete(src, dst)
            final_status = "Completed" if success else "Failed"

            cursor.execute("UPDATE photos SET status = ? WHERE id = ?", (final_status, record_id))
            conn.commit()

        conn.close()
        logger.info("All live file migration operations finished.")
    else:
        logger.info("Dry Run finished. Pass the `--live` flag to commit and move files.")

if __name__ == "__main__":
    main()
