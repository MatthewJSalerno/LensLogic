"""
Photo Organizer — Phase 1 (of 3)
==================================

PROJECT GOAL
    Organize a large photo collection by:
      1. Organizing   - sort photos into a Year/Month/Day directory structure
      2. Deduplicating - identify and remove duplicate images, preserving EXIF
      3. Cleaning      - identify and group/remove visually similar photos

PROJECT SCOPE
    - Recursive scan of the source directory, any nesting depth
    - Formats: .jpg, .jpeg, .png, .heic, .tiff, .raw, .dng
    - Video files: scanned, but explicitly excluded from deduplication
    - Archives (.zip, .tar.gz, etc.) and non-image files: ignored
    - Google Takeout archives: out of scope (JSON metadata parsing not handled)

THIS SCRIPT'S STATUS (Phase 1 — Organization)
    Done:
      - Recursive scan, sorts into Year/Month/Day using EXIF "Date Taken"
      - Falls back to file modification time when EXIF is missing/unreadable
      - Duplicate-filename safety: never overwrites, appends a numeric suffix
      - SHA1 checksum per file; exact (byte-identical) duplicates are deleted
        rather than moved, keeping the first copy encountered
      - Perceptual hash (pHash) computed and stored per file for Phase 3's
        near-duplicate grouping, but NOT acted on here (no fuzzy-match
        deletions happen in this phase)
      - SHA1/pHash persisted to a SQLite DB so re-runs don't re-hash files
        already processed in a prior --live run
      - Dry-run by default; --live required to actually move/delete anything

    Not yet done (future phases):
      - Video file handling
      - Google Takeout support
      - Phase 3: acting on perceptual-hash similarity to group/remove
        near-duplicates (visually similar but not byte-identical)

Required libraries:
    pip install Pillow          # core image handling (required)

Optional libraries (script runs without them, with reduced functionality):
    pip install pillow-heif     # enables EXIF reads for .heic files
    pip install exifread        # enables EXIF reads for .raw / .dng files
    pip install imagehash       # enables perceptual hash (pHash) computation

Standard library only (no install needed): os, shutil, logging, argparse,
hashlib, sqlite3, datetime, pathlib
"""

import os
import shutil
import logging
import argparse
import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path
from PIL import Image

# Optional: HEIC support. Falls back gracefully if the plugin isn't installed.
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIC_SUPPORTED = True
except ImportError:
    HEIC_SUPPORTED = False

# Optional: RAW/DNG support via exifread (lighter than rawpy, EXIF-only).
try:
    import exifread
    EXIFREAD_SUPPORTED = True
except ImportError:
    EXIFREAD_SUPPORTED = False

# Optional: perceptual hashing, for Phase 3's near-duplicate grouping.
# Computed now (while every file is already being opened) but not acted on.
try:
    import imagehash
    IMAGEHASH_SUPPORTED = True
except ImportError:
    IMAGEHASH_SUPPORTED = False

SHA1_CHUNK_SIZE = 65536

SUPPORTED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.heic', '.tiff', '.raw', '.dng'}
RAW_EXTENSIONS = {'.raw', '.dng'}  # formats PIL can't open directly

# EXIF tags that may hold the "date taken" value, in priority order.
DATE_TAGS_PIL = (36867, 36868, 306)  # DateTimeOriginal, DateTimeDigitized, DateTime
EXIFREAD_TAG_KEYS = (
    'EXIF DateTimeOriginal',
    'EXIF DateTimeDigitized',
    'Image DateTime',
)

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def parse_exif_date(value):
    """Parse an EXIF date string ('YYYY:MM:DD HH:MM:SS') into a datetime."""
    try:
        return datetime.strptime(str(value)[:19], '%Y:%m:%d %H:%M:%S')
    except (ValueError, TypeError):
        return None


