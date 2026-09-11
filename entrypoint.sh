#!/bin/sh
set -e

# Fallback defaults if PUID/PGID are not passed
PUID=${PUID:-1000}
PGID=${PGID:-1000}

# Create (or reuse) a group/user matching host UID/GID[cite: 2]
if ! getent group "$PGID" >/dev/null 2>&1; then
    addgroup --gid "$PGID" appuser
fi
if ! getent passwd "$PUID" >/dev/null 2>&1; then
    adduser --uid "$PUID" --gid "$PGID" --disabled-password --gecos "" appuser
fi

# Ensure output and appdata directories are writable by the mapped user[cite: 2]
chown -R "$PUID:$PGID" /data/dest /appdata 2>/dev/null || true

# Drop root privileges and execute command[cite: 2]
exec gosu "$PUID:$PGID" "$@"