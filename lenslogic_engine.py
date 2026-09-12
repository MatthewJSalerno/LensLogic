"""
Project: LensLogic (Phase 1: Core Engine)
Description: A backend engine for organizing large photo collections based on spec.

Runtime Arguments:
- --source <path> (Optional) Path to unorganized source directory (default: "/data/source").
- --dest <path> (Optional) Path for organized output directory (default: "/data/dest").
- --base <path> (Optional) Base directory for app artifacts (default: "/data").
  Creates/uses <base>/db/ for SQLite and <base>/logs/ for logs.
- --workers <N> (Optional) Override the worker process count used for
  hashing/date resolution (default: os.cpu_count()).
- --exts <.ext1,.ext2,...> (Optional) Comma-separated extension list,
  replacing the built-in default set for directory scanning. Has no effect
  on --file-ids targeting, since that bypasses directory scanning entirely.
- --file-ids <id1,id2,...> (Optional) Comma-separated database row IDs to
  target, bypassing the directory scan and processing exactly these
  already-cataloged files. A file must have gone through at least one prior
  Index for its ID to exist. This is what powers selection-scoped
  operations from the web UI (Phase 2) — e.g. "Move just these 3 photos" —
  but works identically from the CLI.

Mode flags (mutually exclusive — pick at most one; omitting both runs the
default Index):
- --move: Execute physical migration (Copy-Verify-Delete). Source files are
  moved: deleted after a verified copy lands at the destination. Confirmed
  exact duplicates are also removed from source once a verified copy of
  their content exists elsewhere at the destination.
- --copy: Non-destructive. Same verified Copy-Verify step as --move, but the
  source file is never deleted or modified afterward. Duplicate source files
  are also left untouched in this mode — nothing is ever removed from source.
- (neither of the above): Index — full scan, hashing, and destination-path
  resolution, exactly like --move/--copy would compute, but no physical
  action is taken. This is the safe default described in spec §4.1.

Cancellation: sending SIGTERM or SIGINT (e.g. `docker stop`, or Ctrl+C)
during a --move/--copy run lets the file currently being copy-verified
finish, then stops before starting the next one. Every file that didn't get
a chance to run is written to the operations log with status='Cancelled' —
current, un-started work stays 'Pending' in the photos table (so a plain
re-run naturally picks it back up), while the operations log keeps a full
historical record of exactly what happened during that specific run,
including which items never got reached. Duplicate-source cleanup is
skipped entirely for a cancelled run, since it depends on every Pending
item's fate being fully known first.

Retries: there is no dedicated retry mechanism. If some files fail (or a
run is cancelled), just re-run the same command — files already moved are
gone from --source and won't be reprocessed; only what's still there (still
'Pending', or previously 'Failed' and re-flagged 'Pending' by the next
Index) gets touched again. This is fast because nothing already-successful
needs to be redone.

No result is ever silently lost across crashes either: a run interrupted by
something uncatchable (SIGKILL, OOM-kill, power loss) leaves its `runs` row
at status='Running' with no end time — the next invocation's startup
reconciliation detects this and marks it 'Crashed' with a real end
timestamp, rather than leaving a phantom "still running" entry forever.

System & Python Dependencies:
- System Binary (HARD REQUIREMENT — the engine refuses to start without
  both this binary and the PyExifTool Python package; see Metadata
  Extraction below for exactly what ExifTool is used for and why Pillow/
  rawpy/imagehash are still required alongside it, not replaced by it):
  - ExifTool
    Linux: sudo apt install libimage-exiftool-perl
    macOS: brew install exiftool
    Windows: choco install exiftool
  - Python package: pip install pyexiftool

Metadata Extraction:
    ExifTool is used via a PERSISTENT process per worker (PyExifTool's
    `-stay_open` mode, one instance per ProcessPoolExecutor worker,
    started once via `_init_worker_process` and reused for every file that
    worker handles) rather than spawning a fresh `exiftool` subprocess per
    file. Measured directly: a repeat query against an already-running
    instance took ~2.6ms vs ~24ms+ paying process-spawn overhead — roughly
    a 10x difference that compounds significantly across a large library.

    For every file, the engine captures BOTH "date taken" (used to compute
    the destination folder) AND the full metadata set available (camera
    make/model, ISO, aperture, shutter speed, and whatever else the source
    exposes) from the same underlying capture. Fallback order, falling
    through only if the previous step fails or finds nothing at all:

    1. ExifTool (persistent per-worker process, full tag set, not a
       curated subset) — the primary and now-guaranteed-available source.
       The only method that can read metadata from RAW-family files
       (.cr2, .nef, .arw, .raf, .raw, .dng), since neither PIL nor rawpy
       expose EXIF/metadata fields for those formats (rawpy only decodes
       pixel data, for pHash generation — it has no metadata-reading API
       at all).
    2. PIL (Image.getexif(), PLUS the "Exif" sub-IFD via get_ifd(0x8769))
       — a defensive per-FILE fallback, not a "ExifTool isn't installed"
       fallback anymore (that case can no longer happen — see above). Used
       only if ExifTool genuinely ran but returned nothing usable for a
       specific file. Works for standard formats (JPEG, PNG, TIFF, HEIC
       with pillow-heif); cannot open RAW-family formats at all.
    3. File modification time — used only if neither of the above
       produces a usable date. No richer metadata is available at this
       fallback level; the stored metadata is just {"date_taken": ...}.

    IMPORTANT — ExifTool being a hard requirement does NOT mean Pillow,
    rawpy, and imagehash became optional or got removed. They do a
    completely different job that ExifTool cannot do at all: ExifTool
    reads embedded metadata tags, it does not decode pixel data. pHash
    generation (compute_phash()) and thumbnail generation both require
    actually opening and decoding the image (PIL for standard/HEIC
    formats, rawpy for RAW-family formats) and feeding real pixel data to
    `imagehash.phash()` — there is no metadata-only substitute for this,
    and ExifTool's ability to extract an already-embedded camera preview
    image (where one exists) doesn't change that, since imagehash still
    needs that extracted preview decoded through PIL to hash it anyway.
"""