def get_date_from_exif(file_path, ext):
    """
    Attempts to extract 'Date Taken' from EXIF data.
    Uses PIL for formats it understands natively (jpg, png, tiff, heic-with-plugin).
    Falls back to exifread for RAW/DNG, which PIL cannot open.
    Returns a datetime object, or None if not found/unreadable.
    """
    # RAW/DNG: PIL can't open these, so go straight to exifread if available.
    if ext in RAW_EXTENSIONS:
        if not EXIFREAD_SUPPORTED:
            return None
        try:
            with open(file_path, 'rb') as f:
                tags = exifread.process_file(f, details=False, stop_tag='EXIF DateTimeOriginal')
            for key in EXIFREAD_TAG_KEYS:
                if key in tags:
                    return parse_exif_date(tags[key])
        except Exception as e:
            logger.debug(f"exifread failed on {file_path.name}: {e}")
        return None

    # HEIC without the plugin registered: PIL.Image.open() will raise.
    if ext == '.heic' and not HEIC_SUPPORTED:
        return None

    try:
        with Image.open(file_path) as img:
            exif_data = img.getexif()
            if exif_data:
                for tag in DATE_TAGS_PIL:
                    if tag in exif_data:
                        parsed = parse_exif_date(exif_data[tag])
                        if parsed:
                            return parsed
    except Exception as e:
        logger.debug(f"PIL EXIF read failed on {file_path.name}: {e}")

    return None


def get_fallback_date(file_path):
    """Falls back to the file's modification time."""
    return datetime.fromtimestamp(os.path.getmtime(file_path))


def compute_sha1(file_path):
    """Streams the file in chunks and returns its SHA1 hex digest."""
    h = hashlib.sha1()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(SHA1_CHUNK_SIZE), b''):
            h.update(chunk)
    return h.hexdigest()


def compute_phash(file_path, ext):
    """
    Computes a perceptual hash (pHash) for near-duplicate detection later.
    Best-effort: returns None for formats that can't be opened as an image
    (RAW/DNG, or HEIC without the plugin) or if imagehash isn't installed.
    This value is stored but NOT used for any deletion decision in Phase 1.
    """
    if not IMAGEHASH_SUPPORTED:
        return None
    if ext in RAW_EXTENSIONS:
        return None
    if ext == '.heic' and not HEIC_SUPPORTED:
        return None
    try:
        with Image.open(file_path) as img:
            return str(imagehash.phash(img))
    except Exception as e:
        logger.debug(f"pHash failed on {file_path.name}: {e}")
        return None


