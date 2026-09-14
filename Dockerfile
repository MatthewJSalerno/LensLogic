FROM python:3.11-slim

# Install system dependencies (ExifTool, gosu, tzdata, and build essentials for
# pillow-heif). tzdata is required for -e TZ=<zone> to have any effect: without
# the zoneinfo database the variable is silently ignored and the container stays
# on UTC, which quietly changes which YYYY/MM/DD folder a photo lands in.
RUN apt-get update && apt-get install -y --no-install-recommends \
    exiftool \
    gosu \
    tzdata \
    build-essential \
    libheif-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application script and entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
COPY ns-engine.py .
RUN chmod 644 ns-engine.py

# Source and destination MUST map to separate, non-overlapping underlying
# folders. Never mount the same folder at both paths, or nest one in the other.
# Different container paths do not ensure separate storage (including NFS).
# Overlapping mounts are unsupported and can cause unintended file deletion.
# Pre-create standard volume mount points
RUN mkdir -p /data/source /data/dest /appdata/db /appdata/logs

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "ns-engine.py"]