import argparse
import fcntl
import hashlib
import json
import logging
import os
import shutil
import signal
import sqlite3
import sys
import threading
import queue
import time
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor
from typing import Optional, Callable, Any, List

# --- Configuration & Constants ---
MAX_WORKER_PROCESSES = os.cpu_count() or 4
DB_QUEUE_SIZE = 1000
SHA1_CHUNK_SIZE = 65536
MAX_RETRIES = 3
INITIAL_RETRY_DELAY = 1.0  # Seconds
SUPPORTED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.heic', '.tiff', '.raw', '.dng', '.cr2', '.nef', '.arw', '.raf'}
PARTIAL_SUFFIX = ".organizing.partial"
LOCK_FILENAME = "engine.lock"

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

# FIX: rawpy added so pHash generation actually works for RAW-family files.
# Previously compute_phash() only ever tried PIL.Image.open() on every file
# — PIL cannot decode real RAW sensor data (.raw/.dng/.cr2/.nef/.arw/.raf)
# at all, so every one of those files silently got the literal string
# "error" stored as its phash, never a usable hash. This matches what
# project-spec.md §3.3 already calls for ("rawpy for robust RAW and DNG
# metadata handling") but which wasn't actually wired in.
try:
    import rawpy
    RAWPY_SUPPORTED = True
except ImportError:
    RAWPY_SUPPORTED = False

RAW_EXTENSIONS = {'.raw', '.dng', '.cr2', '.nef', '.arw', '.raf'}

# --- Dependency Check: ExifTool is a HARD requirement (see module docstring
# for why) — checked here, enforced with a clear fatal error in main(). Two
# things must both be true: the PyExifTool Python package is importable, AND
# the actual `exiftool` system binary is on PATH (the package is just a thin
# wrapper — it does nothing without the real binary installed).
try:
    import exiftool as pyexiftool
    PYEXIFTOOL_PACKAGE_AVAILABLE = True
except ImportError:
    PYEXIFTOOL_PACKAGE_AVAILABLE = False

EXIFTOOL_BINARY_AVAILABLE = shutil.which('exiftool') is not None
EXIFTOOL_SUPPORTED = PYEXIFTOOL_PACKAGE_AVAILABLE and EXIFTOOL_BINARY_AVAILABLE


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
    run_id: int
    has_name_collision: bool = False
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

# --- Cancellation Support ---
# Set by a SIGTERM/SIGINT handler registered in main(). Checked between
# files in the Move/Copy loop — never mid-file — so a cancellation always
# lets the file currently being copy-verified finish cleanly rather than
# risking a partial/corrupt state for that one file.
cancel_requested = threading.Event()


def _handle_cancel_signal(signum, frame):
    logger.warning(f"Received signal {signum} — finishing current file, then cancelling the rest of this run.")
    cancel_requested.set()


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
            logger.warning(
                f"Transient IO error during [{action_description}]: {e}. "
                f"Retrying in {delay:.1f}s (Attempt {attempt}/{MAX_RETRIES})..."
            )
            time.sleep(delay)
            delay *= 2.0


# --- SQLite Connection Helper ---
def get_db_connection(db_path: str) -> sqlite3.Connection:
    """Creates a connection with WAL mode enabled and an extended busy timeout for concurrent safety."""
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


# --- Database Schema Initialization ---
def init_database(db_path: str):
    """
    Creates all three tables if they don't exist yet. Called once, early in
    main(), before any run row is inserted or worker threads start.

    Table roles:
    - photos: CURRENT STATE only, one row per source_path (enforced UNIQUE).
      Continuously overwritten in place by the upsert in db_writer_worker —
      this is what keeps "what's still Pending" queries fast.
    - runs: one row per engine invocation (Index, Move, or Copy), recording
      what was asked for and how it ended (Completed/Cancelled/Failed).
    - operations: the audit LOG. Append-only, one row per file per run, so
      the same file can appear multiple times across different runs/attempts
      without losing history the way overwriting a column on `photos` would.
    """
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
            metadata_json TEXT,
            has_name_collision BOOLEAN DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT NOT NULL,
            source_path TEXT,
            dest_path TEXT,
            file_ids_filter TEXT,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            status TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            photo_id INTEGER,
            original_filename TEXT,
            source_path TEXT,
            dest_path TEXT,
            status TEXT NOT NULL,
            error_message TEXT,
            has_name_collision BOOLEAN DEFAULT 0,
            timestamp TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES runs(id),
            FOREIGN KEY(photo_id) REFERENCES photos(id)
        )
    """)
    conn.commit()
    conn.close()


def start_run(db_path: str, mode: str, source_path: str, dest_path: str, file_ids: Optional[List[int]]) -> int:
    """Inserts the `runs` row for this invocation and returns its id."""
    conn = get_db_connection(db_path)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO runs (mode, source_path, dest_path, file_ids_filter, started_at, status) "
        "VALUES (?, ?, ?, ?, ?, 'Running')",
        (mode, source_path, dest_path, json.dumps(file_ids) if file_ids else None, datetime.now().isoformat())
    )
    conn.commit()
    run_id = cursor.lastrowid
    conn.close()
    return run_id


def finish_run(db_path: str, run_id: int, status: str):
    """Finalizes the `runs` row — called on normal completion, cancellation, or crash."""
    conn = get_db_connection(db_path)
    conn.execute(
        "UPDATE runs SET status = ?, ended_at = ? WHERE id = ?",
        (status, datetime.now().isoformat(), run_id)
    )
    conn.commit()
    conn.close()


def log_operation(conn: sqlite3.Connection, run_id: int, photo_id: Optional[int], source_path: str,
                   dest_path: Optional[str], status: str, error_message: Optional[str] = None,
                   has_name_collision: bool = False):
    """Appends one row to the operations audit log. Never overwrites — every call is new history."""
    conn.execute(
        """INSERT INTO operations
           (run_id, photo_id, original_filename, source_path, dest_path, status, error_message,
            has_name_collision, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run_id, photo_id, Path(source_path).name if source_path else None, source_path, dest_path,
            status, error_message, 1 if has_name_collision else 0, datetime.now().isoformat()
        )
    )
    conn.commit()


