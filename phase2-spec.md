# Functional & Technical Design Specification: NegativeSpace Web Interface

## 1. System Overview & Architecture

The NegativeSpace Web Interface provides a modern web UI for the containerized Python Phase 1 engine (`ns-engine.py`). It transforms the CLI engine into an interactive application supporting real-time operation monitoring, selective file processing, context-aware duplicate resolution, detailed metadata inspection, dedicated runtime settings management, extension validation, and audit logging.

**The web UI is the interface.** As of Phase 2 the engine's command-line flags are an *internal* calling convention between FastAPI and the engine — not a supported end-user surface. Users interact with NegativeSpace through the web UI; nothing in the user-facing documentation should direct them to invoke `ns-engine.py` by hand.

The flags are deliberately **not** hidden (no `argparse.SUPPRESS`), and the engine reference documentation stays in the repository. Anyone cloning the project to understand, debug, or extend it benefits from being able to run the engine directly, and hiding the flags would buy nothing — anyone who can execute the engine can read its source. The distinction is *documented for users* versus *available to developers*, not *present* versus *absent*.

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
|   (ns-engine.py --workers N --exts ex1,ex2 --file-ids id1,id2               |
|                        --source-subdir path)                                      |
+-----------------------------------------------------------------------------------+
```


---

## 2. Core Operational Requirements & Workflows

### Operational Flow

```
1. User triggers an Index scan (full directory or, on a repeat visit,
   just a "Rescan" to pick up newly added files).
   -> FastAPI checks for an already-active job (409 if one exists, §5.7)
   -> FastAPI spawns: python3 ns-engine.py
   -> Engine scans the full source directory, hashes everything, flags
      duplicates, captures metadata, and populates SQLite.

2. Frontend queries the catalog (paginated/filterable) and renders it
   as a browsable, selectable grid (Gallery view).

3. User selects one or more files (or a folder, for large batches — see
   §2's Selective File Processing) and chooses an operation: Move or Copy.

4. Frontend POSTs the selection + operation to FastAPI
   (`POST /api/v1/jobs/start`, see §6.2).

5. FastAPI re-checks for an active job (409 if one exists), then spawns
   the engine, scoped one of two ways:
   python3 ns-engine.py --move --file-ids 101,102,105  (or --copy)
   python3 ns-engine.py --move --source-subdir sd_card/day1

6. Job progress streams back to the frontend via WebSocket, reusing the
   engine's own status transitions (Pending -> Processing ->
   Completed/Failed/Removed_Duplicate) rather than a separate progress
   protocol. Any client connecting or reconnecting first replays
   `GET /api/v1/runs/{run_id}/operations` before tailing live (§4.1).

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

**No source file is ever deleted on the catalog's word alone.** All three places the engine removes a user file — the verified copy in Move, an already-delivered source it finds at the destination, and a duplicate source during cleanup — hash *both* files live at the moment of the decision. Stored hashes identify a candidate; they never authorize a deletion. A UI must not offer to relax this, and it needs no separate "verify before deleting" option, because there is no path that skips the check.

One consequence for display: after a Move, each `Duplicate` row's `dest_path` is repointed at the location recording its content, and that step reads no files by design. A duplicate's recorded destination therefore means *where its content is recorded*, not *confirmed present and matching*. Do not present it as verified — the guarantee lives at the moment of deletion, not in the column.
* **Copy Mode (`--copy`):** Non-destructive. Performs verified copy to destination while leaving source files untouched.

### Selective File Processing
Users can select individual files or multiple files across grid views to run targeted operations.
* **Multi-Select Controls:** Checkboxes on photo cards, Shift-click range selections, and "Select all on page."
* **Selection size limit:** Individual multi-select (including "Select all on page") is capped at a configurable maximum (default: 1,000 files) per job submission — this isn't an arbitrary UX restriction, it's because each selected file becomes an integer in the `--file-ids` command-line argument passed to the engine, and there's a real OS limit on total command-line length. Exceeding the cap shows a clear message (e.g. *"1,000 file limit for individual selection — try Folder Selection below for larger batches"*) rather than silently truncating the selection or attempting a job that might fail at spawn time.
* **Folder Selection (for large batches):** Instead of "select all matching current filter" against individual files, users can select a source folder (recursive) and scope the operation to everything currently indexed under it. This maps directly to the engine's `--source-subdir <path>` flag (`project-spec.md` §4.1) rather than enumerating individual IDs, which sidesteps the command-line length limit entirely — there's no practical upper bound on how many files a folder selection can cover. Symlinks are excluded automatically, inherited from the original Index that populated the catalog (a symlink was never indexed as a row in the first place). If a folder hasn't been indexed yet (zero matching rows), show *"No indexed files found under this folder — run an Index first."*
* **Sticky Action Bar:** Appears when items (individual or folder) are selected, presenting **Move Selected** and **Copy Selected** actions.
* **Targeted Execution:** Individual selections use the `--file-ids <id1,id2>` flag; folder selections use `--source-subdir <path>`. These are mutually exclusive targeting mechanisms in a single job — pick one per submission. IDs (not raw file paths) were chosen for the individual case specifically because a database primary key is unambiguous and doesn't depend on path strings staying identical between when the frontend fetched the catalog and when the operation actually runs — and it keeps one targeting implementation rather than a parallel web-only code path, which is what makes the engine directly runnable for debugging and development (see §1).

