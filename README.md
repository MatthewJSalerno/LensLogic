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

> **Note:** `--move` against a read-only-mounted source will not corrupt anything, but every file will report `status='Failed'` — the copy succeeds and only the source deletion fails, so nothing is ever lost. Re-running is safe and does **not** accumulate duplicate copies: the engine recognizes that an identical copy already exists at the destination and skips rewriting it, failing only on the delete. Use `--copy` for read-only sources instead — it is the same verified copy without the futile delete step.

---

## Configuration & Notes

- **User Mapping:** `PUID`/`PGID` match the container process permissions to your host user, preventing root-owned output files.
- **Timezone (`TZ`):** Controls which `YYYY/MM/DD` folder a photo lands in when it has **no usable EXIF date** and the engine falls back to the file's modification time. A container does **not** inherit your workstation's timezone — it runs UTC unless told otherwise — so a file modified at 21:00 local time can be filed under the *next* day. Pass your zone to avoid that:

  ```bash
  -e TZ=America/New_York
  ```

  Photos that *do* carry an EXIF date are unaffected: those timestamps have no timezone attached and are used exactly as the camera recorded them, which is almost always what you want. The engine logs the zone it resolved at startup (`Timezone: EDT (UTC-0400)`), so you can confirm the setting took effect rather than assuming it did.
- **Volume Layout:**
  - `/data/source`: Raw input directory containing photos.
  - `/data/dest`: Structured target directory organized by `YYYY/MM/DD` format.
  - `/appdata`: Dedicated application directory storing persistent data inside `/appdata/db` and log files inside `/appdata/logs`.
- **Persistence:** SQLite database (`ns_sqlite.db`) stores SHA1 checksums, perceptual hashes, and status to prevent re-processing across multiple runs. The storage engine is named in the file so a second store can sit beside it later without ambiguity.
- **Date & Metadata Resolution:** ExifTool is a **hard requirement** — the engine won't start without it (both the `exiftool` binary and the `PyExifTool` Python package). It runs as a persistent process per worker rather than spawning a subprocess per file, cutting ExifTool overhead roughly 30x. PIL and file-modification-time remain as defensive per-file fallbacks for the rare case ExifTool itself fails on one specific file — see `ns-engine.py`'s module docstring for the full breakdown.
- **Single-Instance Lock:** Only one engine process may run against a given `--base` (i.e., a given `/appdata` mount) at a time — enforced via an OS-level `flock` on `/appdata/engine.lock`. This is released automatically by the kernel on any exit, including a `docker stop` that times out into `SIGKILL` — no manual deletion of the lock file is ever needed, even after a force-stopped container. **NFS caveat:** `flock` reliability is weaker over NFS depending on the server/client's `lockd`/`statd` setup — fine for local disk or a standard Docker volume backing `/appdata`, but worth a second look if that mount is ever NFS-backed instead.
- **Supported Formats:** `.jpg`, `.jpeg`, `.png`, `.heic`, `.tiff`, `.raw`, `.dng`, `.cr2`, `.nef`, `.arw`, `.raf`.