# --- Database Consumer (Thread) ---
def db_writer_worker(db_path: str):
    """The Consumer: Only this thread interacts with the SQLite database during scanning."""
    conn = get_db_connection(db_path)
    cursor = conn.cursor()
    logger.info("Database worker thread started.")

    while True:
        result = result_queue.get()
        if result is None:
            result_queue.task_done()
            break

        # FIX: the whole body is now wrapped in try/except/finally. A bad row
        # (or any other unexpected error) used to crash this thread silently;
        # since the main thread's result_queue.join() waits on task_done()
        # being called for every item, a dead consumer thread meant the whole
        # script hung forever with no error surfaced. Now a single bad file
        # is logged and skipped instead of taking down the run.
        try:
            # FIX: exclude the file's own previous row from the duplicate
            # check ("source_path != ?"), AND exclude rows that are
            # themselves already Duplicate/Removed_Duplicate. Without the
            # status exclusion, re-scanning a duplicate pair could cascade:
            # each file would find the OTHER one's persisted 'Duplicate'
            # status and count it as "an existing copy elsewhere", flipping
            # BOTH files to Duplicate with no Pending anchor left for either
            # — meaning that photo could never be moved, and (since cleanup
            # only acts on a verified Completed copy) could never be
            # deleted either. It would sit stuck in source forever. Only a
            # genuine anchor status (Pending/Processing/Completed/Failed)
            # should count as "the original."
            cursor.execute(
                "SELECT id FROM photos WHERE sha1_hash = ? AND source_path != ? "
                "AND status NOT IN ('Duplicate', 'Removed_Duplicate')",
                (result.sha1_hash, result.file_path)
            )
            existing = cursor.fetchone()

            status = result.status
            if existing and status != "Failed":
                status = "Duplicate"

            # FIX: UPSERT on source_path instead of a blind INSERT. This is
            # the direct fix for the crash — re-scanning a file already
            # cataloged from a prior run (e.g. re-running an Index, or the
            # standard Index-then-move sequence) previously violated the
            # UNIQUE constraint on source_path and killed this thread. Now a
            # re-scan just refreshes that row's hashes/status in place.
            cursor.execute(
                """INSERT INTO photos
                   (source_path, dest_path, sha1_hash, phash, collision_group, is_master, status,
                    metadata_json, has_name_collision)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source_path) DO UPDATE SET
                       dest_path = excluded.dest_path,
                       sha1_hash = excluded.sha1_hash,
                       phash = excluded.phash,
                       collision_group = excluded.collision_group,
                       is_master = excluded.is_master,
                       status = excluded.status,
                       metadata_json = excluded.metadata_json,
                       has_name_collision = excluded.has_name_collision
                """,
                (
                    result.file_path,
                    result.dest_path,
                    result.sha1_hash,
                    result.phash,
                    result.collision_group,
                    1 if result.is_master else 0,
                    status,
                    json.dumps(result.metadata),
                    1 if result.has_name_collision else 0
                )
            )
            conn.commit()

            # Audit log entry for this scan result. Looked up by source_path
            # rather than trusting cursor.lastrowid, since that's unreliable
            # across the UPDATE branch of an upsert.
            cursor.execute("SELECT id FROM photos WHERE source_path = ?", (result.file_path,))
            photo_row = cursor.fetchone()
            photo_id = photo_row[0] if photo_row else None
            log_operation(conn, result.run_id, photo_id, result.file_path, result.dest_path, status)
        except Exception as e:
            logger.error(f"DB writer failed to record {result.file_path}: {e}")
        finally:
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

        # FIX: a run that was killed uncatchably (SIGKILL, OOM-kill, power
        # loss — anything that bypasses main()'s try/finally) never reaches
        # finish_run(), so its `runs` row is left at status='Running' with
        # ended_at=NULL forever. Confirmed via testing: a hard-killed process
        # leaves exactly this state, and it is NOT cleaned up by any
        # subsequent run without this step — a run-history view would show
        # a job as perpetually "still running" long after the process that
        # ran it is gone. Every such row found at startup genuinely cannot
        # still be running (this new process holds the same DB), so mark it
        # accordingly.
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='runs';")
        if cursor.fetchone():
            cursor.execute("SELECT id FROM runs WHERE status = 'Running'")
            orphaned_runs = cursor.fetchall()
            for (run_id,) in orphaned_runs:
                logger.warning(f"Run #{run_id} was left 'Running' by an unclean shutdown — marking Crashed.")
                cursor.execute(
                    "UPDATE runs SET status = 'Crashed', ended_at = ? WHERE id = ?",
                    (datetime.now().isoformat(), run_id)
                )

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
        required_gb = required_bytes / (1024 ** 3)
        free_gb = stat.free / (1024 ** 3)
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


