#!/usr/bin/env python3
"""
End-to-end smoke tests for ns-engine.py.

Run this INSIDE the container (or anywhere ExifTool, Pillow, imagehash and
rawpy are installed) — it drives the real engine as a subprocess against real
image files, rather than importing it and stubbing things out.

    docker build -t negativespace .
    docker run --rm -v "$PWD":/app -w /app negativespace python3 tests/engine_smoke_test.py

    # or directly, if deps are installed locally:
    python3 tests/engine_smoke_test.py

Options:
    --engine PATH   path to ns-engine.py (default: alongside this file's parent)
    --keep          leave the workspace on disk for inspection
    --filter NAME   run only tests whose name contains NAME
    -v              show engine stdout for each run

Environment:
    NS_TEST_RAW_DIR  Folder of genuine RAW files (.cr2/.nef/.arw/.raf/.dng) to
                     exercise the rawpy decode path for real. That path cannot
                     be covered with synthetic fixtures — LibRaw rejects
                     fabricated files — so the test skips unless you point this
                     at real camera output. Worth doing at least once: it is
                     the only part of the engine no synthetic test can reach.

Exit code is non-zero if any test fails.
"""

import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ENGINE = None
WORKSPACE = None
VERBOSE = False
RESULTS = []


# ----------------------------------------------------------------- utilities

class Fail(AssertionError):
    pass


def check(condition, message):
    if not condition:
        raise Fail(message)


def engine_output(proc):
    """Everything the engine printed. Logging goes to stdout (configure_logging
    attaches a StreamHandler on sys.stdout), but read both streams so a test
    asserting on a log line cannot quietly pass or fail on the wrong one."""
    return (proc.stdout or "") + (proc.stderr or "")


def test(fn):
    """Registers a test function. Name doubles as the label."""
    RESULTS.append(fn)
    return fn


def run_engine(case, *args, expect_rc=0, timeout=300):
    """Runs the engine against a case directory. Returns CompletedProcess."""
    cmd = [sys.executable, str(ENGINE),
           "--source", str(case / "src"),
           "--dest", str(case / "dest"),
           "--base", str(case / "appdata")] + [str(a) for a in args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        # Surface what the engine managed to print before it stalled. Without
        # this a hang reports only the command line — which is what happened in
        # CI, leaving a 300-second timeout with nothing to diagnose it from.
        # The last lines before the stall say which phase it died in.
        raw = e.stdout or b""
        partial = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
        tail = "\n".join(f"      | {l}" for l in partial.splitlines()[-25:])
        raise Fail(
            f"engine did not finish within {timeout}s — a HANG, not a slow run.\n"
            f"    args: {' '.join(str(a) for a in args) or '(index)'}\n"
            f"    last output before it stalled:\n{tail or '      | (nothing captured)'}"
        )
    if VERBOSE:
        print("\n".join("      | " + l for l in proc.stdout.splitlines()))
    if expect_rc is not None and proc.returncode != expect_rc:
        raise Fail(f"engine exited {proc.returncode}, expected {expect_rc}\n"
                   f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}")
    return proc


def spawn_engine(case, *args):
    """Starts the engine without waiting, for signal/kill tests."""
    cmd = [sys.executable, str(ENGINE),
           "--source", str(case / "src"),
           "--dest", str(case / "dest"),
           "--base", str(case / "appdata")] + [str(a) for a in args]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def db(case):
    conn = sqlite3.connect(case / "appdata" / "db" / "ns_sqlite.db")
    conn.row_factory = sqlite3.Row
    return conn


def rows(case, sql, params=()):
    conn = db(case)
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def status_of(case, name):
    r = rows(case, "SELECT status FROM photos WHERE source_path LIKE ?", (f"%/{name}",))
    return r[0]["status"] if r else None


def dest_files(case):
    d = case / "dest"
    return sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file()) if d.exists() else []


def src_files(case):
    s = case / "src"
    return sorted(p.relative_to(s).as_posix() for p in s.rglob("*") if p.is_file())


def new_case(name):
    case = WORKSPACE / name
    (case / "src").mkdir(parents=True)
    (case / "appdata").mkdir(parents=True)
    return case


