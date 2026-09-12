# Functional & Technical Design Specification: LensLogic Web Interface

## 1. System Overview & Architecture

The LensLogic Web Interface provides a modern web UI for the containerized Python Phase 1 engine (`phase1-organize.py`). It transforms the CLI engine into an interactive application supporting real-time operation monitoring, selective file processing, context-aware duplicate resolution, detailed metadata inspection, dedicated runtime settings management, extension validation, and audit logging.

```
+-----------------------------------------------------------------------------------+
|                                  React Frontend                                   |
|  [ Dashboard ]  [ Gallery / Grid ]  [ Split Inspector ]  [ Settings / Config ]    |
+-----------------------------------------------------------------------------------+
| HTTP REST / WebSockets
+-----------------------------------------------------------------------------------+
|                                 FastAPI Backend                                   |
|  [ Job Queue ]  --->  [ Engine Subprocess Execution ]  --->  [ Log Parser Engine ]|
+-----------------------------------------------------------------------------------+
| SQLite (WAL) / Subprocess
+-----------------------------------------------------------------------------------+
|                             Phase 1 Engine Core                                   |
|   (phase1-organize.py --workers N --exts ex1,ex2 --file-ids id1,id2)              |
+-----------------------------------------------------------------------------------+
```


---

## 2. Core Operational Requirements & Workflows

### Operational Flow

```
1. User triggers an Index scan (full directory or, on a repeat visit,
   just a "Rescan" to pick up newly added files).
   -> FastAPI spawns: python3 phase1-organize.py
   -> Engine scans the full source directory, hashes everything, flags
      duplicates, captures metadata, and populates SQLite.

2. Frontend queries the catalog (paginated/filterable) and renders it
   as a browsable, selectable grid (Gallery view).

3. User selects one or more files and chooses an operation: Move or Copy.

4. Frontend POSTs the selected file IDs + operation to FastAPI
   (`POST /api/v1/jobs/start`, see §6.2).

5. FastAPI spawns the engine again, this time scoped:
   python3 phase1-organize.py --move --file-ids 101,102,105  (or --copy)

6. Job progress streams back to the frontend via WebSocket, reusing the
   engine's own status transitions (Pending -> Processing ->
   Completed/Failed/Removed_Duplicate) rather than a separate progress
   protocol.

7. Frontend updates the Gallery/Operations Drawer in real time as rows
   change status.
```

The full-directory Index (step 1) is the one operation that is *not*
selection-scoped — there's nothing to select from until the catalog
exists. Every operation after that can be either whole-directory or
scoped to a specific selection via `--file-ids`.

### Action Mode Selection
The UI allows switching between execution modes prior to triggering operations:
* **Move Mode (`--move`):** Transactional Copy-Verify-Delete. Deletes source files only after SHA-1 checksum verification succeeds at destination.
* **Copy Mode (`--copy`):** Non-destructive. Performs verified copy to destination while leaving source files untouched.

### Selective File Processing
Users can select individual files or multiple files across grid views to run targeted operations.
* **Multi-Select Controls:** Checkboxes on photo cards, Shift-click range selections, "Select all on page," and "Select all matching current filter" (e.g. every `Pending` file) — the latter matters for large libraries where scrolling to individually check hundreds of thumbnails isn't practical.
* **Sticky Action Bar:** Appears when items are selected, presenting **Move Selected** and **Copy Selected** actions.
* **Targeted Execution:** Uses the `--file-ids <id1,id2>` flag in `phase1-organize.py` to bypass full directory scanning and process only specified database records. IDs were chosen over passing raw file paths specifically because a database primary key is unambiguous and doesn't depend on path strings staying identical between when the frontend fetched the catalog and when the operation actually runs — and it gives the CLI the exact same targeting capability the web UI uses, with no web-only code path.

---

## 3. Dedicated Settings Management (`/settings`)

A dedicated Settings view provides central management of engine parameters, persisted to SQLite and passed to engine instances on startup.