# --- Persistent Per-Worker ExifTool Process ---
# ProcessPoolExecutor spawns separate OS processes, so a single shared
# ExifTool instance can't be passed between them. Instead, each worker
# process gets its OWN persistent ExifTool subprocess, started once (via
# ProcessPoolExecutor's `initializer=`) and reused for every file that
# worker handles — this is what actually delivers the performance win:
# avoiding a fresh subprocess spawn per FILE, not per worker. Verified via
# direct timing: a repeat query against an already-running instance took
# ~2.6ms vs ~24ms+ for one paying process-spawn overhead — roughly a 10x
# difference that compounds across a large library.
_worker_exiftool: Optional["pyexiftool.ExifToolHelper"] = None


def _init_worker_process(exiftool_supported: bool):
    """
    ProcessPoolExecutor initializer — runs once when each worker process
    starts, before it's given any files. Sets up this worker's persistent
    ExifTool instance (if ExifTool is available) and registers cleanup so
    the subprocess doesn't outlive its parent worker.
    """
    global _worker_exiftool
    if not exiftool_supported:
        return
    try:
        _worker_exiftool = pyexiftool.ExifToolHelper(
            common_args=[]  # deliberately override PyExifTool's default
                             # ["-G", "-n"] (grouped tag names + raw numeric
                             # values) to match the flat-key, human-readable
                             # output the rest of this module already parses
                             # (extract_date_from_metadata expects a bare
                             # "DateTimeOriginal" key, not "EXIF:DateTimeOriginal"),
                             # and so metadata_json's shape doesn't silently
                             # change for anything already reading it.
        )
    except Exception:
        _worker_exiftool = None
    import atexit
    atexit.register(_shutdown_worker_exiftool)


def _shutdown_worker_exiftool():
    """Terminates this worker's persistent ExifTool subprocess on normal exit."""
    global _worker_exiftool
    if _worker_exiftool is not None:
        try:
            _worker_exiftool.terminate()
        except Exception:
            pass
        _worker_exiftool = None


def get_full_exif_via_exiftool(file_path: Path) -> Optional[dict]:
    """
    Returns the COMPLETE tag set ExifTool can extract for this file, as a
    dict, or None if ExifTool isn't available / fails / returns nothing.
    This is deliberately the full tag set rather than a curated subset,
    storing everything now means Phase 3 (EXIF inspection/editing) doesn't
    need to re-scan the whole library later to get fields nobody thought to
    whitelist today.

    Uses this worker process's persistent ExifTool instance (see
    _init_worker_process) instead of spawning a subprocess per file.
    """
    global _worker_exiftool
    if _worker_exiftool is None:
        return None
    try:
        result = _worker_exiftool.get_metadata([str(file_path)])
        if result and isinstance(result, list):
            return result[0]
    except Exception as e:
        logger.debug(f"ExifTool (persistent) extraction failed for {file_path.name}: {e}")
        # Self-healing: if the persistent subprocess itself died or got into
        # a bad state (e.g. choked on a malformed file), don't silently lose
        # ExifTool capability for every remaining file this worker handles —
        # replace it with a fresh instance and let the NEXT file try again.
        try:
            _worker_exiftool.terminate()
        except Exception:
            pass
        try:
            _worker_exiftool = pyexiftool.ExifToolHelper(common_args=[])
        except Exception:
            _worker_exiftool = None
    return None


def get_exif_via_pil(file_path: Path) -> Optional[dict]:
    """
    Fallback full-metadata capture when ExifTool isn't installed. Converts
    PIL's raw numeric-tag-id EXIF dict into a named-tag dict using PIL's own
    tag name table, so the stored JSON is human-readable either way this
    function is reached. Only works for formats PIL can open (not RAW).

    FIX: img.getexif() alone only returns the top-level "0th" IFD (Make,
    Model, and similar basic tags) — it does NOT automatically expand the
    "Exif" sub-IFD (pointed to by tag 0x8769 / "ExifOffset"), which is where
    DateTimeOriginal, ISO, FNumber, and ExposureTime actually live. Without
    explicitly fetching that sub-IFD via get_ifd(0x8769), date resolution
    and camera-settings capture silently fail on this fallback path even
    when the file genuinely has that data embedded — confirmed via a test
    JPEG with real embedded EXIF: only Make/Model/ExifOffset came through
    before this fix, with DateTimeOriginal/ISO/FNumber/ExposureTime missing
    entirely and the date silently falling back to file mtime instead.
    """
    if not PIL_SUPPORTED:
        return None
    try:
        from PIL.ExifTags import TAGS
        with Image.open(file_path) as img:
            exif_data = img.getexif()
            if not exif_data:
                return None
            result = {TAGS.get(tag, str(tag)): str(value) for tag, value in exif_data.items()}
            try:
                exif_ifd = exif_data.get_ifd(0x8769)  # the "Exif" sub-IFD
                if exif_ifd:
                    result.update({TAGS.get(tag, str(tag)): str(value) for tag, value in exif_ifd.items()})
            except Exception:
                pass
            return result if result else None
    except Exception as e:
        logger.debug(f"PIL EXIF read failed on {file_path.name}: {e}")
        return None


