# Functional & Technical Design Specification: NegativeSpace Phase 3 — Fuzzy Matching & EXIF Editing

## 1. Scope

Phase 3 covers near-duplicate (visually similar, not byte-identical) detection and grouping, plus EXIF metadata editing/synchronization across related images. This content was originally drafted as part of `phase2-spec.md` §4.3 and was moved here since Phase 2's scope is explicitly the web UI for existing Move/Copy/Index operations — fuzzy matching is a separate body of work with its own data-model and UX implications, not a natural fit alongside Phase 2's file-selection/job-management focus.

Phase 1 already generates and stores a pHash (`phash` column on `photos`) for every file — that work is done and is a prerequisite for everything below. What's *not* built yet is anything that acts on those hashes: comparing them to each other, grouping visually similar images, or surfacing that grouping in the UI.

## 2. Match Mode Switcher (Exact vs. Fuzzy)

The top header provides a Match Mode toggle:
* **Exact SHA-1 (Default):** 100% byte-for-byte checksum matching for operations — this is what Phase 1/2 already implement.
* **Fuzzy Match (pHash):** Perceptual hash visual similarity grouping based on Hamming distance thresholds. Displays similarity percentage scores in the inspector. Groundwork for the EXIF editing capabilities below.

## 3. Destination Inventory and Duplicate Reporting

The destination is normally NegativeSpace's exclusively. Deduplication happens at Index time by SHA-1 — among files with identical content one row is `Pending` and the rest are `Duplicate`, and only `Pending` rows are ever written — so a destination the engine owns holds exactly one copy of each distinct content by construction. Duplicates there only arise when something outside the engine puts them there.

**The engine must keep its invariant: nothing under `--dest` is ever deleted.** That property is why `--copy` is trivially safe, why a read-only destination mount works, and why the worst case of a bug in the move loop is a lost source file that still exists at the destination. Deduplicating in place would trade it away permanently, giving the engine the ability to delete from the copy the user has designated canonical, on the strength of a hash comparison. Every future bug in that path would eat the good tree.

Phase 3 therefore **reports** destination duplicates rather than resolving them. A read-only inventory pass records each destination file's current SHA-1 and location; the UI groups by hash and shows what is redundant. The same view that groups by `phash` for near-duplicates groups by `sha1_hash` for exact ones — one query shape, two columns. The user acts on the result with their own tools, or by re-processing (below). No deletion path into `--dest` is added.

### 3.1 Re-processing a Disordered Destination

When a destination has been reorganized or polluted from outside, the supported repair needs no new engine capability: point `--source` at the old destination, `--dest` at a fresh location, and run `--move`. Index deduplicates by content, only anchors are written, and duplicate cleanup removes the redundant copies from what is now the source. `--move` copies then deletes one file at a time, so peak additional storage is roughly the largest single file rather than a second copy of the library.

**Two costs to surface in the UI before offering this.**

Photo IDs and run history do not survive it — see §3.2.

More subtly, **files dated from modification time will move.** A photo with no usable EXIF date was filed under its mtime; after NegativeSpace wrote it to the destination, that mtime is the time the engine wrote the file, not anything about the photograph. Re-indexing the destination therefore dates those files to the day of the re-processing run rather than their original bucket. Files carrying a real EXIF date are unaffected, since EXIF timestamps are used exactly as the camera recorded them. On a library where a meaningful fraction was mtime-dated, this silently relocates that fraction into one folder. The UI must say so before starting, and should show the count — it is directly available as `date_source = 'file_mtime'` in `metadata_json`.

This is a strong argument for reporting over re-processing on a library that is already organized: reporting costs nothing and moves nothing.

### 3.2 Open Question: Content-Addressed History

`photos.id` is the engine's identity for a file, and `operations.photo_id` hangs off it. Rebuilding the catalog — the documented remedy for a schema change, and the outcome of the re-processing above — issues new IDs, orphaning every historical row. This is in tension with the project's stated position that the catalog is a derived artifact that can be deleted and rebuilt: `photos` genuinely is derived, but `runs` and `operations` are not. They are the only record of what the engine did, and nothing recomputes them.

The sharpest case is `Removed_Duplicate`. After a `--move`, that row is the only evidence a file ever existed: the source is gone by design and the content survives only under the anchor's name. Delete the catalog and the knowledge that it existed goes with it.

Keying history on `sha1_hash` rather than `photos.id` would let it survive a rebuild, since content identity is stable across renames, moves, re-processing and new catalogs. Three problems have to be answered first:

* **Failed files have no hash.** A file that could not be read produces `sha1_hash = ''` — and failures are precisely the history worth keeping. A content key cannot cover them; they need `source_path` as a fallback identity, which is itself unstable.
* **Content identity is not file identity.** Two distinct files with identical bytes share one hash. History keyed on content becomes "everything that happened to this content," spanning several original paths. That is arguably more correct, but it is a different question from "what happened to this file," and the UI would have to say which one it is answering.
* **Editing content breaks the chain.** The EXIF editing in §5 rewrites files, changing their hashes. A content-addressed history would need to record that hash *A* became hash *B*, or it loses everything before the edit — a rename-tracking problem in a different costume.

Worth considering alongside: whether `runs` and `operations` belong in the rebuildable catalog at all, or in a separate store that is never discarded. That would remove the tension directly rather than working around it, at the cost of a second database file and cross-file joins the API layer would have to do itself.

## 4. Open Design Questions for Phase 3

These weren't resolved in the original draft and need scoping before implementation:

* **Hamming distance threshold:** what similarity score counts as "a match" for grouping purposes? Likely needs to be user-adjustable rather than fixed, given pHash similarity is inherently fuzzy and different collections (bursts of near-identical shots vs. edited variants of one photo) may want different sensitivity.
* **`collision_group` / `is_master` semantics:** these columns already exist on `photos` (added in Phase 1, unused until now). Need to define: how is a `collision_group` ID assigned (on-demand when the UI requests clustering, or continuously during every Index)? Who/what decides `is_master` — first-discovered, highest-resolution, user-selected?
* **Merger tool UX:** the original Phase 1/2 spec drafts mention a "Merger" tool for resolving fuzzy-match groups, without detail. Needs its own design pass — likely: show the group, let the user pick a master, decide what happens to the others (delete? keep both? merge metadata?).
* **EXIF editing/synchronization:** "groundwork for future EXIF editing capabilities" was mentioned but not specified — does this mean editing a single file's metadata, or propagating metadata from a group's master to its near-duplicates? These are different features with different risk profiles (the latter mutates files that weren't directly selected by the user).

## 5. Dependencies on Phase 1/2

* Requires the `phash` values Phase 1 already computes — no new hashing work needed, just new logic that reads and compares existing values.
* Likely requires a new backend endpoint (e.g. `GET /api/v1/photos/similar/{id}?threshold=N`) computing Hamming distance across the `photos` table's `phash` column — this is a Node.js/API-layer concern, not a `ns-engine.py` engine change, unless clustering needs to happen at index time rather than on-demand.
* The Inspector panel (Phase 2 §4.2) already displays a file's own pHash — Phase 3 extends that panel with the similarity group/percentage data once clustering exists.