```
+---------------------------------------------------------------------------------+
| SETTINGS & SYSTEM CONFIGURATION                                                 |
+---------------------------------------------------------------------------------+
| WORKER & PROCESS TUNING                                                         |
| Max Worker Processes (MAX_WORKER_PROCESSES):                                  |
| [ 8 ] (Auto-detected: 8 CPU cores. Controls concurrent hashing & I/O threads)   |
|                                                                                 |
| QUEUE & BACKPRESSURE MANAGEMENT                                                 |
| DB Queue Size (DB_QUEUE_SIZE):                                                |
| [ 1000 ] items (Maximum pending database write operations before backpressure)  |
+---------------------------------------------------------------------------------+
| SUPPORTED FILE EXTENSIONS (SUPPORTED_EXTENSIONS)                             |
| Selected Formats:                                                               |
| [x] .jpg   [x] .jpeg   [x] .cr2   [x] .nef   [x] .arw   [x] .dng               |
| [x] .heic  [x] .png    [x] .tiff  [ ] .mp4   [ ] .mov                          |
|                                                                                 |
| Add Custom Extension:                                                           |
| [ .txt                  ]  [ + Add Extension ]                                  |
|                                                                                 |
| (!) WARNING: Custom extension '.txt' does not natively support EXIF metadata.    |
|     When no EXIF data is present, filesystem creation/modification date will     |
|     be used for organization.                                                   |
+---------------------------------------------------------------------------------+
|                                                   [ RESET ]  [ SAVE SETTINGS ]  |
+---------------------------------------------------------------------------------+
```


### 3.1 Extension EXIF Support Validation Subsystem
When a user attempts to add or select a custom extension in the settings panel or via API, the backend/UI validates it against a metadata-support registry:

1. **Standard EXIF Image Formats (Native Support):** `.jpg`, `.jpeg`, `.tiff`, `.tif`, `.heic`, `.heif`, `.webp`, and RAW formats (`.cr2`, `.cr3`, `.nef`, `.arw`, `.dng`, `.rw2`, `.orf`, `.pef`).
2. **Non-EXIF Formats (Trigger Non-Blocking Warning):** Container formats or plain files (e.g., `.png`, `.bmp`, `.gif`, `.mp4`, `.mov`, `.mkv`, `.avi`, `.txt`).
3. **UI Warning UX:** Displays an inline warning badge: *"File type `.ext` does not support EXIF data. When no EXIF data is available, file creation/modification date will be used."*
4. **Validation Behavior:** The warning is **informative/non-blocking**. Users can still add non-EXIF file types; the backend flags `has_exif_support = false` in the config state so the engine falls back gracefully to filesystem timestamps (`mtime`/`ctime`).

---

## 4. UI Layouts & Component Specs

### 4.1 Real-Time Operations Drawer
When a job is active, a progress drawer expands at the bottom of the viewport.

```
+-----------------------------------------------------------------------------------+
| [X] Moving 3 Files...  | Progress: [========............] 66% (1 Remaining)       |
+-----------------------------------------------------------------------------------+
| CURRENT STEP: Verification & Checksum Comparison (Workers: 8 | Queue: 42/1000)   |
| LOGS:                                                                             |
| - [DONE] IMG_001.JPG -> /data/dest/2026/02/14/IMG_001.JPG (SHA1 Verified)         |
| - [IN PROGRESS] IMG_002.CR2 -> /data/dest/2026/02/14/IMG_002.CR2                   |
+-----------------------------------------------------------------------------------+
```