---

## 3. Dedicated Settings Management (`/settings`)

A dedicated Settings view provides central management of engine parameters, persisted to SQLite and passed to engine instances on startup.

**Settings changes never affect an already-running operation.** Every engine invocation reads its configuration once, at spawn time, as CLI flags (`--workers`, `--exts`) — there's no live-reload path, by design (see `project-spec.md` §4.1). Saving new settings in this panel only affects jobs started *after* the save. If a user wants a change applied to work that's currently in progress, they need to cancel the running job (§4.1's Cancel Job) and start it again — at which point the new settings apply from that fresh invocation. The Settings UI should make this explicit (e.g. a note near Save: *"Changes apply to new operations only — cancel and restart an in-progress job to apply immediately"*) rather than implying a change takes effect instantly everywhere.

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
* **Job Control:** Provides a **Cancel Job** button. Sends `SIGTERM` to the engine subprocess (§6.2 `jobs/{id}/cancel`). During the **Index/scan** phase the engine stops at the next batch boundary and skips the move/copy phase entirely (everything already indexed is kept, so re-running continues where it left off) — note the UI should not expect per-file `Cancelled` rows for a scan-phase cancellation, since no physical work was scoped out yet. During **Move/Copy**, the file currently being copy-verified finishes normally, then every remaining targeted file is logged to the `operations` audit table with status `Cancelled` (not silently dropped — visible in the run's history afterward) and duplicate-source cleanup for that run is skipped entirely.
* **WebSocket Reconnection & Replay:** On connecting (or reconnecting after a dropped connection or browser refresh — see §5.2), the frontend does **not** assume it saw every event live. It subscribes to the run's live WebSocket stream **first**, buffering events without displaying them. It then queries `GET /api/v1/runs/{run_id}/operations` (backed by `SELECT * FROM operations WHERE run_id = ? ORDER BY id`) to backfill the LOGS panel, merges the buffered events in, discarding any whose operation `id` the backfill already holds, and only then displays the live stream. The order matters: fetching history and then subscribing leaves a window in which an operation lands after the query but before the subscription, and is never shown. So every live event carries its `operations.id`, and ordering is by `id`, not `timestamp` — the single writer assigns ids in commit order, while two operations can share a timestamp. This is what makes "job continues unaffected by a browser refresh" (§5.2) actually true for the *displayed history*, not just the underlying job — without this replay step, a reconnecting client would see progress resume from wherever it currently is with an empty-looking log, even though the job had been running for a while.

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
Jobs run asynchronously in FastAPI. If a user closes or refreshes their browser, the job continues unaffected. Reopening the web UI re-establishes the WebSocket connection and replays the operations log for that run (§4.1's WebSocket Reconnection & Replay) to stream live progress with full history intact, not just progress from the reconnection point forward.

### 5.3 Error Center
If operations fail, an Error Banner highlights the failures, sourced directly from the `operations` log's `error_message` column (see §6.1) — the real exception text is persisted, not just a generic "failed" flag.

**Failures belong to attempts, not to a photo's current status.** Query `operations.status = 'Failed'`, joining the photo and run for context; do not filter on `photos.status`. The two deliberately disagree in at least one case: when duplicate cleanup cannot verify that a destination copy still matches the source, it leaves the photo `Duplicate` — correct, since the source is intact and still a duplicate — while recording a `Failed` operation explaining why the deletion did not happen. An Error Center filtering on `photos.status` would show that photo as an ordinary duplicate and never surface the failure, which is the invisibility the recorded operation exists to end.

Some failures have no photo at all. A folder the scan could not read is recorded as a `Failed` operation with `photo_id` NULL and the folder as `source_path` — the photos inside it were never examined, so there is no catalog row to attach to. Left-join `photos` (an inner join drops these), and present such a row as a folder the user needs to fix permissions on, not as a file.

**`Skipped` is an outcome, not a failure.** A run records `Skipped` for a selected photo it deliberately left alone — today, a duplicate whose original carries its content — with a reason naming that original (`Duplicate of photo #N ...`). Show these as informational, grouped apart from failures, and link the named original: a user who selected only the duplicate needs to know which photo to select instead. They exist so that every photo in a selection ends the job with a recorded outcome; a job whose selection held only duplicates used to finish green with nothing recorded at all.

For that case specifically, the recorded `error_message` reads `Duplicate verification failed: ...`, and the underlying cause is worth distinguishing in the UI: a `ChecksumMismatch` means the two files' contents differ, while an `OSError` means one of them could not be read and the comparison never happened. Neither should be presented as "the destination is a verified backup", and neither should suggest deleting anything by hand.

```
+-----------------------------------------------------------------------------------+
| FAILED OPERATIONS (3 Items)                                                       |
+-----------------------------------------------------------------------------------+
| 1. IMG_0488.CR2                                                                    |
|    Source: /data/source/imports/IMG_0488.CR2                                      |
|    Error:  PermissionError: [Errno 13] Permission denied: '/data/dest/...'        |
|                                                                                   |
| 2. IMG_0912.JPG                                                                   |
|    Source: /data/source/corrupted/IMG_0912.JPG                                   |
|    Error:  ChecksumMismatch: SHA-1 verification failed                            |
|                                                                                   |
| 3. IMG_1050.JPG                                                                   |
|    Source: /data/source/sd_card/IMG_1050.JPG                                     |
|    Error:  Source file changed: no longer found at /data/source/sd_card/         |
|            IMG_1050.JPG. It may have been moved, renamed, or deleted outside      |
|            NegativeSpace since the last Index.                                       |
+-----------------------------------------------------------------------------------+
```

Users can view exact system error strings (e.g., `PermissionError`, `ChecksumMismatch`, `Source file changed`). The distinct wording on the third case (`project-spec.md` §4.2) is intentional — it should read differently from a permissions/disk failure, since the fix is "run an Index" rather than "check destination permissions."

**No dedicated retry subsystem.** There is no "Retry Item" / "Retry All Failed" backend endpoint and no `retry_count` tracking. A failed file's `photos.status` is reset to `Pending` automatically the next time it's re-indexed (a plain re-scan, full or `--file-ids`-scoped), so retrying is just re-running the same operation — files that already succeeded are gone from `--source` and won't be touched again, so this is fast even for a large batch with only a few failures. The web UI's equivalent of "retry" is selecting the photos associated with failed attempts and re-issuing the same Move/Copy operation via `POST /api/v1/jobs/start` with their IDs in `file_ids` — no new endpoint required. Take those IDs from the failed `operations` rows rather than from `photos.status`, deduplicating when several attempts reference one photo, and do not require the photo's current status to be `Failed`: a duplicate-verification failure stays `Duplicate` and is retried by Move's duplicate cleanup on the next run. Retrying does not by itself fix a content mismatch or an unreadable file, so the UI should not promise that it will.

### 5.4 Operations Audit Log (`/logs`)
A searchable table logging every operation performed by the engine:
* **Columns:** Timestamp, Mode (`MOVE`/`COPY`), Source Path, Destination Path, Status (`Completed`, `Copied`, `Removed_Duplicate`, `Failed`), and System Error Message.
* **Controls:** Filter by date, status, or free-text search; CSV/JSON export.

### 5.5 Job Outcome Is Derived, Not Read From `runs.status`

**`runs.status` describes the run's lifecycle, not whether the work succeeded.** A run that reaches the end of its file loop is recorded `Completed` even if every single file in it failed. That is accurate for what the column means — the process ran to completion rather than crashing, being cancelled, or aborting on a pre-flight check — but it is the wrong thing to put in front of a user on its own.

Observed in practice: a `--move` against a source mounted `:ro` fails every file (the copy succeeds, only the source deletion fails) and still reports:

```
Run #2 finished with status: Completed
```

Surfacing that verbatim would show a green **Completed** for a job where nothing succeeded, and the user would have to open the Error Center to discover their entire operation did nothing.

**The API therefore derives a job outcome from the `operations` rows rather than echoing `runs.status`:**

```sql
SELECT status, COUNT(*) FROM operations WHERE run_id = ? GROUP BY status;
```

The per-file truth is already recorded there — `Completed`, `Copied`, `Failed`, `Cancelled`, `Removed_Duplicate`, each with an `error_message` where applicable — so no engine change and no new status vocabulary is required. `runs.status` keeps its current meaning and remains the right thing to check for "is a job still running" (§5.7) and for crash reconciliation.

Job responses should carry both: the lifecycle status **and** the derived counts, so the UI can render *"Move finished — 0 of 23 succeeded, 23 failed"* rather than a bare word. Recommended presentation rules:

| condition | display |
|---|---|
| `runs.status` is `Running` | in progress, with live counts |
| succeeded > 0, failed = 0 | success |
| succeeded > 0, failed > 0 | partial success — surface the failed count and link the Error Center |
| succeeded = 0, failed > 0 | **failure**, regardless of `runs.status` being `Completed` |
| `runs.status` is `Cancelled` / `Crashed` / `Failed` | that status wins; still show counts for what was done before it ended |

Note the engine may write more `operations` rows than the job targeted — the scan phase logs a row per file and the move phase logs another, and a `--move` additionally logs `Removed_Duplicate` cleanup rows. Count against the operation the user asked for rather than assuming one row per file.

---

### 5.6 Engine Invocation as a Trust Boundary

Because the engine's flags are now assembled by FastAPI from HTTP request bodies rather than typed by someone with shell access, argument construction is a **security boundary**, not a convenience. Three rules follow:

* **Build the command as an argument list, never a shell string.** Use `subprocess.Popen([...])` / `subprocess.run([...])` without `shell=True`. Interpolating a user-supplied `source_subdir` into a shell command would be command injection reachable directly from an HTTP request — this is the single most damaging mistake available in this layer.
* **`--source-subdir` carries user-chosen input** from the folder picker and is the most exposed parameter. The engine already resolves it and rejects anything escaping `--source` via `..` — that check is load-bearing under Phase 2 and must not be removed as a redundant-looking sanity check. FastAPI should validate independently rather than relying solely on the engine; defense in depth is the point, and the API can return a clean `400` instead of a failed job.
* **`--exts` is the subject of the validation feature in §3.1.** The engine normalizes the leading dot and casing but does not otherwise constrain the value, so the API owns deciding which extensions are acceptable. Scope is limited to the mounted source directory, so the risk is indexing unintended file types rather than reading outside the volume — but a user-facing field still needs a server-side allowlist, not just client-side checks.

Note also the `--file-ids` length ceiling described in §2: the 1,000-item selection cap is a real OS command-line limit, and enforcing it is the API's responsibility. Folder selections use `--source-subdir` precisely to sidestep it.

---

### 5.7 Single Active Job Enforcement

Only one engine process may run at a time — see `project-spec.md` §4.1/§7 for the engine-level guarantee (an OS-level `flock`, held for the whole process lifetime, released automatically even on a hard `SIGKILL`). This is enforced in two layers, not one:

* **Fast pre-check (FastAPI):** Before spawning the engine, `POST /api/v1/jobs/start` probes the engine's own lock: a non-blocking `flock` on `<base>/engine.lock`. If the lock is held, an engine is running, and it returns `409 Conflict` immediately — no subprocess is spawned, and the response includes the newest `Running` run's `id`, `mode`, and `started_at` so the frontend can show *"A Move operation is already in progress (started 2 minutes ago) — wait for it to finish or cancel it."* The Rescan/Move/Copy/Settings-Save buttons should all be disabled client-side whenever a job is known to be active, so this 409 is a backstop for races (e.g. two tabs), not the primary UX.

  The pre-check must not decide from `runs.status = 'Running'` alone. A row orphaned by a crash stays `Running` until the next engine run reconciles it, so a pre-check that trusted the table would refuse to start that very run: every job blocked, permanently, by a process that no longer exists. If the probe *acquires* the lock, any `Running` rows are stale; settle them as in the restart case below before releasing the probe and spawning. Two requests can still race between the probe's release and the engine's own acquisition. The engine's lock decides, and the losing engine exits non-zero with its FATAL message, which the API reports as a 409.
* **Authoritative guarantee (engine):** The `flock` in `project-spec.md` §4.1 is what actually prevents data corruption if the fast check above is ever wrong or stale — see the FastAPI-restart case below. Even if FastAPI's own bookkeeping says "nothing running" incorrectly, a second engine process attempting to start will still be refused by the lock and exit cleanly with a logged error, never silently racing a real in-progress run.

**FastAPI-restart edge case:** if FastAPI itself restarts (redeploy, crash) while a job is running, its in-memory job/WebSocket-subscriber state is lost, but the engine subprocess is *not* killed by its parent dying — it keeps running under the protection of its own lock. On startup, FastAPI should reconcile this by querying `runs` for any `status = 'Running'` row. Two cases:
1. **The engine process is genuinely still alive** (the common case) — FastAPI should treat this as an active job for UI purposes (allow reconnecting clients to replay/stream it per §4.1) without being able to directly re-attach to the subprocess's stdout; the `operations` log is what makes this possible without that direct attachment.
2. **The engine process crashed too, before its own next-run reconciliation ever got a chance to mark that row `Crashed`** (`project-spec.md` §4.2) — this is a double-failure case (both the engine and FastAPI went down around the same time) that would otherwise leave a phantom `Running` row until someone happens to run the engine again. FastAPI can distinguish the two cases on its own startup by attempting a **non-blocking `flock` on the same lock file as a liveness probe** — if it succeeds (nothing holds the lock), no engine process owns any `Running` row. **While still holding the probe lock**, FastAPI records the IDs of the `Running` rows it found, marks exactly those `Crashed`, and only then releases the probe, rather than waiting for a future engine invocation to notice. Releasing first would let a new engine start in between and create its own `Running` row, which a blanket `UPDATE ... WHERE status = 'Running'` would then mark `Crashed` while it runs.

### 5.8 Index Is a Precondition for Move and Copy

Move and Copy act on the catalog, never on the filesystem directly. Both targeting mechanisms — `--file-ids` and `--source-subdir` — resolve rows a previous Index recorded; neither walks the source tree. A Move or Copy issued against a source that has never been indexed therefore matches zero rows and does nothing.

The engine reports this rather than hiding it: each targeting mode logs a warning naming the cause and the remedy when it matches nothing. But the engine can only explain the situation *after* the user has already waited for a job that was never going to do anything, and it deliberately does not treat an empty selection as an error — "nothing left to do" is the correct outcome for a re-run, and failing would break idempotency.

Preventing the situation is the UI's job:

* **The folder picker can only offer indexed folders.** Its tree should be built from `SELECT DISTINCT` over indexed `source_path` prefixes, not from a filesystem listing. A folder the user cannot select is a folder they cannot mis-target. This also matches what the user is choosing between — they are picking from photos the app knows about, not browsing a disk.
* **Move and Copy are disabled while the catalog is empty**, with the control labelled to say why (*"Run a Scan first — NegativeSpace acts on indexed photos"*) rather than being inert with no explanation. `SELECT COUNT(*) FROM photos` is sufficient to drive this.
* **A newly added source directory is not silently actionable.** A source the user has just configured has no rows until a Scan completes over it. The Settings flow that adds a source should offer to run that Scan immediately, so the common path never produces an un-indexed source.
* **Stale scope is surfaced, not assumed.** `--source-subdir` sees only rows as of the last Index over that path, so files added to a folder since then are invisible to a Move or Copy targeting it. Where the UI shows a folder's file count, it should show when that folder was last scanned alongside it, so a user comparing "1,318 photos" against what they see in their file manager can tell the difference between a bug and a stale index.

The general principle: the engine guarantees it will never act on something it has not catalogued, and says so when a selection resolves to nothing. The UI is responsible for making an empty selection hard to construct in the first place.

### 5.9 Duplicate Space: Reclaimable, Reclaimed, and Saved

"How much space are my duplicates wasting?" is a headline figure for the Dashboard, and the catalog already answers it without any engine change. Deduplication acts on two different volumes, though, and conflating them produces a number that is wrong in whichever direction the user's mode does not apply:

| Figure | Where | Realized by |
| :--- | :--- | :--- |
| **Reclaimable** — duplicate sources still on disk | Source | `--move` only |
| **Reclaimed** — duplicate sources already deleted | Source | Past `--move` runs |
| **Saved** — duplicate copies never written | Destination | `--move` *and* `--copy` |

**These are not addends.** A Move both deletes a duplicate source and declines to write it to the destination, so the same bytes appear under *Reclaimed* and under *Saved*. Summing them into one "total saved" double-counts every moved duplicate. Show them as separate figures, each labelled with the volume it refers to.

#### Reclaimable at the source

**The waste is every copy beyond the one that is kept, not the whole group.** Three copies of one photo waste two copies' worth of bytes; the third is the photo itself, which the user is keeping. Deduplication already encodes exactly that split: among rows sharing a `sha1_hash`, one is the anchor (`Pending`, or `Copied`/`Completed` once delivered) and every other is `Duplicate`. So the figure is a single aggregate over the rows that are *not* anchors:

```sql
SELECT COUNT(*)                  AS duplicate_files,
       COUNT(DISTINCT sha1_hash) AS duplicate_groups,
       COALESCE(SUM(file_size), 0) AS reclaimable_bytes
FROM photos WHERE status = 'Duplicate';
```

No `GROUP BY`, no "subtract one per group" arithmetic, and no risk of the off-by-one that counting whole groups invites. `idx_photos_status` backs it, so it stays a cheap query on a large catalog.

**Call it reclaimable, not wasted, and say what reclaims it.** A `Duplicate` row means the redundant source file is still on disk. Only `--move` deletes those (duplicate cleanup, after verifying a destination copy still matches live); `--copy` deliberately removes nothing and records `Skipped` for them, per §5.3. A Dashboard tile reading *"3,028 duplicate files across 1,510 photos — 6.4 GB reclaimable by Move"* is honest about both the number and the action that realizes it. Phrasing it as space the app will "save" invites the user to expect Copy to free it.

**Four things not to fold into the figure:**

* **`Removed_Duplicate` is already reclaimed**, not reclaimable. Those source files are gone, so they are the *Reclaimed* figure — history rather than an opportunity — and adding them here double-counts. They also count toward the destination saving below, which is a different volume, not a second helping of the same one.
* **No destination deletion is implied.** The engine never deletes anything under `--dest`. Redundancy that something outside the engine put there is reported, not resolved, and only in Phase 3 (`phase3-spec.md` §3). This figure covers source files the engine can remove; what deduplication saves at the destination is the separate figure below.
* **`Failed` rows are not duplicates.** A source that vanished outside NegativeSpace is marked `Failed` at the next full Index, which removes it from its duplicate group and lets a surviving copy be promoted to anchor. It therefore drops out of this figure automatically — correct, since deleting a file that no longer exists reclaims nothing.
* **Sizes are as of the last scan.** `file_size` is recorded by the Index that wrote the row (§6.1), so the total is as current as the catalog. Show it alongside the last scan time, as §5.8 asks of folder counts, so a stale figure reads as stale rather than as wrong.

#### Space saved at the destination

The figures above are about the source tree, which is why only Move realizes them. **Deduplication's benefit to `--copy` is entirely at the destination:** a duplicate is never written there, so the destination holds one copy of each distinct photo instead of N. That saving is real in both modes, and it is the *only* one a Copy user gets.

A duplicate has saved destination space once its content has actually been delivered — that is, once some other row in its group is `Copied` or `Completed`:

```sql
SELECT COUNT(*)                      AS copies_not_written,
       COALESCE(SUM(d.file_size), 0) AS bytes_not_written
FROM photos d
WHERE d.status IN ('Duplicate', 'Removed_Duplicate')
  AND EXISTS (SELECT 1 FROM photos a
              WHERE a.sha1_hash = d.sha1_hash AND a.id != d.id
                AND a.status IN ('Copied', 'Completed'));
```

Both statuses count, because neither was ever written to the destination: a `Duplicate` still sits in the source, a `Removed_Duplicate` has been deleted from it, and in both cases the destination holds one copy rather than two. The `EXISTS` clause is what makes this *saved* rather than *savable* — a duplicate whose original has not been delivered yet has saved nothing so far, and belongs in the reclaimable figure instead.

What this is not: re-running a Copy does not write files it already delivered (§2's content-aware skip), but that is idempotency, not deduplication. Those rows are the anchors themselves, and the query excludes them by construction. Redundancy that something outside the engine put in the destination is a different question again, answered by the Phase 3 inventory (`phase3-spec.md` §3), not here.

The Inspector's `duplicates` array (§6.2, `GET /api/v1/photos/{id}/inspect`) should carry each copy's `file_size` for the same reason, so a single photo's panel can show what removing its duplicates would reclaim.

---

## 6. Database Schema & API Specifications

### 6.1 SQLite Schema

Schema changes are versioned with SQLite's built-in `PRAGMA user_version`, but **there is no in-place upgrade path and none should be added**. A catalog recording a different version is refused at startup with instructions to delete and rebuild, rather than migrated. Migration code runs rarely, on real user data, along a path that is almost never exercised; the engine previously carried three migration branches and one had a latent bug that survived until someone read it closely.

**Only `photos` is derived. `runs` and `operations` are not, and rebuilding discards them.** Every value in `photos` is recomputable by re-running an Index over the same sources — verified by rebuilding a ~29,000-file catalog from scratch and getting identical per-status counts. Nothing recomputes the audit log: it records what the engine *did*, and re-scanning the filesystem cannot reconstruct it. The sharpest case is `Removed_Duplicate`, where after a `--move` that row is the only evidence the file ever existed — its source was deleted by design and its content survives only under the anchor's name.

The practical consequence for the UI: rebuilding is cheap and safe for a catalog that has only been Indexed or Copied, and lossy for one that has been Moved against. Before offering a rebuild, check whether any `Removed_Duplicate` rows exist and say what will be lost. Offer a backup first — `sqlite3 <db> ".backup '<path>'"` is atomic under WAL where a file copy is not — and treat a JSON export of `runs` and `operations` as the format for reading history outside the app or carrying it across a schema change, not as a substitute for the database backup.

**Status values are enforced by the database, not by convention.** Each `status` column carries a `CHECK` constraint listing exactly its vocabulary, generated from the same tuples the engine uses. An API write of `'copied'` or a filter on `'Complete'` fails loudly at write time rather than silently disagreeing with the engine — a mismatch whose only symptom would otherwise be photos that never appear. Treat the constraint as the contract and do not hardcode a parallel list; read it from the engine's constants or from `sqlite_master` if the API needs to enumerate.

**The API layer should not create or alter the schema.** It opens a database the engine owns. It should read `PRAGMA user_version` on startup and refuse to serve if it does not match the version it was built against, surfacing "run a Scan to rebuild the catalog" rather than querying a shape it does not understand. Two writers disagreeing about schema on the same file is exactly what the single-instance lock exists to prevent.

Note the asymmetry this creates for the UI: deleting the catalog is cheap for Index state, but it discards the record of which files a previous Move already migrated. Where the UI offers a rebuild, it should say so.

The schema below reflects what's actually implemented in `ns-engine.py`, not a set of `ALTER TABLE` additions on top of the original `photos` table. Error tracking, name-collision flags, and original filenames live in a dedicated **audit log table** rather than as columns bolted onto `photos` — see the design note below for why.

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
    file_size INTEGER,            -- size/mtime as of the scan that wrote this
    file_mtime REAL,              -- row; the unchanged-file skip compares
                                   -- against these instead of re-reading
    CHECK (status IS NULL OR status IN ('Pending', 'Processing', 'Completed',
           'Copied', 'Failed', 'Duplicate', 'Removed_Duplicate'))
);
-- NOT YET PRESENT: thumbnail_path TEXT. Phase 2 adds it (see §4.2.1); the
-- block above is the schema as the engine creates it today. Adding it is a
-- schema change, which means a version bump and a rebuilt catalog, not an
-- ALTER on a live database.

-- Required, not optional. The engine's per-file duplicate check runs once for
-- EVERY file scanned; without idx_photos_sha1 it degrades to a full scan of a
-- table that is itself growing with every file (quadratic over library size).
-- idx_operations_run is what the per-job history view (§5.4) pages over, and
-- idx_operations_photo the per-photo panel — operations is append-only and
-- grows with files x runs. idx_photos_phash is what Phase 3's match gallery
-- groups on; idx_photos_source_stat covers the unchanged-file skip.
-- idx_operations_sha1 answers "everything that happened to this content",
-- across duplicates and catalog rebuilds.
-- The engine recreates all seven on every startup with IF NOT EXISTS.
CREATE INDEX idx_photos_sha1 ON photos(sha1_hash);
CREATE INDEX idx_photos_status ON photos(status);
CREATE INDEX idx_photos_source_stat ON photos(source_path, file_size, file_mtime);
CREATE INDEX idx_photos_phash ON photos(phash);
CREATE INDEX idx_operations_run ON operations(run_id);
CREATE INDEX idx_operations_photo ON operations(photo_id);
CREATE INDEX idx_operations_sha1 ON operations(sha1_hash);

-- runs: one row per engine invocation (Index, Move, or Copy). This is
-- what "previous run information" (§5.4) is actually built from — no
-- separate run-history table needed beyond this.
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    source_path TEXT,
    dest_path TEXT,
    file_ids_filter TEXT,   -- Self-describing JSON object naming which
                            -- targeting mechanism scoped the run:
                            --   {"file_ids": [101, 102]}
                            --   {"source_subdir": "sd_card/day1"}
                            -- NULL for a full directory scan. Always the
                            -- object form; the column name predates
                            -- --source-subdir and is kept as-is.
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL,   -- Running, Completed, Cancelled, Failed, Crashed
    CHECK (status IN ('Running', 'Completed', 'Cancelled', 'Failed', 'Crashed'))
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
    sha1_hash TEXT,         -- the photo's content hash, read when the row is
                            -- written; NULL if the file could not be read.
                            -- photo_id is valid in one catalog only; this
                            -- links the same content across rebuilds.
    -- Same vocabulary as photos.status, plus Cancelled (reached but never
    -- started; the photo stays Pending) and Skipped (reached and deliberately
    -- left alone — a duplicate whose original carries its content — with the
    -- reason, naming that original, in error_message).
    CHECK (status IN ('Pending', 'Processing', 'Completed', 'Copied', 'Failed',
           'Duplicate', 'Removed_Duplicate', 'Cancelled', 'Skipped')),
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

Starts an engine execution job, automatically injecting active configuration parameters from /settings if not explicitly overridden. `file_ids` and `source_subdir` are mutually exclusive — provide one or neither (a full directory scan), never both. Returns `409 Conflict` if another job is already running (§5.7) instead of spawning a doomed subprocess.

    Body (individual selection):
    JSON

    {
      "mode": "move",
      "file_ids": [101, 102, 105]
    }

    Body (folder selection):
    JSON

    {
      "mode": "move",
      "source_subdir": "sd_card/day1"
    }

    409 Response (another job already active):
    JSON

    {
      "error": "job_already_running",
      "active_run": { "id": 47, "mode": "MOVE", "started_at": "2026-02-14T10:28:03Z" }
    }

GET /api/v1/runs/{run_id}/operations

Returns the full `operations` history for a run (`SELECT * FROM operations WHERE run_id = ? ORDER BY id`). Used for the reconnect replay in §4.1/§5.2 — always called *after* subscribing to the run's live WebSocket stream, with live events buffered and deduplicated by `id`, and not just after a detected disconnect, so the log is complete regardless of when the client first connected.

GET /api/v1/runs/{run_id}

Returns a run with its **derived** outcome (§5.5). `status` is the engine's lifecycle value and `outcome` is computed from the `operations` rows — never render `status` alone, since a run where every file failed still reports `Completed`.

    Response:
    JSON

    {
      "id": 47,
      "mode": "MOVE",
      "status": "Completed",
      "started_at": "2026-02-14T10:28:03Z",
      "ended_at": "2026-02-14T10:31:11Z",
      "targeting": { "source_subdir": "sd_card/day1" },
      "outcome": {
        "verdict": "failed",
        "succeeded": 0,
        "failed": 23,
        "cancelled": 0,
        "removed_duplicates": 0,
        "summary": "0 of 23 succeeded"
      }
    }

`verdict` is one of `success`, `partial`, `failed`, `cancelled`, `crashed`, `running`, resolved by the rules in §5.5. `targeting` echoes the decoded `runs.file_ids_filter` object (§6.1) so the UI can show what the job was scoped to.

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

GET /api/v1/operations?status=Failed

Fetches failed attempts for the Error Center (§5.3), filtering on `operations.status`. Returns the operation ID, photo ID (nullable), run ID, timestamp, source and destination paths, status, error message, and the associated photo's current status as a separate field — the two are not interchangeable, per §5.3. Left-join the photo so a missing row cannot hide a failure. Supports run and date filters with stable ordering for pagination.

GET /api/v1/stats/duplicates

Backs the Dashboard's duplicate-space tiles (§5.9). Three separate figures, each naming the volume it applies to: what Move could still reclaim from the source, what past Moves already reclaimed from it, and what was never written to the destination in either mode. They overlap by design — a moved duplicate appears in both `already_reclaimed` and `saved_at_destination` — so the API returns them separately and the UI must not total them. `last_indexed_at` comes from the most recent completed run, so the UI can label the figures' age rather than implying they are live.

    Response:
    JSON

    {
      "reclaimable_at_source": {
        "duplicate_files": 3028,
        "duplicate_groups": 1510,
        "bytes": 6871947673
      },
      "already_reclaimed_at_source": {
        "removed_duplicates": 12,
        "bytes": 41943040
      },
      "saved_at_destination": {
        "copies_not_written": 3040,
        "bytes": 6913890713
      },
      "last_indexed_at": "2026-02-14T10:30:00Z"
    }

`GET /api/v1/photos?status=Failed` remains available for filtering the catalog, but it is not the Error Center's data source: it misses any failure whose photo is not currently `Failed`. "Retrying" is selecting the associated photo IDs and calling `POST /api/v1/jobs/start` again with the same mode — no separate retry endpoint, per the design note in §5.3.

---

## 7. Explicitly Out of Scope

* **Fuzzy-match clustering, similarity grouping, EXIF editing/synchronization** — Phase 3 scope, see `phase3-spec.md`.
* **Multi-user auth/sessions** — not addressed in this spec. Add as a separate concern if the web UI needs to be exposed beyond a single trusted user on a local/private network.