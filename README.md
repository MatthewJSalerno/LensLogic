# NegativeSpace

A backend engine for organizing large photo collections based on EXIF metadata, hash verification, and transactional file moves.

## Docker Usage

### Build

```bash
docker build -t negativespace .
```

### Operations Summary

NegativeSpace has three mutually exclusive modes. `--move` and `--copy` cannot be combined — pick at most one:

| Mode | Flag | Source files | Destination |
| --- | --- | --- | --- |
| **Index** (default) | *(none)* | Untouched | Nothing written |
| **Move** | `--move` | Deleted after a verified copy lands at destination; confirmed exact duplicates are also removed from source | Files organized into `YYYY/MM/DD` |
| **Copy** | `--copy` | Never touched — fully non-destructive | Files organized into `YYYY/MM/DD` |

### Run (Index — Default)

Scan, extract metadata, hash every file (SHA1 + pHash), and catalog everything into SQLite — including flagging exact duplicates — without moving, copying, or deleting anything. Mount `/data/source` as read-only (`:ro`) for safety; Index never needs write access to it.

```bash
docker run --rm \
  -e PUID=$(id -u) -e PGID=$(id -g) \
  -v /path/to/your/photos:/data/source:ro \
  -v /path/to/organized:/data/dest \
  -v /path/to/appdata:/appdata \
  negativespace
```

### Run (Move — Copy-Verify-Delete)

Performs pre-flight disk space validation, copies files, verifies SHA1 checksums, and only then deletes originals from the source folder. Confirmed exact duplicates are also removed from source once a verified copy of their content exists at the destination. **Drop `:ro`** — this mode deletes from source, so the container needs write access to it.

```bash
docker run --rm \
  -e PUID=$(id -u) -e PGID=$(id -g) \
  -v /path/to/your/photos:/data/source \
  -v /path/to/organized:/data/dest \
  -v /path/to/appdata:/appdata \
  negativespace python3 ns-engine.py --move
```

### Run (Copy — Non-Destructive)

Same verified Copy-Verify step as Move, but the source file is never deleted or modified — nothing is ever removed from source, including duplicates. Because of this, `/data/source` can safely **stay `:ro`** even in this mode, unlike `--move`.

```bash
docker run --rm \
  -e PUID=$(id -u) -e PGID=$(id -g) \
  -v /path/to/your/photos:/data/source:ro \
  -v /path/to/organized:/data/dest \
  -v /path/to/appdata:/appdata \
  negativespace python3 ns-engine.py --copy
```

> **Note:** `--move` against a read-only-mounted source will not corrupt anything, but every file will report `status='Failed'` (the copy succeeds; only the source deletion fails) and repeated attempts will accumulate orphaned duplicate copies at the destination. Use `--copy` for read-only sources instead.

---

## Configuration & Notes

- **User Mapping:** `PUID`/`PGID` match the container process permissions to your host user, preventing root-owned output files.
- **Volume Layout:**
  - `/data/source`: Raw input directory containing photos.
  - `/data/dest`: Structured target directory organized by `YYYY/MM/DD` format.
  - `/appdata`: Dedicated application directory storing persistent data inside `/appdata/db` and log files inside `/appdata/logs`.
- **Persistence:** SQLite database (`photo_hashes.db`) stores SHA1 checksums, perceptual hashes, and status to prevent re-processing across multiple runs.
- **Date & Metadata Resolution:** ExifTool is a **hard requirement** — the engine won't start without it (both the `exiftool` binary and the `PyExifTool` Python package). It runs as a persistent process per worker rather than spawning a subprocess per file, cutting ExifTool overhead roughly 30x. PIL and file-modification-time remain as defensive per-file fallbacks for the rare case ExifTool itself fails on one specific file — see `ns-engine.py`'s module docstring for the full breakdown.
- **Single-Instance Lock:** Only one engine process may run against a given `--base` (i.e., a given `/appdata` mount) at a time — enforced via an OS-level `flock` on `/appdata/engine.lock`. This is released automatically by the kernel on any exit, including a `docker stop` that times out into `SIGKILL` — no manual deletion of the lock file is ever needed, even after a force-stopped container. **NFS caveat:** `flock` reliability is weaker over NFS depending on the server/client's `lockd`/`statd` setup — fine for local disk or a standard Docker volume backing `/appdata`, but worth a second look if that mount is ever NFS-backed instead.
- **Supported Formats:** `.jpg`, `.jpeg`, `.png`, `.heic`, `.tiff`, `.raw`, `.dng`, `.cr2`, `.nef`, `.arw`, `.raf`.