* **Metrics:** Active step, progress percentage, active worker count, current DB queue backpressure level, files completed vs. remaining.
* **Live Log Stream:** Direct source-to-destination mapping display with verification status.
* **Job Control:** Provides a **Cancel Job** button. Sends `SIGTERM` to the engine subprocess (§6.2 `jobs/{id}/cancel`); the file currently being copy-verified finishes normally, then every remaining targeted file is logged to the `operations` audit table with status `Cancelled` (not silently dropped — visible in the run's history afterward) and duplicate-source cleanup for that run is skipped entirely.

### 4.2 Split-Screen Photo Inspector Panel
Clicking an image opens a right-side 50% detail panel.

```
+---------------------------------------------------------------------------------+
| PHOTO DETAIL INSPECTOR                                                      [X] |
+---------------------------------------------------------------------------------+
| [                    IMAGE PREVIEW                    ]                         |
+---------------------------------------------------------------------------------+
| FILE INFORMATION                                                                |
| Path at Destination:  /data/dest/2026/02/14/IMG_0001_1.JPG                      |
| Collision Status:     Renamed on target (Name collision resolved)               |
|                                                                                 |
| Path from Source:     /data/source/sd_card/IMG_0001-1234.JPG                     |
|                                                                                 |
| Timestamps:           Created: 2026-02-14 10:30:00 | Modified: 2026-02-14 10:30:00 |
+---------------------------------------------------------------------------------+
| EXIF & METADATA                                                                 |
| Date Taken: 2026:02:14 10:30:00  | Camera: Canon EOS R5                         |
| ISO: 100   Aperture: f/2.8      | Shutter: 1/1000s                             |
| SHA-1: a4b8c9d123...             | pHash: 1001101...                             |
+---------------------------------------------------------------------------------+
| DISCOVERED DUPLICATES (SOURCE & DESTINATION)                                    |
| * [SOURCE] /data/source/sd_card/IMG_0001-1234.JPG (Completed)                   |
| * [DEST]   /data/dest/2026/02/14/IMG_0001_1.JPG (Completed)                      |
+---------------------------------------------------------------------------------+
```


#### Inspector Data Display Matrix

| Field Category | Source Context View (`/data/source`) | Destination Context View (`/data/dest`) |
| :--- | :--- | :--- |
| **Media Preview** | Rendered preview (standard / RAW via `rawpy`) | Rendered preview |
| **Destination Path** | Target computed path | Assigned destination path (`Path at Destination`) |
| **Source Path** | Current source path | Original source path (`Path from Source`) |
| **Timestamps** | File Created & Modified dates | File Created & Modified dates |
| **EXIF & Hashes** | EXIF metadata, SHA-1, pHash | EXIF metadata, SHA-1, pHash |
| **Duplicates List** | Matching paths across **both** Source & Destination | Matching paths across **both** Source & Destination |

#### 4.2.1 Thumbnail Generation (Media Preview backing)

The Gallery grid and Inspector's "Media Preview" both need something to actually render — this requires new engine-side work, not just a frontend concern, since the engine is the only thing with RAW-decode capability (`rawpy`) already loaded.

* **Generation point:** During Index, alongside SHA1/pHash computation — reuses the image decode already happening for pHash rather than a second pass over the file (standard/HEIC formats via `PIL.Image`, RAW-family via `rawpy`).
* **Storage:** Small JPEG (longest edge ~256px), written to `/appdata/thumbnails/<sha1>.jpg`, keyed by hash so identical files (including cross-directory duplicates) share one thumbnail instead of generating redundant copies.
* **Schema:** New `thumbnail_path` column on `photos` (nullable) — see §6.1.
* **Failure handling:** A thumbnail generation failure (corrupt file, unsupported variant) must not fail the overall Index for that file — log and leave `thumbnail_path` NULL; the Gallery/Inspector show a placeholder icon for those rows instead of erroring.
* **Serving:** `GET /api/v1/photos/{id}/thumbnail` (§6.2) serves the file directly from `/appdata/thumbnails/`.

---

## 5. Safeguards, Operations & Error Handling

### 5.1 Pre-Flight Disk Space Protection
Before initiating any move or copy job, the system computes total payload size plus a 500 MB safety buffer. If destination disk space is insufficient, execution is blocked and a warning banner displays required vs. available space.

### 5.2 Job Persistence & Background Execution
Jobs run asynchronously in FastAPI. If a user closes or refreshes their browser, the job continues unaffected. Reopening the web UI re-establishes the WebSocket connection to stream live progress.

### 5.3 Error Center
If operations fail, an Error Banner highlights the failures, sourced directly from the `operations` log's `error_message` column (see §6.1) — the real exception text is persisted, not just a generic "failed" flag.

```
+-----------------------------------------------------------------------------------+
| FAILED OPERATIONS (2 Items)                                                       |
+-----------------------------------------------------------------------------------+
| 1. IMG_0488.CR2                                                                    |
|    Source: /data/source/imports/IMG_0488.CR2                                      |
|    Error:  PermissionError: [Errno 13] Permission denied: '/data/dest/...'        |
|                                                                                   |
| 2. IMG_0912.JPG                                                                   |
|    Source: /data/source/corrupted/IMG_0912.JPG                                   |
|    Error:  ChecksumMismatch: SHA-1 verification failed                            |
+-----------------------------------------------------------------------------------+
```

Users can view exact system error strings (e.g., `PermissionError`, `ChecksumMismatch`).

**No dedicated retry subsystem.** There is no "Retry Item" / "Retry All Failed" backend endpoint and no `retry_count` tracking. A failed file's `photos.status` is reset to `Pending` automatically the next time it's re-indexed (a plain re-scan, full or `--file-ids`-scoped), so retrying is just re-running the same operation — files that already succeeded are gone from `--source` and won't be touched again, so this is fast even for a large batch with only a few failures. The web UI's equivalent of "retry" is simply selecting the failed items (they're still visible with `status = 'Failed'`) and re-issuing the same Move/Copy operation via `POST /api/v1/jobs/start` with their IDs in `file_ids` — no new endpoint required.

### 5.4 Operations Audit Log (`/logs`)
A searchable table logging every operation performed by the engine:
* **Columns:** Timestamp, Mode (`MOVE`/`COPY`), Source Path, Destination Path, Status (`Completed`, `Copied`, `Removed_Duplicate`, `Failed`), and System Error Message.
* **Controls:** Filter by date, status, or free-text search; CSV/JSON export.

---

## 6. Database Schema & API Specifications

### 6.1 SQLite Schema

The schema below reflects what's actually implemented in `phase1-organize.py`, not a set of `ALTER TABLE` additions on top of the original `photos` table. Error tracking, name-collision flags, and original filenames live in a dedicated **audit log table** rather than as columns bolted onto `photos` — see the design note below for why.

```sql
-- photos: CURRENT STATE only, one row per source_path (UNIQUE constraint
-- enforces this). Continuously overwritten in place on every re-scan —
-- this is what keeps "what's still Pending" queries fast, and is the
-- table --file-ids targets by primary key.
CREATE TABLE photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path TEXT UNIQUE,
    dest_path TEXT,
    sha1_hash TEXT,
    phash TEXT,
    collision_group INTEGER,      -- reserved for Phase 3 fuzzy clustering
    is_master BOOLEAN DEFAULT 0,  -- reserved for Phase 3 fuzzy clustering
    status TEXT,                  -- Pending, Processing, Completed, Failed,
                                   -- Duplicate, Removed_Duplicate, Copied
    metadata_json TEXT,           -- full captured EXIF/metadata, not just date
    has_name_collision BOOLEAN DEFAULT 0,
    thumbnail_path TEXT           -- NEW, not yet implemented — see §4.2.1.
                                   -- NULL until Phase 2's thumbnail
                                   -- generation work lands.
);

-- runs: one row per engine invocation (Index, Move, or Copy). This is
-- what "previous run information" (§5.4) is actually built from — no
-- separate run-history table needed beyond this.
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    source_path TEXT,
    dest_path TEXT,
    file_ids_filter TEXT,   -- JSON array, NULL for a full directory scan
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL    -- Running, Completed, Cancelled, Failed, Crashed
);

-- operations: the audit LOG. Append-only — one row per file per run, so
-- the same file can appear multiple times across different attempts
-- without losing history the way overwriting a column on `photos` would.
-- This is what backs §5.3 (Error Center) and §5.4 (Audit Log) directly.
CREATE TABLE operations (
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
);
```

**Design note — why a log table instead of columns on `photos`:** `photos` answers "what's the current state of this file?" A single `error_message`/`retry_count` column on that table can only ever hold the *most recent* attempt's outcome — it can't show that a file failed twice with different errors before eventually succeeding, and it can't answer "show me everything that happened in run #47." Since §5.4 explicitly requires a Timestamp column and per-run history, and §5.3's retry flow needs to reference a specific failed *attempt*, an append-only `operations` table (joined to a `runs` table for run-level context like start/end time and overall outcome) satisfies both requirements directly, where a couple of extra columns on `photos` could not.

**No `retry_count` column.** There is no retry subsystem — see the note on §5.3 above.

```sql
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

### 6.2 Key REST API Endpoints
POST /api/v1/settings/validate-extension

Validates whether a provided file extension supports EXIF metadata.

    Request Body:
    JSON

    {
      "extension": ".mp4"
    }

    Response:
    JSON

    {
      "extension": ".mp4",
      "supports_exif": false,
      "warning": "File type '.mp4' does not support EXIF data. When no EXIF data is available, file creation/modification date will be used."
    }

GET /api/v1/settings

Retrieves persisted system settings along with EXIF support status for each configured extension.

    Response:
    JSON

    {
      "max_worker_processes": 8,
      "supported_extensions": [
        { "ext": ".jpg", "supports_exif": true },
        { "ext": ".cr2", "supports_exif": true },
        { "ext": ".png", "supports_exif": false, "warning": "File type '.png' does not support EXIF data. When no EXIF data is available, file creation/modification date will be used." }
      ]
    }

PUT /api/v1/settings

Updates global engine settings.

    Body:
    JSON

    {
      "max_worker_processes": 8,
      "supported_extensions": [".jpg", ".cr2", ".png"]
    }

POST /api/v1/jobs/start

Starts an engine execution job, automatically injecting active configuration parameters from /settings if not explicitly overridden.

    Body:
    JSON

    {
      "mode": "move",
      "file_ids": [101, 102, 105]
    }

POST /api/v1/jobs/{id}/cancel

Sends SIGTERM to the engine subprocess for graceful job cancellation — the currently in-flight file finishes, every remaining targeted file is logged to `operations` with status `Cancelled`, and duplicate-source cleanup is skipped for that run.

GET /api/v1/photos/{id}/thumbnail

Serves the thumbnail JPEG for a photo (see §4.2.1), read from `/appdata/thumbnails/<sha1>.jpg` via the row's `thumbnail_path`. Returns a placeholder/404 if `thumbnail_path` is NULL.

GET /api/v1/photos/{id}/inspect

Returns inspector details for a specific photo.

    Response:
    JSON

    {
      "id": 502,
      "status": "Completed",
      "source_info": {
        "path": "/data/source/sd_card/IMG_0001-1234.JPG"
      },
      "destination_info": {
        "path": "/data/dest/2026/02/14/IMG_0001_1.JPG",
        "has_collision_rename": true
      },
      "timestamps": { 
        "created": "2026-02-14T10:30:00Z", 
        "modified": "2026-02-14T10:30:00Z" 
      },
      "exif": { 
        "date_taken": "2026-02-14T10:30:00Z", 
        "camera": "Canon EOS R5" 
      },
      "hashes": { 
        "sha1": "a4b8c9...", 
        "phash": "1001101..." 
      },
      "duplicates": [
        { "location_type": "source", "path": "/data/source/sd_card/IMG_0001-1234.JPG", "status": "Completed" },
        { "location_type": "destination", "path": "/data/dest/2026/02/14/IMG_0001_1.JPG", "status": "Completed" }
      ]
    }

GET /api/v1/photos?status=Failed

Fetches failed items for display in the Error Center (§5.3). "Retrying" is just selecting these IDs and calling `POST /api/v1/jobs/start` again with the same mode — no separate retry endpoint, per the design note in §5.3.

---

## 7. Explicitly Out of Scope

* **Fuzzy-match clustering, similarity grouping, EXIF editing/synchronization** — Phase 3 scope, see `phase3-spec.md`.
* **Multi-user auth/sessions** — not addressed in this spec. Add as a separate concern if the web UI needs to be exposed beyond a single trusted user on a local/private network.