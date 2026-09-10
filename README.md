# PhotoOrganizer
## Docker

### Build

```bash
docker build -t photo-organizer .
```

### Run (dry run — default)

```bash
docker run --rm \
  -e PUID=$(id -u) -e PGID=$(id -g) \
  -v /path/to/your/photos:/data/source:ro \
  -v /path/to/organized:/data/dest \
  -v /path/to/hashdb:/data/db \
  photo-organizer
```

### Run (live — actually moves/deletes files)

```bash
docker run --rm \
  -e PUID=$(id -u) -e PGID=$(id -g) \
  -v /path/to/your/photos:/data/source \
  -v /path/to/organized:/data/dest \
  -v /path/to/hashdb:/data/db \
  photo-organizer --live
```

**Notes:**
- `PUID`/`PGID` map the container's user to your host user, so files created in `/data/dest` aren't root-owned.
- `/data/source` is mounted `:ro` (read-only) for the dry run. Drop `:ro` for the live run — exact-duplicate deletion needs write access to the source.
- The SQLite hash DB at `/data/db` persists across runs, so re-running the container won't re-hash files it already processed.