def extract_date_from_metadata(metadata: dict) -> Optional[datetime]:
    """Pulls a usable 'date taken' out of whichever metadata dict was captured."""
    for key in ("DateTimeOriginal", "CreateDate", "DateTime"):
        if key in metadata:
            parsed = parse_exif_date(metadata[key])
            if parsed:
                return parsed
    return None


def get_metadata_and_date(file_path: Path) -> tuple:
    """
    Metadata Extraction Fallback Chain (see module docstring for the full
    rationale): ExifTool -> PIL -> file mtime. Returns (datetime, metadata
    dict) together, since both are now sourced from the same underlying
    capture rather than two separate passes.

    ExifTool is now a hard requirement for the engine to even start (see
    module docstring), so the PIL/mtime steps below are no longer covering
    for "ExifTool isn't installed" — that case can't happen anymore. They
    remain as a defensive per-FILE fallback for the narrower case where
    ExifTool is genuinely running but fails on one specific file (corrupted
    data, an unusual format edge case) — the persistent process itself
    already self-heals from that in get_full_exif_via_exiftool(); this is
    the next layer down if a file just doesn't yield usable metadata at all.
    """
    metadata = get_full_exif_via_exiftool(file_path)
    if metadata:
        dt = extract_date_from_metadata(metadata)
        if dt:
            return dt, metadata
        # ExifTool ran but found no usable date tag — still keep whatever
        # metadata it did find, just fall through for the date itself.
        fallback_dt = datetime.fromtimestamp(os.path.getmtime(file_path))
        return fallback_dt, metadata

    metadata = get_exif_via_pil(file_path)
    if metadata:
        dt = extract_date_from_metadata(metadata)
        if dt:
            return dt, metadata
        fallback_dt = datetime.fromtimestamp(os.path.getmtime(file_path))
        return fallback_dt, metadata

    # Neither source found anything at all.
    fallback_dt = datetime.fromtimestamp(os.path.getmtime(file_path))
    return fallback_dt, {}


def compute_sha1(file_path: str) -> str:
    def _hash():
        h = hashlib.sha1()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(SHA1_CHUNK_SIZE), b''):
                h.update(chunk)
        return h.hexdigest()
    return retry_io_operation(f"SHA1 Hash {file_path}", _hash)


def compute_phash(file_path: str) -> str:
    if not IMAGEHASH_SUPPORTED:
        return "not_supported"

    ext = Path(file_path).suffix.lower()

    # FIX: RAW-family formats need rawpy to decode at all — PIL can't open
    # them. Demosaic at half_size for speed, since a perceptual hash only
    # needs a coarse visual fingerprint, not full resolution.
    if ext in RAW_EXTENSIONS:
        if not (RAWPY_SUPPORTED and PIL_SUPPORTED):
            return "not_supported"
        try:
            with rawpy.imread(file_path) as raw:
                rgb = raw.postprocess(use_camera_wb=True, half_size=True, no_auto_bright=True, output_bps=8)
            img = Image.fromarray(rgb)
            return str(imagehash.phash(img))
        except Exception as e:
            logger.debug(f"rawpy pHash failed for {file_path}: {e}")
            return "error"

    if not PIL_SUPPORTED:
        return "not_supported"
    try:
        with Image.open(file_path) as img:
            return str(imagehash.phash(img))
    except Exception as e:
        logger.debug(f"PIL pHash failed for {file_path}: {e}")
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
def copy_verify_delete(source_str: str, dest_str: str, delete_source: bool = True) -> tuple:
    """
    Copies source to dest via a verified temp-file-then-rename sequence.
    delete_source=True (the --move behavior): source is deleted only
    after the copy is verified byte-for-byte identical — this is the
    Copy-Verify-Delete protocol from spec §4.3.
    delete_source=False (the --copy behavior): the copy is still verified
    the same way, but the source file is left untouched — non-destructive.

    Returns (success: bool, error_message: Optional[str]) — the message is
    None on success, and a human-readable description of what failed
    otherwise, so callers can persist the real reason to the operations
    log instead of just a bare pass/fail.
    """
    source = Path(source_str)
    dest = Path(dest_str)
    partial_dest = Path(dest_str + PARTIAL_SUFFIX)

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        retry_io_operation(f"Copying {source.name}", shutil.copy2, source, partial_dest)

        src_sha1 = compute_sha1(str(source))
        partial_sha1 = compute_sha1(str(partial_dest))

        if src_sha1 != partial_sha1:
            error_message = f"ChecksumMismatch: SHA1 verification failed for {source.name}"
            logger.error(error_message)
            if partial_dest.exists():
                partial_dest.unlink()
            return False, error_message

        retry_io_operation(f"Rename partial {partial_dest.name}", partial_dest.rename, dest)

        if delete_source:
            retry_io_operation(f"Delete original {source.name}", source.unlink)
            logger.info(f"Successfully migrated: {source.name} -> {dest}")
        else:
            logger.info(f"Successfully copied: {source.name} -> {dest} (source untouched)")
        return True, None
    except Exception as e:
        error_message = f"{type(e).__name__}: {e}"
        logger.error(f"Failed transactional copy for {source_str}: {error_message}")
        if partial_dest.exists():
            try:
                partial_dest.unlink()
            except Exception:
                pass
        return False, error_message


