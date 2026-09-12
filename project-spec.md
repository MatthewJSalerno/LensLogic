# Project Design Specification: NegativeSpace

## 1. Project Overview
**Objective:** A web-based application designed to automate the organization of large, complex photo collections.
**Core Problem:** Users struggle with redundant files, non-standardized directory structures, and inconsistent metadata across multiple devices/exports.

**Development Roadmap:**
*   **Phase 1 (Core Engine - MVP) — Implemented:** Backend "heavy lifting," delivered as a standalone Python CLI engine.
    *   Automated organization into structured `YYYY/MM/DD` directories, based on EXIF "Date Taken."
    *   Generation and storage of SHA1 and pHash for every supported file.
    *   Full metadata capture (not just date) — camera, ISO, aperture, shutter speed, and whatever else the source format exposes.
    *   Exact deduplication (SHA1-based), including safe removal of duplicate source files.
    *   Selection-scoped operations via `--file-ids`, usable from the CLI directly or driven by the Phase 2 web UI.
    *   Graceful cancellation (`SIGTERM`/`SIGINT`), with a full audit trail of what was cancelled.
    *   **Safety Protocols:** "Index" default, "Copy-Verify-Delete"/"Copy-Verify" for physical operations, crash-safe resume (including run-history reconciliation after a hard kill).
    *   **Multi-Source Metadata:** Support for standard images, HEIC, and professional RAW formats (RAW, DNG, CR2, NEF, ARW, RAF).
*   **Phase 2 (Web UI) — In Design:** Managing Move/Copy/Index operations from a browser, with file-level selection. See [phase2-spec.md](./phase2-spec.md).
*   **Phase 3 (Discovery & Analysis) — Not Started:** Fuzzy-matching visual reports, similarity clustering, "Merger" tools, EXIF editing/synchronization. See [phase3-spec.md](./phase3-spec.md).

## 2. Target Audience
*   **Primary:** Professional photographers and content creators managing thousands of assets.
*   **Secondary:** General users with large, unorganized mobile/camera backups.

## 3. System Architecture (Polyglot Design)
To balance heavy-duty data processing with a high-quality user experience, the application utilizes a multi-layered architecture:

### 3.1. The Frontend (User Experience) — Phase 2
*   **Technology:** Modern Web Framework (e.g., React, Vue, or Svelte).
*   **Function:** Provides a dashboard for file path configuration, catalog browsing with file-level selection, Index report viewing, and real-time progress tracking via WebSockets. See `phase2-spec.md` for details.

### 3.2. The Web & API Layer (Node.js / FastAPI) — Phase 2
*   **Role:** Acts as the "Command Center" and communication bridge.
*   **Function:** Handles HTTP/WebSocket requests, spawns the Python engine as a child process (passing `--file-ids` for selection-scoped operations), reads the SQLite database to report progress, and relays real-time updates to the frontend. This is the only layer that talks to both the Python engine and the database directly — the frontend talks only to this layer.
*   **Communication:** WebSockets for real-time updates during long-running tasks; sends `SIGTERM` to the engine subprocess for graceful cancellation.

### 3.3. The Processing Engine (Python) — Implemented (`ns-engine.py`)
*   **Role:** The "Workhorse" for data heavy-lifting. Runs as a standalone CLI process today; Phase 2's API layer invokes it as a child process rather than replacing it.
*   **Function:** Handles filesystem crawling (or targeted ID lookup), EXIF/full-metadata extraction, SHA1/pHash generation, and physical file manipulation.
*   **Concurrency:**
    *   A `ProcessPoolExecutor` (sized to `os.cpu_count()`, overridable via `--workers`) parallelizes hashing and metadata extraction across CPU cores.
    *   A Producer-Consumer model hands results to a single dedicated background thread, which is the *only* thread that ever writes to SQLite — this is what guarantees the "single writer" thread-safety requirement in §7, rather than relying on SQLite's own locking alone.
