FROM python:3.11-slim

# Non-root user the entrypoint drops to after fixing ownership.
# Override at runtime with -e PUID=$(id -u) -e PGID=$(id -g) to match
# your host user, so files the script writes aren't root-owned.
ENV PUID=1000
ENV PGID=1000
 
RUN apt-get update && apt-get install -y --no-install-recommends \
        gosu \
    && rm -rf /var/lib/apt/lists/*
 
WORKDIR /app
 
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
 
COPY phase1-organize.py .
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
 
# Default mount points — map your host directories onto these with -v.
# /data/source and /data/dest hold photos; /data/db holds the SQLite hash DB
# so it survives container restarts.
RUN mkdir -p /data/source /data/dest /data/db
 
ENTRYPOINT ["entrypoint.sh"]
CMD ["--source", "/data/source", "--dest", "/data/dest", "--db", "/data/db/photo_hashes.db"]
