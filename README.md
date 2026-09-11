# LensLogic

A backend engine for organizing large photo collections based on EXIF metadata, hash verification, and transactional file moves.

## Docker Usage

### Build

```bash
docker build -t lenslogic .
```

### Run (Dry Run — Default)

Scan, metadata-extract, and index photos into SQLite without moving files. Mount `/data/source` as read-only (`:ro`) to ensure safety[cite: 3].

```bash
docker run --rm \
  -e PUID=$(id -u) -e PGID=$(id -g) \
  -v /path/to/your/photos:/data/source:ro \
  -v /path/to/organized:/data/dest \
  -v /path/to/appdata:/appdata \
  lenslogic
```

### Run (Live — Copy-Verify-Delete)

Perform pre-flight disk space validation, copy files, verify SHA-1 checksums, and safely delete originals from the source folder[cite: 3]. Drop `:ro` so the container can clean up files[cite: 3].

```bash
docker run --rm \
  -e PUID=$(id -u) -e PGID=$(id -g) \
  -v /path/to/your/photos:/data/source \
  -v /path/to/organized:/data/dest \
  -v /path/to/appdata:/appdata \
  lenslogic python3 phase1-organize.py --live
```

---

## Configuration & Notes

* **User Mapping:** `PUID`/`PGID` match the container process permissions to your host user, preventing root-owned output files[cite: 3].
* **Volume Layout:**
  * `/data/source`: Raw input directory containing photos[cite: 3].
  * `/data/dest`: Structured target directory organized by `YYYY/MM` format[cite: 3].
  * `/appdata`: Dedicated application directory storing persistent data inside `/appdata/db` and log files inside `/appdata/logs`.
* **Persistence:** SQLite database (`photo_hashes.db`) stores SHA-1 checksums and metadata to prevent re-processing across multiple runs[cite: 3].