def init_db(db_path):
    """Creates (if needed) the SQLite DB used to track hashes across runs."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS photos (
            sha1 TEXT PRIMARY KEY,
            phash TEXT,
            original_path TEXT NOT NULL,
            final_path TEXT NOT NULL,
            date_taken TEXT,
            processed_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def lookup_sha1(conn, sha1):
    """Returns the stored row for a hash if this file was already processed, else None."""
    cur = conn.execute("SELECT final_path FROM photos WHERE sha1 = ?", (sha1,))
    return cur.fetchone()


def record_photo(conn, sha1, phash, original_path, final_path, date_taken):
    conn.execute(
        "INSERT OR REPLACE INTO photos (sha1, phash, original_path, final_path, date_taken, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (sha1, phash, str(original_path), str(final_path), date_taken.isoformat(), datetime.now().isoformat()),
    )
    conn.commit()


def get_unique_path(target_path):
    """If a file with the same name exists, appends a counter (image_1.jpg, image_2.jpg, ...)."""
    if not target_path.exists():
        return target_path

    parent = target_path.parent
    stem = target_path.stem
    suffix = target_path.suffix

    counter = 1
    while True:
        new_path = parent / f"{stem}_{counter}{suffix}"
        if not new_path.exists():
            return new_path
        counter += 1


def organize_photos(source_dir, dest_dir, db_path, dry_run=True):
    src_root = Path(source_dir).expanduser().resolve()
    dest_root = Path(dest_dir).expanduser().resolve()

    if not src_root.exists():
        logger.error(f"Source directory {src_root} does not exist.")
        return

    if not dry_run:
        dest_root.mkdir(parents=True, exist_ok=True)

    conn = init_db(db_path)

    logger.info("DRY RUN MODE" if dry_run else "LIVE MODE")
    logger.info(f"Scanning: {src_root}")
    logger.info(f"Hash DB: {Path(db_path).resolve()}")
    if not HEIC_SUPPORTED:
        logger.warning("pillow-heif not installed — .heic files will use fallback date only.")
    if not EXIFREAD_SUPPORTED:
        logger.warning("exifread not installed — .raw/.dng files will use fallback date only.")
    if not IMAGEHASH_SUPPORTED:
        logger.warning("imagehash not installed — perceptual hashes will not be computed.")

    count_moved = 0
    count_exif_hits = 0
    count_fallback = 0
    count_duplicates = 0
    count_errors = 0

    # Dry runs never touch the DB (so you can preview repeatedly without side
    # effects). Duplicates *within* a single dry-run scan are still caught
    # using this in-memory map; only a --live run persists hashes to SQLite.
    session_hashes = {}

    try:
        for file_path in src_root.rglob('*'):
            if not file_path.is_file():
                continue

            ext = file_path.suffix.lower()
            if ext not in SUPPORTED_EXTENSIONS:
                continue

            try:
                sha1 = compute_sha1(file_path)

                # Exact duplicate of a file already processed (this run or, for
                # --live runs, a prior run recorded in the DB)?
                existing_path = session_hashes.get(sha1)
                if not existing_path and not dry_run:
                    row = lookup_sha1(conn, sha1)
                    existing_path = row[0] if row else None

                if existing_path:
                    count_duplicates += 1
                    if dry_run:
                        logger.info(f"[DRY RUN] Would delete exact duplicate: {file_path} (matches {existing_path})")
                    else:
                        file_path.unlink()
                        logger.info(f"Deleted exact duplicate: {file_path} (matches {existing_path})")
                    continue

                date_obj = get_date_from_exif(file_path, ext)
                if date_obj:
                    count_exif_hits += 1
                else:
                    date_obj = get_fallback_date(file_path)
                    count_fallback += 1

                phash = compute_phash(file_path, ext)

                target_dir = dest_root / date_obj.strftime('%Y') / date_obj.strftime('%m') / date_obj.strftime('%d')
                final_path = get_unique_path(target_dir / file_path.name)

                if dry_run:
                    logger.info(f"[DRY RUN] Would move: {file_path.name} -> {final_path}")
                    session_hashes[sha1] = str(final_path)
                else:
                    target_dir.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(file_path), str(final_path))
                    logger.info(f"Moved: {file_path.name} -> {final_path}")
                    record_photo(conn, sha1, phash, file_path, final_path, date_obj)
                    count_moved += 1

            except Exception as e:
                count_errors += 1
                logger.error(f"Failed to process {file_path}: {e}")
    finally:
        conn.close()

    logger.info("Process complete.")
    logger.info(
        f"EXIF date found: {count_exif_hits} | Fallback to mtime: {count_fallback} | "
        f"Exact duplicates: {count_duplicates} | Errors: {count_errors}"
    )
    if dry_run:
        logger.info("No files were moved or deleted. Re-run with --live to perform the changes.")
    else:
        logger.info(f"Successfully moved {count_moved} images, deleted {count_duplicates} exact duplicates.")


def main():
    parser = argparse.ArgumentParser(description="Organize photos into a Year/Month/Day structure.")
    parser.add_argument("--source", help="Path to your source photos.")
    parser.add_argument("--dest", help="Path for the new organized structure.")
    parser.add_argument("--live", action="store_true", help="Actually move files. Omit for a dry run.")
    parser.add_argument("--db", default="photo_hashes.db", help="Path to the SQLite hash DB (default: photo_hashes.db).")
    args = parser.parse_args()

    source = args.source or input("Enter the path to your source photos: ").strip()
    dest = args.dest or input("Enter the path for the new organized structure: ").strip()

    organize_photos(source, dest, db_path=args.db, dry_run=not args.live)


if __name__ == "__main__":
    main()