# NegativeSpace

A photo-organizing engine. `ns-engine.py` is the engine; Phase 2 adds a web UI
that calls it. CLI args are the internal calling convention between the two —
they stay documented and usable, but end users interact through the web UI.

## Privacy: never publish library contents

The maintainer's real photo library is used for validation. Its directory
names and filenames contain personal information — family names, event names,
locations, dates.

**Never put any of the following into a commit message, PR title or body,
issue, code comment, test fixture, or any other file in this repository:**

- Absolute paths from a real run (`/nfs/...`, `/data/source/...`, `/home/<user>/...`)
- Directory or file names observed in the library, including partial ones
- Hostnames, usernames, or mount points from the maintainer's machines
- Verbatim log output or SQL query results from a run against the real library

**Do publish** aggregate figures — file counts, byte totals, throughput,
wall-clock times, per-status counts, schema versions. These are what make a
validation record useful and they carry nothing identifying. When a specific
file or directory must be referred to, describe it by shape instead: "a single
leaf directory of ~1,300 files from a bulk-download export", never its path.

When the maintainer pastes run output into a conversation, treat it as
reference material for that conversation only. Summarize it numerically before
it goes anywhere near the repository.

## Validation records

The maintainer validates branches against the real library before merging.
Record those results as a table in the PR body (counts, sizes, rates, wall
clock) so the evidence outlives the terminal scrollback. Never paste the raw
run log.
