FROM python:3.11-slim

# Install system dependencies (ExifTool, gosu, and build essentials for pillow-heif)
RUN apt-get update && apt-get install -y --no-install-recommends \
    exiftool \
    gosu \
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

# Pre-create standard volume mount points
RUN mkdir -p /data/source /data/dest /appdata/db /appdata/logs

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "ns-engine.py"]
