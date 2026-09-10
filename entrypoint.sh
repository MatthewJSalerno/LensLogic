#!/bin/sh
set -e
 
# Create (or reuse) a group/user matching the host UID/GID passed in via
# -e PUID/-e PGID, so files this script creates/moves on the mounted
# volumes end up owned by you on the host, not by root.
if ! getent group "$PGID" >/dev/null 2>&1; then
    addgroup --gid "$PGID" appuser
fi
if ! getent passwd "$PUID" >/dev/null 2>&1; then
    adduser --uid "$PUID" --gid "$PGID" --disabled-password --gecos "" appuser
fi
 
# Make sure the mounted directories are writable by that user.
chown -R "$PUID:$PGID" /data/dest /data/db 2>/dev/null || true
 
# Drop from root to the mapped user, then hand off to the script.
exec gosu "$PUID:$PGID" python3 phase1-organize.py "$@"