*   **Key Libraries:**
    *   `Pillow` (+ `pillow-heif` for HEIC) — standard-format image decoding and EXIF reads.
    *   `imagehash` — pHash generation.
    *   `rawpy` — RAW-family pixel decoding, required for pHash generation on `.raw/.dng/.cr2/.nef/.arw/.raf` files (Pillow cannot open these formats at all).
    *   `ExifTool` (external system binary + `PyExifTool` Python wrapper) — **hard requirement**, not optional. Invoked as a persistent per-worker process (`-stay_open` mode) rather than a fresh subprocess per file — measured directly at roughly a 30x reduction in per-file ExifTool overhead (~74ms → ~2.5ms) for the ExifTool call itself. The only method in the engine that can read metadata from RAW-family files. The engine refuses to start if either the binary or the Python package is missing, rather than silently degrading — see §4.2 for exactly what it's used for and why Pillow/rawpy/imagehash are still required alongside it, not replaced by it.

## 4. Functional Requirements
### 4.1. Input & Configuration
*   **Path Definitions:** `--source` (default `/data/source`), `--dest` (default `/data/dest`), and `--base` (default `/appdata`, holding `<base>/db/photo_hashes.db` and `<base>/logs/organizer.log`).
*   **Tuning:**
    *   `--workers <N>` — overrides the `ProcessPoolExecutor` worker count (default: `os.cpu_count()`).
    *   `--exts <.ext1,.ext2,...>` — overrides the default extension set for directory scanning. Has no effect on `--file-ids` targeting.
    *   The database write-queue size is intentionally **not** configurable — left as a hardcoded internal constant rather than exposed, since there was no concrete need identified for tuning it separately from `--workers`.
*   **Targeted Processing** (mutually exclusive with each other — pick at most one, or omit both for a full directory scan):
    *   `--file-ids <id1,id2,...>` — comma-separated `photos.id` values from a prior Index. Bypasses the directory scan entirely; looks up each ID's `source_path` directly and processes exactly those files. IDs not found in the database are logged as a warning and skipped, not treated as fatal. This is what a web UI's individual/multi-select maps onto, but works identically from the CLI.
    *   `--source-subdir <path>` — scopes the operation to every already-indexed file whose `source_path` falls under this directory, recursively. Queries the existing `photos` catalog (`WHERE source_path LIKE '<path>/%'`) rather than re-walking the filesystem, which means it inherently excludes symlinks (they were already excluded at the original Index that populated those rows) and — critically — avoids passing a large ID list as a command-line argument at all. This is the mechanism behind the web UI's "select a folder" option, and the recommended path for large selections instead of enumerating thousands of individual `--file-ids` (see `phase2-spec.md` §2 for the UI-side selection-size limit this replaces for bulk operations). Only reflects files known as of the last Index over that path — newly added files need a rescan first, same as the whole-library case.
*   **Operational Modes** (mutually exclusive — at most one flag; the engine will refuse to start if more than one is given):
    *   **Index (default, no flag):** Full scan (or `--file-ids`/`--source-subdir`-scoped lookup), hashing, metadata resolution, and destination-path computation. Every result is written to the database (including duplicate flagging), but no file is copied, moved, or deleted.
    *   **`--move`:** Runs the Copy-Verify-Delete protocol (§4.2) for every targeted `Pending` file. Source files are deleted only after a verified copy lands at the destination. Confirmed exact duplicates are also removed from source once a verified copy of their content exists elsewhere at the destination.
    *   **`--copy`:** Runs the same verified Copy-Verify step as `--move`, but the source file is never deleted or modified — including duplicate source files, which are left untouched. Fully non-destructive; safe to run against a read-only-mounted source.