# --- Processing Worker ---
def process_file_task(file_path_str: str, dest_base_path: str, run_id: int) -> ProcessingResult:
    file_path = Path(file_path_str)
    sha1 = compute_sha1(str(file_path))
    phash = compute_phash(str(file_path))

    dt, metadata = get_metadata_and_date(file_path)
    # Keep an explicit, guaranteed-present date_taken key regardless of which
    # capture path produced `metadata`, since downstream consumers (path
    # computation here, and the inspector UI later) shouldn't need to know
    # ExifTool's exact tag-naming conventions just to find "the date."
    metadata["date_taken"] = dt.isoformat()

    year_dir = dt.strftime("%Y")
    month_dir = dt.strftime("%m")
    day_dir = dt.strftime("%d")
    target_folder = Path(dest_base_path) / year_dir / month_dir / day_dir
    initial_dest = target_folder / file_path.name
    final_dest = get_unique_dest_path(initial_dest)
    has_collision = (final_dest != initial_dest)

    return ProcessingResult(
        file_path=str(file_path),
        sha1_hash=sha1,
        phash=phash,
        metadata=metadata,
        status="Pending",
        dest_path=str(final_dest),
        run_id=run_id,
        has_name_collision=has_collision
    )


# --- Single-Instance Enforcement ---
def acquire_single_instance_lock(base_dir: Path):
    """
    Acquires an exclusive, non-blocking OS-level lock (project-spec.md
    §4.1/§7) so at most one engine process ever runs against a given
    --base at a time — Index, Move, and Copy alike, since a rescan racing
    a physical operation on the same database is exactly as unsafe as two
    physical operations racing each other.

    Returns the open file descriptor (caller must keep a reference to it
    for the lock's lifetime — closing it releases the lock) on success, or
    None if another process already holds it.

    Deliberately a flock, not a PID-file-existence check or a DB-row flag:
    the lock is tied to the holding process's open file descriptor, not to
    the file's mere presence on disk, so it is released automatically by
    the kernel on ANY exit path — normal completion, an exception, or an
    uncatchable SIGKILL — with no manual cleanup possible or needed. This
    was verified directly: hard-killing a lock-holding process left the
    lock file sitting on disk looking exactly like a "stale" lock, but a
    fresh process was able to re-acquire it instantly, no waiting, no
    error. A container being force-stopped (`docker stop` timing out into
    SIGKILL) cannot leave this lock in a state requiring manual deletion.

    Caveat: flock reliability is weaker over NFS depending on lockd/statd
    configuration. Not a concern for a local disk or standard Docker
    volume backing --base, but worth a second look if --base is ever
    NFS-mounted.
    """
    lock_path = base_dir / LOCK_FILENAME
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        os.close(fd)
        return None
    # Diagnostic content only — purely informational for a human inspecting
    # the file later, has no bearing on the lock's actual semantics (which
    # are entirely kernel-side, keyed off the open file descriptor above).
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"PID {os.getpid()} — held since {datetime.now().isoformat()}\n".encode())
    except OSError:
        pass
    return fd


def release_single_instance_lock(lock_fd):
    """
    Best-effort explicit release for a clean, immediate unlock on normal
    completion. Not required for correctness — the OS releases the lock
    automatically the moment this process's file descriptors close, on
    every exit path including a crash — but tidier for anything chaining
    multiple engine invocations back-to-back in quick succession.
    """
    if lock_fd is None:
        return
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    except OSError:
        pass


