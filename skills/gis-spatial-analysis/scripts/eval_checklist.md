# Eval Checklist — gis-spatial-analysis

Three separate questions, tested separately. A skill can fail any one of them
independently of the other two — perfect content that never loads, or reliable
triggering into instructions that get ignored, or clean triggering+steering on
content that's silently wrong. Run all three; passing one doesn't imply the others.

## 1. Content-level checks (cheap — run these first, before anything else)

```bash
# Broken references: every path SKILL.md mentions should exist on disk
grep -oE '(references|scripts)/[a-zA-Z0-9_.-]+\.(md|py)' SKILL.md | sort -u > /tmp/referenced.txt
find references scripts -type f | sort > /tmp/existing.txt
diff /tmp/referenced.txt /tmp/existing.txt   # should be empty

# Script syntax (catches typos without needing geopandas/pyproj installed)
python3 -m py_compile scripts/test_crs_transforms.py
python3 -m py_compile scripts/workflow_tanam_pastian.py

# The load-bearing correctness check — run in a real environment with pyproj installed
python3 scripts/test_crs_transforms.py
```

Status as of last audit: references clean (no broken links), both scripts compile,
18 of the table's CRS entries have test coverage (all state Cassini grids + the
three main RSO/Hotine grids: 3375, 3376, 3168, 29873). Not covered: chains/feet unit
variants (3167, 29871, 29872) — deliberately excluded rather than guessing at a unit
conversion factor.

## 2. Trigger testing — does the skill fire when it should, stay quiet when it shouldn't?

Run each prompt in a **fresh conversation**, ~3 times each (triggering is
probabilistic). Watch whether SKILL.md gets read before the answer is written —
visible directly in Claude Code's tool calls, visible in the transcript in Claude.ai.

**Should trigger — explicit:**
- "Convert these Kertau coordinates to GDM2000, data is in EPSG:3168"
- "Write a PostGIS query for parcels within 300m of this drainage reserve"
- "Which state Cassini grid should I use for a Terengganu dataset?"

**Should trigger — implicit (no GIS keywords; the hard cases a weak description misses):**
- "I've got a shapefile of old survey marks and a new resurvey CSV, how do I check if the marks moved too much?"
- "These GPS points came from a handheld unit, how do I get them onto our GDM2000 base map?"
- "Which kampung boundaries fall within 500m of this pipeline route, data is in EPSG:3168"

**Should NOT trigger (negative space — a skill firing on everything is as broken as one that never fires):**
- "What's a GiST index?"
- "Explain the difference between vector and raster data"
- "Style this map layer"

A skill that fires reliably on explicit prompts but inconsistently on implicit ones
has a **description** problem — fix by adding the missing phrasings to SKILL.md's
frontmatter description, not by editing the body. Keep test prompts substantive;
trivially-answerable questions get answered from general knowledge regardless of
description quality, which isn't a real trigger failure.

## 3. Steering testing — once triggered, does the output actually follow the skill's rules?

Concrete, checkable assertions — each one maps to a specific rule stated in
SKILL.md or the reference files, not a generic best practice. Delete an assertion
if baseline Claude (no skill) already passes it 100% of the time; it isn't
measuring the skill.

- [ ] Every generated query specifies SRID/CRS explicitly — no bare geometry ops, no implicit default assumed
- [ ] Proximity queries use `ST_DWithin`, never `ST_Distance(...) < d` (see postgis_patterns.md)
- [ ] Given ambiguous-datum input (region stated but not which grid/era), Claude asks which datum rather than guessing — this is the core behavior the malaysian_crs.md pitfalls section exists to enforce
- [ ] Given a lat/lon buffer request, Claude does not buffer in degrees — either transforms to a projected CRS first or explicitly uses geography type with the tradeoff noted
- [ ] A cadastral/precision-sounding question (tolerance, refixing, boundary marks) triggers a flag about JUPEM/PUK regulatory tolerances rather than silently applying generic GIS defaults
- [ ] When a Malaysian CRS parameter is needed, Claude reads malaysian_crs.md rather than stating a parameter from memory — should be visible as a file read in the transcript
- [ ] Response pairs code with concise spatial reasoning (what it does, assumptions, what to sanity-check) rather than handing back bare code

## 4. Baseline comparison — the most convincing single test

Run the same prompt with and without the skill installed, diff the outputs. The
skill is decoration if they're indistinguishable. Expected visible differences for
this skill specifically:
- Asking about datum/era when a Malaysian CRS is mentioned but underspecified
- Refusing to state Kertau/GDM2000 parameters from memory, reading the reference table instead
- The cadastral-tolerance flag on precision-sounding questions — this is a good
  canary because no generic model behavior produces it unprompted

## Log

- First audit (this file's creation): content-level checks passed after fixing a
  real gap — EPSG:3168 was referenced in the pitfalls section but had no actual
  parameters in the table. Test coverage extended from 4 to 18 grids. Trigger and
  steering testing not yet run — needs fresh conversations, tracked as open.