def wait_for_move_to_start(case, timeout=60):
    """
    Blocks until the engine has actually begun writing to the destination.
    Signalling after a fixed sleep is a race — on a fast machine the whole move
    finishes first and the test silently stops exercising anything.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = case / "dest"
        if d.exists() and any(p.is_file() for p in d.rglob("*")):
            return True
        time.sleep(0.05)
    return False


def make_photo(path: Path, content_seed: str, date="2024:02:14 09:30:00", size=(64, 48),
               noise=False):
    """
    Writes a real JPEG with real EXIF. Pixel content is derived from the seed so
    that distinct seeds produce genuinely different bytes (different SHA-1),
    and an identical seed produces an identical file.
    """
    import hashlib
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    # md5, not hash(): str hashing is randomized per process, so hash() would
    # not be reproducible across runs and two seeds could collide mod 256 in
    # all three channels. Painting md5 bytes into the top row guarantees that
    # distinct seeds differ in actual pixel data, while an identical seed
    # reproduces byte-identical output (which the duplicate and idempotency
    # tests both depend on).
    if noise:
        # Incompressible content, so the file on disk is genuinely large and a
        # move takes long enough to interrupt. Random rather than seeded: these
        # callers need distinct files, not reproducible ones.
        img = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    else:
        digest = hashlib.md5(content_seed.encode()).digest()
        img = Image.new("RGB", size, (digest[0], digest[1], digest[2]))
        for x in range(min(len(digest), size[0])):
            img.putpixel((x, 0), (digest[x], digest[(x + 1) % 16], digest[(x + 2) % 16]))
    img.save(path, "JPEG", quality=95)
    if date:
        subprocess.run(
            ["exiftool", "-overwrite_original", f"-DateTimeOriginal={date}",
             f"-CreateDate={date}", str(path)],
            capture_output=True, check=True,
        )


# --------------------------------------------------------------------- tests

@test
def index_excludes_hidden_and_appledouble():
    """Index: skips hidden files, AppleDouble sidecars and hidden trees."""
    case = new_case("index_hidden")
    make_photo(case / "src" / "IMG_0001.jpg", "a")
    make_photo(case / "src" / "sub" / "IMG_0002.JPG", "b")
    make_photo(case / "src" / "._IMG_0001.jpg", "appledouble")
    make_photo(case / "src" / ".Trashes" / "IMG_0003.jpg", "trash")
    (case / "src" / ".DS_Store").write_bytes(b"junk")

    run_engine(case)
    indexed = sorted(Path(r["source_path"]).name for r in rows(case, "SELECT source_path FROM photos"))
    check(indexed == ["IMG_0001.jpg", "IMG_0002.JPG"],
          f"expected only the two real photos, got {indexed}")
    check(not (case / "dest").exists(),
          "a bare Index created the destination tree; it must not write there")


@test
def exts_accepts_bare_and_dotted():
    """--exts: 'jpg' and '.jpg' behave identically."""
    case = new_case("exts")
    make_photo(case / "src" / "a.jpg", "a")
    make_photo(case / "src" / "b.png", "b", date=None)

    run_engine(case, "--exts", "jpg")
    bare = {Path(r["source_path"]).name for r in rows(case, "SELECT source_path FROM photos")}
    check(bare == {"a.jpg"}, f"--exts jpg matched {bare or 'nothing'}; expected just a.jpg")

    case2 = new_case("exts_dotted")
    make_photo(case2 / "src" / "a.jpg", "a")
    make_photo(case2 / "src" / "b.png", "b", date=None)
    run_engine(case2, "--exts", ".jpg")
    dotted = {Path(r["source_path"]).name for r in rows(case2, "SELECT source_path FROM photos")}
    check(dotted == bare, f"dotted form gave {dotted}, bare form gave {bare}")


@test
def move_preserves_distinct_photos_sharing_a_filename():
    """Move: two different photos named alike both survive (the data-loss regression)."""
    case = new_case("collision")
    make_photo(case / "src" / "cardA" / "IMG_0001.jpg", "PHOTO-A")
    make_photo(case / "src" / "cardB" / "IMG_0001.jpg", "PHOTO-B")

    run_engine(case)
    run_engine(case, "--move")

    out = dest_files(case)
    check(len(out) == 2, f"expected BOTH photos at the destination, found {out}")
    check(any(f.endswith("IMG_0001.jpg") for f in out) and any("_1" in f for f in out),
          f"expected a suffixed second copy, got {out}")

    hashes = {r["sha1_hash"] for r in rows(case, "SELECT sha1_hash FROM photos")}
    check(len(hashes) == 2, "the two photos should have distinct hashes")
    check(src_files(case) == [], f"--move should have emptied source, left {src_files(case)}")
    collisions = rows(case, "SELECT COUNT(*) c FROM photos WHERE has_name_collision = 1")[0]["c"]
    check(collisions == 1, f"expected exactly one row flagged has_name_collision, got {collisions}")


@test
def exact_duplicate_removed_only_with_verified_copy():
    """Move: an exact duplicate is flagged and its source removed once a verified copy exists."""
    case = new_case("dupes")
    make_photo(case / "src" / "original.jpg", "SAME")
    make_photo(case / "src" / "nested" / "copy.jpg", "SAME")

    run_engine(case)
    statuses = {Path(r["source_path"]).name: r["status"]
                for r in rows(case, "SELECT source_path, status FROM photos")}
    check(sorted(statuses.values()) == ["Duplicate", "Pending"],
          f"expected one Pending anchor and one Duplicate, got {statuses}")

    run_engine(case, "--move")
    final = {Path(r["source_path"]).name: r["status"]
             for r in rows(case, "SELECT source_path, status FROM photos")}
    check("Removed_Duplicate" in final.values(), f"duplicate not cleaned up: {final}")
    check(len(dest_files(case)) == 1, f"only one physical copy should remain: {dest_files(case)}")
    check(src_files(case) == [], f"source should be empty, left {src_files(case)}")

    # Every row — the surviving copy AND each duplicate — must point at a file
    # that actually exists. A duplicate keeps the destination projected for it
    # at Index time under its own filename, which is never written; left
    # uncorrected the record describes a phantom, which is useless exactly when
    # it matters: "this was a duplicate, so where did its content end up?"
    for r in rows(case, "SELECT source_path, status, dest_path FROM photos"):
        check(r["dest_path"] and Path(r["dest_path"]).exists(),
              f"{Path(r['source_path']).name} ({r['status']}) points at a file that does not "
              f"exist: {r['dest_path']}")

    # ...and every member of the group is reachable from its sha1.
    group = rows(case, "SELECT sha1_hash, COUNT(*) n FROM photos GROUP BY sha1_hash")
    check(len(group) == 1 and group[0]["n"] == 2,
          f"both copies should share one sha1 group, got {group}")


@test
def copy_is_non_destructive_and_idempotent():
    """Copy: sources untouched, and repeated Index+Copy cycles do not multiply files."""
    case = new_case("copy_idem")
    make_photo(case / "src" / "photo.jpg", "X")

    for cycle in (1, 2, 3):
        run_engine(case)
        run_engine(case, "--copy")
        out = dest_files(case)
        check(out == ["2024/02/14/photo.jpg"],
              f"cycle {cycle}: expected exactly one copy, got {out}")
        check(src_files(case) == ["photo.jpg"],
              f"cycle {cycle}: --copy must never touch the source")

    check(status_of(case, "photo.jpg") == "Copied",
          f"expected status Copied, got {status_of(case, 'photo.jpg')}")


@test
def file_ids_targeting_is_scoped():
    """--file-ids: only the selected file moves; duplicate cleanup stays in scope."""
    case = new_case("file_ids")
    make_photo(case / "src" / "wanted.jpg", "WANTED")
    make_photo(case / "src" / "untouched.jpg", "UNTOUCHED")
    # an exact-duplicate pair entirely outside the selection
    make_photo(case / "src" / "other1.jpg", "OTHERDUP")
    make_photo(case / "src" / "other2.jpg", "OTHERDUP")

    run_engine(case)
    wanted_id = rows(case, "SELECT id FROM photos WHERE source_path LIKE '%/wanted.jpg'")[0]["id"]
    run_engine(case, "--move", "--file-ids", str(wanted_id))

    remaining = src_files(case)
    check("wanted.jpg" not in remaining, "the targeted file should have moved")
    check("untouched.jpg" in remaining, "an untargeted file was moved")
    dup_left = [f for f in remaining if f.startswith("other")]
    check(len(dup_left) == 2,
          f"duplicate cleanup escaped the selection and deleted out-of-scope sources: {remaining}")


@test
def source_subdir_targeting_is_scoped():
    """--source-subdir: scopes to a folder without enumerating ids."""
    case = new_case("subdir")
    make_photo(case / "src" / "day1" / "a.jpg", "A")
    make_photo(case / "src" / "day1" / "b.jpg", "B")
    make_photo(case / "src" / "day2" / "c.jpg", "C")
    # a sibling whose name shares the prefix — must NOT be swept in
    make_photo(case / "src" / "day1extra" / "d.jpg", "D")

    run_engine(case)
    run_engine(case, "--move", "--source-subdir", "day1")

    remaining = src_files(case)
    check(sorted(remaining) == ["day1extra/d.jpg", "day2/c.jpg"],
          f"subdir targeting moved the wrong set; source still holds {remaining}")


@test
def source_subdir_with_wildcard_chars_is_literal():
    """--source-subdir: '_' and '%' in a folder name are literal, not LIKE wildcards."""
    case = new_case("subdirglob")
    # The prefix match runs through SQL LIKE, where '_' means "any single
    # character" and '%' means "any sequence". Unescaped, targeting My_Photos
    # would also sweep in MyXPhotos — copying files the user never selected,
    # or under --move deleting their sources. Folder names with underscores
    # are ordinary, and Phase 2 lets users pick arbitrary folders.
    make_photo(case / "src" / "My_Photos" / "a.jpg", "A")
    make_photo(case / "src" / "My_Photos" / "nested" / "b.jpg", "B")
    make_photo(case / "src" / "MyXPhotos" / "c.jpg", "C")
    make_photo(case / "src" / "100%Done" / "d.jpg", "D")
    make_photo(case / "src" / "100XDone" / "e.jpg", "E")

    run_engine(case)
    run_engine(case, "--move", "--source-subdir", "My_Photos")

    remaining = sorted(src_files(case))
    check(remaining == ["100%Done/d.jpg", "100XDone/e.jpg", "MyXPhotos/c.jpg"],
          f"'_' was treated as a LIKE wildcard; source still holds {remaining}")

    run_engine(case, "--move", "--source-subdir", "100%Done")

    remaining = sorted(src_files(case))
    check(remaining == ["100XDone/e.jpg", "MyXPhotos/c.jpg"],
          f"'%' was treated as a LIKE wildcard; source still holds {remaining}")


@test
def targeted_runs_skip_unchanged_files():
    """--source-subdir / --file-ids honour the unchanged-file skip, not just full scans."""
    case = new_case("targetskip")
    make_photo(case / "src" / "day1" / "a.jpg", "A")
    make_photo(case / "src" / "day1" / "b.jpg", "B")
    make_photo(case / "src" / "day2" / "c.jpg", "C")

    run_engine(case)  # full index; everything now has size+mtime recorded

    # A scoped re-run must not re-read files the catalog already matches.
    # partition_unchanged() used to be applied only to the full-scan branch,
    # so targeted runs re-hashed and re-decoded every file — the expensive
    # path, on exactly the runs the web UI issues.
    out = engine_output(run_engine(case, "--source-subdir", "day1"))
    check("Skipping 2 unchanged file(s)" in out,
          f"scoped re-run did not skip unchanged files; log said:\n{out}")

    # Touching one file must bring exactly that file back into the scan.
    target = case / "src" / "day1" / "a.jpg"
    os.utime(target, (time.time() + 10, time.time() + 10))
    out = engine_output(run_engine(case, "--source-subdir", "day1"))
    check("Skipping 1 unchanged file(s)" in out,
          f"a changed file was not re-scanned; log said:\n{out}")

    # --force-rehash still overrides the skip.
    out = engine_output(run_engine(case, "--source-subdir", "day1", "--force-rehash"))
    check("Skipping" not in out and "force-rehash" in out,
          f"--force-rehash did not bypass the skip; log said:\n{out}")


@test
def targeting_nothing_says_why():
    """A scoped run against an un-indexed catalog explains itself instead of quietly succeeding."""
    case = new_case("emptytarget")
    make_photo(case / "src" / "day1" / "a.jpg", "A")

    # No Index has ever run, so the catalog is empty. Both targeted modes read
    # the catalog rather than the filesystem, so they match nothing — and the
    # run would otherwise report "completed successfully (0 files)", which is
    # indistinguishable from having had nothing to do.
    out = engine_output(run_engine(case, "--copy", "--source-subdir", "day1"))
    check("No indexed files found" in out,
          f"a scoped copy against an empty catalog did not explain itself; log said:\n{out}")
    check("Run an Index over this source first" in out,
          f"the warning did not say how to fix it; log said:\n{out}")

    out = engine_output(run_engine(case, "--copy", "--file-ids", "1,2,3"))
    check("IDs exist only for files a previous Index recorded" in out,
          f"an unresolvable --file-ids selection did not explain itself; log said:\n{out}")

    # And once indexed, the same scoped command finds its file.
    run_engine(case)
    out = engine_output(run_engine(case, "--copy", "--source-subdir", "day1"))
    check("No indexed files found" not in out,
          f"scoped copy still reported an empty target after indexing; log said:\n{out}")
    check((case / "dest").exists(), "scoped copy produced no destination tree after indexing")


@test
def stale_selection_records_a_specific_failure():
    """A targeted file deleted outside the engine is recorded as Failed with a real reason."""
    case = new_case("stale")
    make_photo(case / "src" / "vanishing.jpg", "V")
    make_photo(case / "src" / "staying.jpg", "S")

    run_engine(case)
    ids = {Path(r["source_path"]).name: r["id"]
           for r in rows(case, "SELECT id, source_path FROM photos")}
    (case / "src" / "vanishing.jpg").unlink()

    # Must be a TARGETED run. A full directory scan never *discovers* a deleted
    # file, so it is simply not processed and keeps its previous status — that
    # is correct. The stale-selection case this checks is specific to
    # --file-ids / --source-subdir, per project-spec.md 4.4.
    run_engine(case, "--file-ids", f"{ids['vanishing.jpg']},{ids['staying.jpg']}")

    check(status_of(case, "vanishing.jpg") == "Failed",
          f"expected Failed, got {status_of(case, 'vanishing.jpg')}")
    msg = rows(case, "SELECT error_message FROM operations WHERE source_path LIKE '%vanishing%' "
                     "AND error_message IS NOT NULL ORDER BY id DESC LIMIT 1")
    check(msg and "Source file changed" in msg[0]["error_message"],
          f"expected the specific 'Source file changed' reason, got {msg}")
    check(status_of(case, "staying.jpg") == "Pending",
          "one bad file must not derail the rest of the scan")


@test
def unreadable_file_does_not_abort_the_scan():
    """A permission-denied file fails alone; the rest of the library still indexes."""
    if os.geteuid() == 0:
        raise Fail("SKIP: running as root, permission checks are bypassed")
    case = new_case("unreadable")
    for i in range(4):
        make_photo(case / "src" / f"ok{i}.jpg", f"ok{i}")
    bad = case / "src" / "bad.jpg"
    make_photo(bad, "bad")
    bad.chmod(0o000)
    try:
        run_engine(case)
        indexed = rows(case, "SELECT status FROM photos")
        check(len(indexed) == 5, f"expected all 5 files recorded, got {len(indexed)}")
        check(status_of(case, "bad.jpg") == "Failed", "unreadable file should be Failed")
        # Match on the FILENAME, not a substring of the full path: the
        # workspace prefix "ns-smoke-" itself contains "ok", so `"ok" in
        # source_path` matched every row — including bad.jpg — and asserted
        # the deliberately-unreadable file should be Pending.
        oks = [r for r in rows(case, "SELECT source_path, status FROM photos")
               if Path(r["source_path"]).name.startswith("ok")]
        check(len(oks) == 4, f"expected 4 readable files, matched {len(oks)}")
        check(all(r["status"] == "Pending" for r in oks),
              f"readable files should still be indexed normally: "
              f"{[(Path(r['source_path']).name, r['status']) for r in oks]}")
    finally:
        bad.chmod(0o644)


@test
def single_instance_lock_rejects_a_second_run():
    """Two engines against one --base cannot run concurrently."""
    import fcntl
    case = new_case("lock")
    make_photo(case / "src" / "a.jpg", "a")

    # Hold the engine's own lock file directly rather than racing a second
    # process — deterministic, with no dependence on how long a scan takes.
    lock_path = case / "appdata" / "engine.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        blocked = run_engine(case, expect_rc=1)
        combined = blocked.stdout + blocked.stderr
        check("already running" in combined,
              f"second run should refuse with a clear message; got:\n{combined}")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    # ...and succeeds once the lock is released.
    run_engine(case)


@test
def sigterm_during_move_cancels_cleanly():
    """SIGTERM mid-move: in-flight file finishes, remainder logged Cancelled, run marked Cancelled."""
    case = new_case("cancel")
    for i in range(120):
        make_photo(case / "src" / f"p{i:03d}.jpg", f"p{i}", size=(500, 400), noise=True)

    run_engine(case)
    proc = spawn_engine(case, "--move")
    check(wait_for_move_to_start(case), "move never started")
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=180)

    run_row = rows(case, "SELECT status FROM runs ORDER BY id DESC LIMIT 1")[0]
    if run_row["status"] == "Completed":
        # The move outran the signal on this machine. Reporting that honestly
        # beats failing, but it does mean cancellation went untested here.
        raise Fail("SKIP: move completed before SIGTERM landed — raise the file "
                   "count or size in this test to exercise cancellation")
    check(run_row["status"] == "Cancelled", f"run should be Cancelled, got {run_row['status']}")
    cancelled = rows(case, "SELECT COUNT(*) c FROM operations WHERE status = 'Cancelled'")[0]["c"]
    check(cancelled > 0, "expected some files logged as Cancelled")
    stuck = rows(case, "SELECT COUNT(*) c FROM photos WHERE status = 'Processing'")[0]["c"]
    check(stuck == 0, "no row should be left mid-flight after a clean cancellation")

    # Every source file must still be accounted for: either moved, or still present.
    for r in rows(case, "SELECT source_path, dest_path, status FROM photos"):
        if r["status"] in ("Completed",):
            check(Path(r["dest_path"]).exists(), f"Completed but missing at dest: {r['dest_path']}")
            check(not Path(r["source_path"]).exists(), f"Completed but source remains: {r['source_path']}")
        elif r["status"] == "Pending":
            check(Path(r["source_path"]).exists(), f"Pending but source gone: {r['source_path']}")


@test
def sigkill_during_move_is_reconciled_and_loses_nothing():
    """SIGKILL mid-move: next run reconciles state, and no photo is lost."""
    case = new_case("crash")
    count = 120
    for i in range(count):
        make_photo(case / "src" / f"p{i:03d}.jpg", f"p{i}", size=(500, 400), noise=True)

    run_engine(case)
    before = {r["sha1_hash"] for r in rows(case, "SELECT sha1_hash FROM photos")}

    proc = spawn_engine(case, "--move")
    check(wait_for_move_to_start(case), "move never started")
    proc.kill()
    proc.wait(timeout=60)

    if len(dest_files(case)) == count:
        raise Fail("SKIP: move completed before SIGKILL landed — raise the file "
                   "count or size in this test to exercise crash recovery")

    # No photo may be absent from BOTH sides at any point after a hard kill.
    missing = []
    for r in rows(case, "SELECT source_path, dest_path, status FROM photos"):
        src_there = Path(r["source_path"]).exists()
        dst_there = r["dest_path"] and Path(r["dest_path"]).exists()
        partial = r["dest_path"] and Path(r["dest_path"] + ".organizing.partial").exists()
        if not (src_there or dst_there or partial):
            missing.append(r["source_path"])
    check(not missing, f"photos vanished from both source and destination after SIGKILL: {missing[:5]}")

    # The next invocation must reconcile the interrupted state.
    run_engine(case)
    stuck = rows(case, "SELECT COUNT(*) c FROM photos WHERE status = 'Processing'")[0]["c"]
    check(stuck == 0, "reconciliation left rows stuck in Processing")
    crashed = rows(case, "SELECT COUNT(*) c FROM runs WHERE status = 'Crashed'")[0]["c"]
    check(crashed >= 1, "the killed run should have been marked Crashed on the next startup")
    leftover = list((case / "dest").rglob("*.organizing.partial"))
    check(not leftover, f"orphaned partial files were not cleaned up: {leftover}")

    # Finish the job; everything must end up at the destination exactly once.
    run_engine(case, "--move")
    after = {r["sha1_hash"] for r in rows(case, "SELECT sha1_hash FROM photos")}
    check(before == after, "the set of known photo hashes changed across the crash")
    finished = len(dest_files(case))
    check(finished == count, f"expected {count} files at the destination, found {finished}")
    check(src_files(case) == [], f"source should be empty after completing the move: {src_files(case)}")


@test
def phash_is_computed_for_real_images():
    """pHash: a real perceptual hash is stored, not 'not_supported' or 'error'."""
    case = new_case("phash")
    make_photo(case / "src" / "a.jpg", "phash-a")
    make_photo(case / "src" / "b.jpg", "phash-b")
    run_engine(case)

    hashes = {Path(r["source_path"]).name: r["phash"]
              for r in rows(case, "SELECT source_path, phash FROM photos")}
    for name, ph in hashes.items():
        check(ph not in ("not_supported", "error", None, ""),
              f"{name}: pHash not computed ({ph!r}) — is imagehash installed?")
        check(len(ph) >= 8 and all(c in "0123456789abcdef" for c in ph.lower()),
              f"{name}: pHash does not look like a hex hash: {ph!r}")


@test
def raw_extension_routes_to_rawpy_and_fails_gracefully():
    """
    RAW: a .dng the decoder cannot read is recorded as 'error' and the file is
    still indexed — it must not raise, and must not silently fall through to
    PIL (which cannot decode RAW sensor data at all).
    """
    case = new_case("raw_route")
    make_photo(case / "src" / "normal.jpg", "normal")
    # A TIFF named .dng. LibRaw rejects it, which is exactly the failure this
    # checks: the engine should degrade to 'error' for that one file, not die.
    from PIL import Image
    import numpy as np
    arr = (np.random.rand(48, 64, 3) * 255).astype("uint8")
    Image.fromarray(arr).save(case / "src" / "broken.dng", "TIFF")

    run_engine(case)

    statuses = {Path(r["source_path"]).name: (r["status"], r["phash"])
                for r in rows(case, "SELECT source_path, status, phash FROM photos")}
    check("broken.dng" in statuses, f"the .dng was not indexed at all: {statuses}")
    status, phash = statuses["broken.dng"]
    check(status == "Pending", f"an undecodable RAW should still index, got status {status}")
    check(phash == "error",
          f"expected phash 'error' for an undecodable RAW, got {phash!r} — "
          f"'not_supported' means rawpy is missing; a real hash means it wrongly used PIL")
    check(statuses["normal.jpg"][1] not in ("error", "not_supported"),
          "the normal JPEG alongside it should still hash correctly")


@test
def real_raw_files_decode_when_supplied():
    """
    RAW decode: opt-in. Set NS_TEST_RAW_DIR to a folder of genuine RAW files
    (.cr2/.nef/.arw/.dng/.raf) to exercise the rawpy decode path for real —
    it cannot be synthesized, LibRaw rejects fabricated files.
    """
    raw_dir = os.environ.get("NS_TEST_RAW_DIR")
    if not raw_dir or not Path(raw_dir).is_dir():
        raise Fail("SKIP: set NS_TEST_RAW_DIR to a folder of real RAW files to run this")
    sources = [p for p in Path(raw_dir).iterdir()
               if p.suffix.lower() in {".cr2", ".nef", ".arw", ".raf", ".dng", ".raw"}]
    if not sources:
        raise Fail(f"SKIP: no RAW files found in {raw_dir}")

    case = new_case("raw_real")
    for p in sources[:5]:
        shutil.copy2(p, case / "src" / p.name)
    run_engine(case)

    for r in rows(case, "SELECT source_path, status, phash, metadata_json FROM photos"):
        name = Path(r["source_path"]).name
        check(r["phash"] not in ("error", "not_supported", None, ""),
              f"{name}: RAW pHash failed ({r['phash']!r})")
        check(r["status"] == "Pending", f"{name}: expected Pending, got {r['status']}")
        check(r["metadata_json"] and "date_taken" in r["metadata_json"],
              f"{name}: no metadata captured from the RAW file")


@test
def date_source_records_where_the_date_came_from():
    """
    Every photo records whether its date came from EXIF or the file's mtime.

    This is the difference between a date that is timezone-proof and one that
    is not: EXIF timestamps carry no zone and are used exactly as the camera
    wrote them, while an mtime is interpreted in the container's timezone — so
    a file modified late in the evening can land in the next day's folder under
    a different TZ. Recording the source per photo is what lets the count be
    reported per run, and what the Phase 2 inspector reads to tell a user their
    date came from the filesystem rather than the camera.
    """
    case = new_case("datesource")
    make_photo(case / "src" / "with_exif.jpg", "e", date="2019:07:04 21:30:00")
    make_photo(case / "src" / "no_exif.jpg", "n", date=None)

    run_engine(case)

    found = {}
    for r in rows(case, "SELECT source_path, metadata_json, dest_path FROM photos"):
        found[Path(r["source_path"]).name] = (
            json.loads(r["metadata_json"]).get("date_source"), r["dest_path"]
        )

    check(found.get("with_exif.jpg", (None,))[0] == "exif",
          f"EXIF-dated photo should record date_source 'exif', got {found.get('with_exif.jpg')}")
    check(found.get("no_exif.jpg", (None,))[0] == "file_mtime",
          f"photo without an EXIF date should record 'file_mtime', got {found.get('no_exif.jpg')}")

    # The EXIF date is used verbatim, so its folder is fixed regardless of
    # the timezone this runs under.
    check("2019/07/04" in found["with_exif.jpg"][1],
          f"EXIF date should bucket to 2019/07/04 in any timezone, got {found['with_exif.jpg'][1]}")


@test
def schema_is_versioned_and_indexed():
    """Schema: user_version set, required indexes present, durability pragmas as intended."""
    case = new_case("schema")
    make_photo(case / "src" / "a.jpg", "a")
    run_engine(case)

    conn = db(case)
    try:
        check(conn.execute("PRAGMA user_version").fetchone()[0] >= 1, "user_version not set")
        idx = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx%'")}
        check({"idx_photos_sha1", "idx_photos_status", "idx_operations_run"} <= idx,
              f"missing expected indexes, found {idx}")
        plan = " ".join(str(x) for x in conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM photos WHERE sha1_hash = ?", ("x",)).fetchall())
        check("SCAN" not in plan.upper() or "INDEX" in plan.upper(),
              f"sha1 lookup is still a table scan: {plan}")
    finally:
        conn.close()


@test
def batched_scan_records_every_file():
    """Batched commits: a scan larger than one batch still persists every row."""
    case = new_case("batching")
    count = 250   # > DB_COMMIT_BATCH_SIZE
    for i in range(count):
        make_photo(case / "src" / f"f{i:03d}.jpg", f"f{i}")
    run_engine(case)
    got = rows(case, "SELECT COUNT(*) c FROM photos")[0]["c"]
    check(got == count, f"expected {count} rows after a multi-batch scan, got {got}")
    ops = rows(case, "SELECT COUNT(*) c FROM operations")[0]["c"]
    check(ops >= count, f"expected an audit row per file, got {ops}")


# ---------------------------------------------------------------------- main

def main():
    global ENGINE, WORKSPACE, VERBOSE
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", default=str(Path(__file__).resolve().parent.parent / "ns-engine.py"))
    ap.add_argument("--keep", action="store_true", help="leave the workspace on disk")
    ap.add_argument("--filter", default="", help="only run tests whose name contains this")
    ap.add_argument("-v", "--verbose", action="store_true", help="show engine output")
    args = ap.parse_args()

    ENGINE = Path(args.engine).resolve()
    VERBOSE = args.verbose
    if not ENGINE.exists():
        print(f"engine not found: {ENGINE}", file=sys.stderr)
        return 2

    for tool, hint in (("exiftool", "apt install libimage-exiftool-perl"),):
        if not shutil.which(tool):
            print(f"required tool '{tool}' not on PATH ({hint})", file=sys.stderr)
            return 2
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("Pillow is required to generate test images (pip install Pillow)", file=sys.stderr)
        return 2

    WORKSPACE = Path(tempfile.mkdtemp(prefix="ns-smoke-"))
    print(f"engine    : {ENGINE}")
    print(f"workspace : {WORKSPACE}\n")

    selected = [t for t in RESULTS if args.filter in t.__name__]
    passed = failed = skipped = 0
    failures = []

    for fn in selected:
        label = fn.__name__.replace("_", " ")
        print(f"  {label} ... ", end="", flush=True)
        started = time.time()
        try:
            fn()
        except Fail as e:
            if str(e).startswith("SKIP:"):
                skipped += 1
                print(f"SKIP ({str(e)[6:]})")
                continue
            failed += 1
            failures.append((fn.__name__, str(e)))
            print(f"FAIL  ({time.time()-started:.1f}s)")
        except Exception as e:
            failed += 1
            failures.append((fn.__name__, f"{type(e).__name__}: {e}"))
            print(f"ERROR ({time.time()-started:.1f}s)")
        else:
            passed += 1
            print(f"ok    ({time.time()-started:.1f}s)")

    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    if failures:
        print("\nfailures:")
        for name, msg in failures:
            print(f"\n  {name}:")
            for line in msg.splitlines():
                print(f"    {line}")

    if args.keep or failures:
        print(f"\nworkspace kept for inspection: {WORKSPACE}")
    else:
        shutil.rmtree(WORKSPACE, ignore_errors=True)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