# --- Main Execution ---
def parse_file_ids(value: str) -> List[int]:
    try:
        return [int(x.strip()) for x in value.split(',') if x.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError(f"--file-ids must be a comma-separated list of integers, got: {value}")


def main():
    parser = argparse.ArgumentParser(description="LensLogic - Photo Collection Organizer (Phase 1 Engine)")
    parser.add_argument("--source", default="/data/source", help="Path to source directory (default: /data/source).")
    parser.add_argument("--dest", default="/data/dest", help="Path to destination directory (default: /data/dest).")
    parser.add_argument("--base", default="/appdata", help="Base directory for DB and logs (default: /appdata).")
    parser.add_argument(
        "--workers", type=int, default=None,
        help=f"Worker process count for hashing/date resolution (default: {MAX_WORKER_PROCESSES}, auto-detected CPU count)."
    )
    parser.add_argument(
        "--exts", type=str, default=None,
        help="Comma-separated list of extensions to scan (e.g. '.jpg,.png'), replacing the built-in default set. "
             "Only affects directory scanning, not --file-ids targeting."
    )
    parser.add_argument(
        "--file-ids", type=parse_file_ids, default=None,
        help="Comma-separated list of existing photo IDs (from a prior Index) to target. "
             "Bypasses the full directory scan — processes exactly these already-cataloged files."
    )

    # Only one mode may be active per run — default (no flag) is the existing
    # Index: full scan + hash + date/dest-path resolution, no physical
    # action. The two flags below are mutually exclusive with each other.
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--move", action="store_true",
        help="Execute physical migration (Copy-Verify-Delete, source files are moved/deleted)."
    )
    mode_group.add_argument(
        "--copy", action="store_true",
        help="Non-destructive: copy source files to destination, verified, but never delete or modify the source."
    )
    args = parser.parse_args()

    worker_count = args.workers if args.workers else MAX_WORKER_PROCESSES
    active_extensions = (
        {e.strip().lower() for e in args.exts.split(',') if e.strip()} if args.exts else SUPPORTED_EXTENSIONS
    )

    # 1. Resolve Base Path & Setup Subdirectories
    base_dir = Path(args.base).resolve()
    db_dir = base_dir / "db"
    log_dir = base_dir / "logs"
    db_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "photo_hashes.db"

    # 2. Configure Logging
    configure_logging(log_dir)

    # 2a. Single-instance enforcement (project-spec.md §4.1/§7) — before
    # touching the database or source/dest paths at all. Applies to every
    # mode, including Index, not just --move/--copy.
    lock_fd = acquire_single_instance_lock(base_dir)
    if lock_fd is None:
        logger.error(
            f"FATAL: another LensLogic engine process is already running against --base {base_dir} "
            f"(lock file: {base_dir / LOCK_FILENAME}). Only one operation may run at a time. "
            f"Wait for it to finish, or cancel it, then retry."
        )
        sys.exit(1)

    # 2b. ExifTool is a hard requirement (module docstring) — fail fast and
    # clearly, before touching source/dest/the database at all, rather than
    # limping along in a degraded PIL-only mode the way earlier versions did.
    if not EXIFTOOL_SUPPORTED:
        missing = []
        if not PYEXIFTOOL_PACKAGE_AVAILABLE:
            missing.append("the 'PyExifTool' Python package (pip install pyexiftool)")
        if not EXIFTOOL_BINARY_AVAILABLE:
            missing.append("the 'exiftool' system binary (apt install libimage-exiftool-perl)")
        logger.error(
            "FATAL: ExifTool is a hard requirement for LensLogic and is not available. "
            f"Missing: {' and '.join(missing)}."
        )
        sys.exit(1)

    source_path = Path(args.source).resolve()
    dest_path = Path(args.dest).resolve()

    if not source_path.exists():
        logger.error(f"Source path does not exist: {source_path}")
        release_single_instance_lock(lock_fd)
        return

    mode_label = "COPY" if args.copy else ("MOVE" if args.move else "INDEX")
    logger.info(f"Initializing LensLogic Engine. Mode: {mode_label}")
    logger.info(f"Base Directory: {base_dir}")
    logger.info(f"Source Directory: {source_path}")
    logger.info(f"Destination Directory: {dest_path}")
    logger.info(f"Database Path: {db_path}")
    if args.file_ids:
        logger.info(f"Targeted file IDs: {args.file_ids}")

    # 3. Schema + Startup Recovery
    init_database(str(db_path))
    reconcile_interrupted_state(db_path)

    # 4. Register cancellation handlers and open the run record. Everything
    # from here down is wrapped in try/except/finally so the `runs` row is
    # always finalized — Completed on normal exit, Cancelled if a signal
    # arrived, Failed if anything unexpected blew up — never left "Running"
    # forever from a crash.
    signal.signal(signal.SIGTERM, _handle_cancel_signal)
    signal.signal(signal.SIGINT, _handle_cancel_signal)
    run_id = start_run(str(db_path), mode_label, str(source_path), str(dest_path), args.file_ids)
    run_outcome = "Failed"

    try:
        # 5. Start DB Writer Thread
        db_thread = threading.Thread(target=db_writer_worker, args=(str(db_path),), daemon=True)
        db_thread.start()

        if args.file_ids:
            # Targeted mode: look up already-cataloged paths by ID instead of
            # scanning the filesystem. A file must have gone through at least
            # one prior Index for its ID to exist at all.
            conn = get_db_connection(str(db_path))
            placeholders = ','.join('?' * len(args.file_ids))
            rows = conn.execute(
                f"SELECT id, source_path FROM photos WHERE id IN ({placeholders})", args.file_ids
            ).fetchall()
            conn.close()
            found_ids = {r[0] for r in rows}
            missing = set(args.file_ids) - found_ids
            if missing:
                logger.warning(f"file-ids not found in database (never indexed?): {sorted(missing)}")
            files_to_process = [r[1] for r in rows if Path(r[1]).exists()]
            logger.info(f"Targeting {len(files_to_process)} of {len(args.file_ids)} requested file IDs.")
        else:
            files_to_process = [
                str(p) for p in source_path.rglob('*')
                if p.is_file() and not p.is_symlink() and p.suffix.lower() in active_extensions
            ]
            logger.info(f"Discovered {len(files_to_process)} supported photo/image files to scan.")

        with ProcessPoolExecutor(
            max_workers=worker_count,
            initializer=_init_worker_process,
            initargs=(EXIFTOOL_SUPPORTED,)
        ) as executor:
            futures = [executor.submit(process_file_task, f, str(dest_path), run_id) for f in files_to_process]
            for future in futures:
                res = future.result()
                result_queue.put(res)

        result_queue.join()
        result_queue.put(None)
        db_thread.join()

        logger.info("Scan and indexing completed successfully. Database updated.")

        if args.move or args.copy:
            run_outcome = _run_move_or_copy(args, db_path, dest_path, run_id)
        else:
            run_outcome = "Completed"
            logger.info("Index finished. Pass `--move` to move files, or `--copy` to copy them non-destructively.")

    finally:
        if cancel_requested.is_set() and run_outcome != "Cancelled":
            # Cancellation arrived during the scan phase itself (before the
            # move/copy loop even started) — nothing file-level to log as
            # Cancelled yet since no per-file work was scoped out, but the
            # run itself still needs to be marked accordingly.
            run_outcome = "Cancelled"
        finish_run(str(db_path), run_id, run_outcome)
        logger.info(f"Run #{run_id} finished with status: {run_outcome}")
        release_single_instance_lock(lock_fd)


