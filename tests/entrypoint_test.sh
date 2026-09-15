#!/bin/sh
# Checks the container entrypoint's ownership handling against real bind
# mounts. Runs on the host, since it needs docker; the smoke suite runs inside
# the container, after the entrypoint has already done its work.
#
#   docker build -t negativespace . && sh tests/entrypoint_test.sh
set -eu

IMAGE=${IMAGE:-negativespace}
MAPPED=4242
ME=$(id -u)
WORK=$(mktemp -d)
FAILED=0

cleanup() {
    # The container hands some of these to MAPPED, which this user may not be
    # able to remove.
    docker run --rm --entrypoint rm -v "$WORK":/w "$IMAGE" -rf /w/dest /w/appdata >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

expect_owner() {
    actual=$(stat -c %u "$1")
    if [ "$actual" = "$2" ]; then
        echo "  ok    $3"
    else
        echo "  FAIL  $3 (owner $actual, expected $2)"
        FAILED=1
    fi
}

mkdir -p "$WORK/dest/2020/01/01" "$WORK/appdata/db"
touch "$WORK/dest/2020/01/01/existing.jpg" "$WORK/appdata/db/ns_sqlite.db"

docker run --rm -e PUID=$MAPPED -e PGID=$MAPPED \
    -v "$WORK/dest":/data/dest -v "$WORK/appdata":/appdata \
    "$IMAGE" python3 ns-engine.py --help >/dev/null

expect_owner "$WORK/dest" "$MAPPED" "the destination root is handed to the mapped user"
expect_owner "$WORK/dest/2020" "$ME" "folders already in the destination keep their owner"
expect_owner "$WORK/dest/2020/01/01/existing.jpg" "$ME" "files already in the destination keep their owner"
expect_owner "$WORK/appdata/db/ns_sqlite.db" "$MAPPED" "appdata is handed over recursively"

exit $FAILED
