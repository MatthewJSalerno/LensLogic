# Functional & Technical Design Specification: NegativeSpace Phase 3 — Fuzzy Matching & EXIF Editing

## 1. Scope

Phase 3 covers near-duplicate (visually similar, not byte-identical) detection and grouping, plus EXIF metadata editing/synchronization across related images. This content was originally drafted as part of `phase2-spec.md` §4.3 and was moved here since Phase 2's scope is explicitly the web UI for existing Move/Copy/Index operations — fuzzy matching is a separate body of work with its own data-model and UX implications, not a natural fit alongside Phase 2's file-selection/job-management focus.

Phase 1 already generates and stores a pHash (`phash` column on `photos`) for every file — that work is done and is a prerequisite for everything below. What's *not* built yet is anything that acts on those hashes: comparing them to each other, grouping visually similar images, or surfacing that grouping in the UI.

## 2. Match Mode Switcher (Exact vs. Fuzzy)

The top header provides a Match Mode toggle:
* **Exact SHA-1 (Default):** 100% byte-for-byte checksum matching for operations — this is what Phase 1/2 already implement.
* **Fuzzy Match (pHash):** Perceptual hash visual similarity grouping based on Hamming distance thresholds. Displays similarity percentage scores in the inspector. Groundwork for the EXIF editing capabilities below.

## 3. Open Design Questions for Phase 3

These weren't resolved in the original draft and need scoping before implementation:

* **Hamming distance threshold:** what similarity score counts as "a match" for grouping purposes? Likely needs to be user-adjustable rather than fixed, given pHash similarity is inherently fuzzy and different collections (bursts of near-identical shots vs. edited variants of one photo) may want different sensitivity.
* **`collision_group` / `is_master` semantics:** these columns already exist on `photos` (added in Phase 1, unused until now). Need to define: how is a `collision_group` ID assigned (on-demand when the UI requests clustering, or continuously during every Index)? Who/what decides `is_master` — first-discovered, highest-resolution, user-selected?
* **Merger tool UX:** the original Phase 1/2 spec drafts mention a "Merger" tool for resolving fuzzy-match groups, without detail. Needs its own design pass — likely: show the group, let the user pick a master, decide what happens to the others (delete? keep both? merge metadata?).
* **EXIF editing/synchronization:** "groundwork for future EXIF editing capabilities" was mentioned but not specified — does this mean editing a single file's metadata, or propagating metadata from a group's master to its near-duplicates? These are different features with different risk profiles (the latter mutates files that weren't directly selected by the user).

## 4. Dependencies on Phase 1/2

* Requires the `phash` values Phase 1 already computes — no new hashing work needed, just new logic that reads and compares existing values.
* Likely requires a new backend endpoint (e.g. `GET /api/v1/photos/similar/{id}?threshold=N`) computing Hamming distance across the `photos` table's `phash` column — this is a Node.js/API-layer concern, not a `ns-engine.py` engine change, unless clustering needs to happen at index time rather than on-demand.
* The Inspector panel (Phase 2 §4.2) already displays a file's own pHash — Phase 3 extends that panel with the similarity group/percentage data once clustering exists.