*   **Metadata Support:** Standard formats (JPEG, PNG, TIFF), HEIC, and professional RAW formats (RAW, DNG, CR2, NEF, ARW, RAF).
*   **Cancellation:** Sending `SIGTERM` or `SIGINT` during a `--move`/`--copy` run lets the file currently being copy-verified finish, then stops before starting the next one. See §4.2 for what happens to the rest of the batch.
*   **Retries:** No dedicated retry mechanism or `retry_count` tracking. Re-running the same command retries whatever's still `Pending` (including previously `Failed` files, which are reset to `Pending` by the next Index) — already-successful files are gone from `--source` and won't be reprocessed, so this is cheap even for a large batch with only a few failures.
*   **Single-Instance Enforcement (implemented):** At startup, before touching the database, the engine acquires an exclusive OS-level lock (`fcntl.flock`, non-blocking) on a fixed lock file (`<base>/engine.lock`). If the lock is already held, the process logs a clear error and exits immediately without modifying anything — **this applies to every mode**, including Index, not just `--move`/`--copy`, since a rescan racing a physical operation on the same database is exactly as unsafe as two physical operations racing each other (see §4.2). The lock is held for the entire process lifetime and released automatically by the OS on any exit path — normal completion, an unhandled exception, or an uncatchable `SIGKILL` — so there is no manual "is the lock stale" reconciliation step needed, unlike the `runs`-table crash recovery in §4.2, which exists for a different purpose (historical accuracy of run records, not mutual exclusion). Verified directly: a lock-holding process was hard-killed (`SIGKILL`) mid-run, its lock file confirmed still present on disk with the dead process's PID in it, and a fresh invocation against the same `--base` immediately succeeded with no delay and no manual cleanup — a container being force-stopped (`docker stop` timing out into `SIGKILL`) cannot leave this lock in a state requiring intervention. Also verified: two different `--base` directories are independent locks and can run concurrently without interfering with each other.
*   **NFS caveat:** `flock` reliability is weaker over NFS, depending on the NFS server/client's `lockd`/`statd` configuration — locks that work reliably on local disk or a standard Docker volume can behave inconsistently if `--base` is ever backed by an NFS mount. Not a concern for the deployment described in §5.1 (a plain host-directory volume mount), but worth re-checking if that assumption ever changes.
*   **Settings are fixed at process start.** `--workers`, `--exts`, and every other flag are read once at launch and never revisited — there is no live-reload concept in the engine itself. (This is already inherently true given the engine is a plain CLI process; the operational rule that a running job ignores subsequent settings changes lives at the Phase 2 orchestration layer — see `phase2-spec.md` §3 — not here.)