def _run_move_or_copy(args, db_path: Path, dest_path: Path, run_id: int) -> str:
    """
    Runs the Pre-flight space check, then the Move/Copy loop, then (Move
    only) duplicate source cleanup. Returns the overall run outcome string.
    Checks cancel_requested between files — never mid-file — so a
    cancellation always lets the file currently being copy-verified finish.
    """
    action_verb = "Moving" if args.move else "Copying"
    logger.info(f"{'Move' if args.move else 'Copy'} Mode enabled. Initiating Pre-flight Space Checks...")
    conn = get_db_connection(str(db_path))
    cursor = conn.cursor()

    if args.file_ids:
        placeholders = ','.join('?' * len(args.file_ids))
        cursor.execute(
            f"SELECT id, source_path, dest_path FROM photos WHERE status = 'Pending' AND id IN ({placeholders})",
            args.file_ids
        )
    else:
        cursor.execute("SELECT id, source_path, dest_path FROM photos WHERE status = 'Pending'")
    pending_records = cursor.fetchall()

    total_bytes_needed = sum(
        Path(src[1]).stat().st_size for src in pending_records if Path(src[1]).exists()
    )

    if not verify_sufficient_disk_space(dest_path, total_bytes_needed):
        logger.error("Aborting due to insufficient space on destination drive.")
        conn.close()
        return "Failed"

    logger.info(
        f"Disk space verified. {action_verb} {len(pending_records)} items "
        f"({total_bytes_needed / (1024 ** 2):.2f} MB)..."
    )

    was_cancelled = False
    for index, (record_id, src, dst) in enumerate(pending_records):
        if cancel_requested.is_set():
            was_cancelled = True
            remaining = pending_records[index:]
            logger.warning(f"Cancellation requested — logging {len(remaining)} remaining item(s) as Cancelled.")
            for cancelled_id, cancelled_src, cancelled_dst in remaining:
                log_operation(conn, run_id, cancelled_id, cancelled_src, cancelled_dst, "Cancelled")
            break

        cursor.execute("SELECT has_name_collision FROM photos WHERE id = ?", (record_id,))
        collision_row = cursor.fetchone()
        has_collision = bool(collision_row[0]) if collision_row else False

        cursor.execute("UPDATE photos SET status = 'Processing' WHERE id = ?", (record_id,))
        conn.commit()

        # --move deletes the verified source (delete_source=True, the
        # default); --copy leaves it untouched (delete_source=False).
        success, error_message = copy_verify_delete(src, dst, delete_source=args.move)

        if args.move:
            final_status = "Completed" if success else "Failed"
        else:
            final_status = "Copied" if success else "Failed"
        cursor.execute("UPDATE photos SET status = ? WHERE id = ?", (final_status, record_id))
        conn.commit()
        log_operation(conn, run_id, record_id, src, dst, final_status, error_message, has_collision)

    if args.move and not was_cancelled:
        # Duplicate source-file removal is a --move-only step. It's
        # deliberately gated on status='Completed', which only a --move
        # run ever produces (--copy runs produce 'Copied' instead) — so
        # this block naturally never touches anything from a --copy run,
        # keeping --copy fully non-destructive as intended, with no
        # separate mode check needed here. Skipped entirely if this run was
        # cancelled, since acting on duplicates from a run that didn't
        # finish its primary moves could delete a source file whose "kept"
        # copy was itself never confirmed.
        cursor.execute("SELECT id, source_path, sha1_hash FROM photos WHERE status = 'Duplicate'")
        duplicate_records = cursor.fetchall()
        removed_count = 0
        for record_id, dup_src_str, sha1_hash in duplicate_records:
            dup_src = Path(dup_src_str)
            if not dup_src.exists():
                continue  # already gone (e.g. handled in a prior run)

            cursor.execute(
                "SELECT dest_path FROM photos WHERE sha1_hash = ? AND status = 'Completed' LIMIT 1",
                (sha1_hash,)
            )
            match = cursor.fetchone()
            if match and Path(match[0]).exists():
                try:
                    retry_io_operation(f"Deleting verified duplicate {dup_src.name}", dup_src.unlink)
                    cursor.execute("UPDATE photos SET status = 'Removed_Duplicate' WHERE id = ?", (record_id,))
                    conn.commit()
                    removed_count += 1
                    logger.info(f"Removed duplicate source file: {dup_src} (verified copy at {match[0]})")
                    log_operation(conn, run_id, record_id, dup_src_str, match[0], "Removed_Duplicate")
                except Exception as e:
                    logger.error(f"Failed to remove duplicate source file {dup_src}: {e}")
            else:
                logger.warning(
                    f"No verified copy found on disk for duplicate {dup_src} — "
                    f"leaving source file in place for safety."
                )

        if duplicate_records:
            logger.info(f"Duplicate cleanup: removed {removed_count} of {len(duplicate_records)} flagged duplicates.")

    conn.close()

    if was_cancelled:
        logger.info(f"{'Move' if args.move else 'Copy'} operation cancelled by user request.")
        return "Cancelled"

    logger.info(f"All {'move' if args.move else 'copy'} operations finished.")
    return "Completed"


if __name__ == "__main__":
    main()