### 4.2. Processing Logic
*   **Deduplication:**
    *   **Exact Match:** Files with identical SHA1 hashes (excluding the file's own row, and excluding other rows already flagged `Duplicate`/`Removed_Duplicate`, to prevent a duplicate pair from cascading into mutually flagging each other across repeated scans) are flagged `status = 'Duplicate'`.
    *   **Duplicate removal (`--move` only):** After all targeted `Pending` files are processed, the engine looks up each `Duplicate`-flagged file's matching `Completed` row. Only if a verified copy is confirmed present on disk at that row's `dest_path` is the duplicate's source file deleted (status becomes `Removed_Duplicate`). If no verified copy is found, the source file is left in place and a warning is logged — this prevents data loss in the case where the "kept" copy's own migration failed. Skipped entirely if the run was cancelled (see below).
    *   **Fuzzy Match:** pHash is generated and stored for every file, but no fuzzy-matching/clustering logic acts on it yet — that's Phase 3 scope (see `phase3-spec.md`).
*   **Metadata Extraction:** for every file, the engine captures BOTH "date taken" (used to compute the destination folder) AND the full metadata set available (camera make/model, ISO, aperture, shutter speed, and whatever else the source exposes), from the same underlying capture:
    1.  **ExifTool** (persistent per-worker process via `PyExifTool`'s `-stay_open` mode — one instance started per `ProcessPoolExecutor` worker and reused for every file that worker handles, not a fresh subprocess per file) — the primary and now-guaranteed-available source, since the engine is a hard-requirement (§3.3) and won't start without it. Captures the COMPLETE tag set ExifTool can extract, not a curated subset. The only method that works for RAW-family formats. Measured directly: switching from subprocess-per-file to the persistent process cut the ExifTool-call overhead specifically from ~74ms/file to ~2.5ms/file — roughly 30x — which compounds significantly across a large library.
    2.  **PIL** (`Image.getexif()`, plus the "Exif" sub-IFD via `get_ifd(0x8769)`) — a defensive per-*file* fallback, not a "what if ExifTool isn't installed" fallback anymore (that case can no longer happen, since §3.3's startup check refuses to run without it). Used only if ExifTool genuinely ran but returned nothing usable for one specific file. Works for standard/HEIC formats; cannot open RAW-family formats at all. Note: `getexif()` alone only returns the top-level "0th" IFD (Make/Model); `DateTimeOriginal`/ISO/aperture/shutter live in the separate Exif sub-IFD and must be explicitly fetched, or they silently go missing even when present.
    3.  **File modification time** — used only if neither of the above produces a usable date. No richer metadata is captured at this fallback level.

    **ExifTool being a hard requirement does not mean Pillow, rawpy, and imagehash became optional or got removed.** They do a fundamentally different job ExifTool cannot do at all: ExifTool reads embedded metadata tags, it does not decode pixel data. pHash generation and thumbnail generation both require actually decoding the image (PIL for standard/HEIC, rawpy for RAW-family) and feeding real pixel data to `imagehash.phash()` — ExifTool's ability to extract an already-embedded camera preview (where one exists) doesn't change that, since `imagehash` still needs that extracted preview decoded through PIL to hash it. See §3.3 for the full library breakdown.

    **Self-healing:** if a worker's persistent ExifTool subprocess dies mid-batch (crash, killed externally), the next file processed by that worker detects the failure and transparently restarts it — tested directly by killing the underlying subprocess and confirming the very next query recovered successfully, without losing ExifTool capability for the rest of that worker's remaining files.
*   **Unique Filename Enforcement:** Before writing to a computed destination path, the engine checks for an existing file at that path and appends a numeric suffix (`_1`, `_2`, ...) until a free name is found. Recorded via `has_name_collision` (see §6).
*   **Safety Protocols:**
    *   **Copy-Verify-Delete / Copy-Verify:** The file is copied to a temporary `<filename><ext>.organizing.partial` file in the destination directory, its SHA1 is recomputed and compared against the source's hash, and only on a match is the partial file atomically renamed to its final destination path. The source is deleted afterward only in `--move` mode. The real error text (not just pass/fail) is captured and returned on any failure — see §6's `operations` table.
    *   **Retry with backoff:** Transient IO errors (e.g., flaky network shares) during copy/rename/delete operations are retried up to 3 times with exponential backoff (1s, 2s) before the operation is considered failed. (This is unrelated to the "no retry mechanism" decision in §4.1 — that refers to re-attempting a whole failed *file* across separate runs, not this in-process backoff for transient IO errors during a single attempt.)
    *   **Pre-flight disk space check:** Before `--move`/`--copy` begins, the engine sums the size of all targeted `Pending` files and confirms the destination volume has enough free space (plus a 500MB safety margin), aborting before any file operations start if not.
    *   **Orphan Cleanup:** On startup, any leftover `.organizing.partial` files from a previous interrupted run are detected and removed.
    *   **Crash Recovery — file level:** On startup, any `photos` row still marked `Processing` (meaning the process died mid-operation) is reconciled: if the destination file exists and the source no longer does, it's marked `Completed`; otherwise it's reset to `Pending` so the next run retries it.
    *   **Crash Recovery — run level:** On startup, any `runs` row still marked `Running` (meaning that process was killed uncatchably — `SIGKILL`, OOM-kill, power loss — bypassing the normal shutdown path) is marked `Crashed` with a real end timestamp, rather than being left showing as perpetually in-progress forever.
    *   **Re-scan safety:** Re-running the engine against a source directory that still contains previously-cataloged files (the normal Index → review → `--move` workflow) updates existing database rows in place (`INSERT ... ON CONFLICT(source_path) DO UPDATE`) rather than failing on a duplicate-key error.
    *   **Cancellation:** Checked between files, never mid-file — the in-flight file always finishes its Copy-Verify(-Delete) before the loop stops. Every remaining targeted file that never got a chance to run is logged to `operations` with `status = 'Cancelled'`, while its `photos.status` stays `Pending` (not overwritten), so a plain re-run picks it back up naturally. Duplicate-source cleanup is skipped entirely for a cancelled run, since it depends on knowing the final fate of every targeted `Pending` file first.
    *   **Source file changed since Index:** If a targeted file's `source_path` no longer exists on disk when the engine actually tries to process it (moved, renamed, or deleted outside NegativeSpace since the last Index — most likely with `--file-ids`/`--source-subdir` targeting a stale selection), this is detected explicitly *before* attempting to open the file, rather than surfacing as a raw `FileNotFoundError` traceback. Recorded as `status = 'Failed'` with a specific, human-readable `error_message`: `"Source file changed: no longer found at <path>. It may have been moved, renamed, or deleted outside NegativeSpace since the last Index."` The distinct wording matters for the Error Center (`phase2-spec.md` §5.3) — a user seeing this should understand to re-index, not assume a permissions or disk problem.
    *   **ExifTool startup check:** Before touching the database or source/dest paths at all, the engine verifies both the `exiftool` binary and the `PyExifTool` package are available and exits immediately with a clear fatal error if either is missing (§3.3) — tested directly (binary hidden from `PATH`) and confirmed it fails cleanly with no directories or database files created.

### 4.3. Reporting & Feedback
*   **Logging:** Structured logs to both console and `<base>/logs/organizer.log`, covering every stage (scan discovery, hashing, metadata resolution, space checks, copy/verify/delete, duplicate cleanup, cancellation).
*   **Audit Trail:** The `runs` + `operations` tables (§6) together give a full history of every invocation and every per-file outcome within it — this is what "show previous run information" is built on, independent of the frontend.
*   **Real-time Feedback (Phase 2):** WebSocket-driven status updates in the web UI — see `phase2-spec.md`.

## 5. Technical Infrastructure
### 5.1. Deployment (Docker)
*   **Environment:** Containerized for consistency across dev/prod environments. `gosu`-based entrypoint maps the container process to the host user via `PUID`/`PGID`.
*   **Required system packages:** `libimage-exiftool-perl` (or platform equivalent) **must** be installed in the image — ExifTool is a hard requirement (§3.3/§4.2), not an optional extra. If it's missing, the container will still build and start, but every engine invocation will immediately exit with a fatal error rather than run in a degraded mode, since the old graceful-degradation-to-PIL behavior for a missing binary no longer applies. Worth an explicit check against the actual `Dockerfile` when adopting this change, since an older image built before this requirement may not have it installed.
*   **Recommended: a proper subreaper as PID 1** (e.g. `tini`/`dumb-init`), general Docker hygiene independent of this specific change — each worker process spawns and manages its own ExifTool child process, and while it's cleaned up on normal worker shutdown, a subreaper ensures nothing is ever left orphaned regardless of how a process exits.
*   **Volume Mapping:** `/data/source` (source photos), `/data/dest` (organized output), `/appdata` (SQLite DB + logs), all host-mapped.
*   **Database:** **SQLite**, opened in WAL mode with a 5-second busy timeout for safe concurrent access between the writer thread and any read-only inspection.

## 6. Data Schema (SQLite)

Three tables, each with a distinct role — this replaced an earlier, simpler single-table design once error tracking and run history needed somewhere to live (see the design note below).

### 6.1. `photos` — current state, one row per source file
| Field | Type | Description |
| :--- | :--- | :--- |
| `id` | Integer | Primary Key, autoincrement. This is what `--file-ids` targets. |
| `source_path` | Text | Original file path. **Unique** — the re-scan upsert logic (§4.2) depends on this constraint. |
| `dest_path` | Text | Target path (including auto-generated suffixes) |
| `sha1_hash` | Text | Exact content hash |
| `phash` | Text | Perceptual hash. `"not_supported"` if the required optional library isn't installed for that format; `"error"` if hashing was attempted but failed (e.g. corrupt file). |
| `collision_group` | Integer | Reserved for Phase 3 fuzzy-match clustering. Not populated yet. |
| `is_master` | Boolean | Reserved for Phase 3 collision resolution. Not populated yet. |
| `status` | String | `Pending`, `Processing`, `Completed`, `Failed`, `Duplicate`, `Removed_Duplicate`, `Copied` |
| `metadata_json` | JSON | Full captured metadata (camera, ISO, aperture, shutter, etc. — whatever the source/method exposed), always including a `date_taken` key. |
| `has_name_collision` | Boolean | Whether the destination filename had to be suffixed (`_1`, `_2`, ...) to avoid overwriting an existing file. |

### 6.2. `runs` — one row per engine invocation
| Field | Type | Description |
| :--- | :--- | :--- |
| `id` | Integer | Primary Key, autoincrement |
| `mode` | Text | `INDEX`, `MOVE`, or `COPY` |
| `source_path` / `dest_path` | Text | As passed to this invocation |
| `file_ids_filter` | Text | JSON array of targeted IDs, or NULL for a full directory scan |
| `started_at` / `ended_at` | Text | ISO timestamps |
| `status` | Text | `Running`, `Completed`, `Cancelled`, `Failed`, `Crashed` |

### 6.3. `operations` — append-only audit log, one row per file per run
| Field | Type | Description |
| :--- | :--- | :--- |
| `id` | Integer | Primary Key, autoincrement |
| `run_id` | Integer | FK to `runs.id` |
| `photo_id` | Integer | FK to `photos.id` |
| `original_filename` | Text | The file's name at time of processing |
| `source_path` / `dest_path` | Text | As of this specific operation |
| `status` | Text | The outcome of this specific attempt — same value set as `photos.status`, plus `Cancelled` |
| `error_message` | Text | The real exception text on failure, e.g. `"OSError: [Errno 30] Read-only file system: ..."` — not just a generic failure flag |
| `has_name_collision` | Boolean | As of this specific operation |
| `timestamp` | Text | ISO timestamp |

**Design note — why three tables instead of columns on `photos`:** `photos` answers "what's the current state of this file?" — a single `error_message` column there could only ever hold the *most recent* attempt's outcome, and couldn't show that a file failed twice with different errors before eventually succeeding, or answer "show me everything that happened in run #47." Splitting current-state (`photos`) from historical audit log (`operations`, joined to `runs` for run-level context) answers both without overloading one table with two different jobs.

## 7. Non-Functional Requirements
*   **Concurrency (within a run):** `ProcessPoolExecutor` parallelizes hashing/metadata-resolution across CPU cores; sized to `os.cpu_count()` or overridden via `--workers`.
*   **Single-Instance Guarantee (across runs, implemented):** At most one engine process may run at a time, enforced by the OS-level lock in §4.1 — this is what makes the "Thread Safety" guarantee below actually hold in practice once Phase 2 allows multiple UI actions to attempt to trigger the engine. Without it, two concurrent processes (each internally thread-safe on its own) could still race each other at the filesystem/database level — e.g. two processes independently computing the same "available" destination filename via a check-then-write sequence with no cross-process coordination, where the second process's write can silently overwrite the first's already-verified file, since the final rename step overwrites an existing target rather than erroring. The lock closes this off entirely by making the "two processes running at once" precondition impossible, rather than trying to make that scenario itself safe.
*   **Thread Safety:** SQLite is written to by exactly one dedicated consumer thread; all other work happens in separate processes that communicate results back through an in-memory queue, never by opening the database themselves.
*   **Durability:** The engine can recover from both a graceful interruption and a hard crash — orphaned partial files are cleaned up, interrupted `Processing` file-records are reconciled to their correct state, and orphaned `Running` run-records are marked `Crashed`, all on the next startup.
*   **Safety:** No file is deleted from source until a byte-for-byte verified copy exists at the destination. This holds for both direct moves and duplicate-source cleanup, and is never bypassed by cancellation — a cancelled run simply stops starting new work, it never skips verification on work already in flight.
