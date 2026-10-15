---
name: analyzing-response-structure
description: >-
  Use BEFORE analyzing any captured HTTP response file (JSON, HTML, or minified
  API dump) to learn its data structure and returned fields — when building a
  parser, extractor, or data model, or when the user asks 返回了什么字段 /
  数据结构 / 怎么解析. Bundled offline stdlib script (inspect_sample.py):
  describe prints a bounded path→type→sample map, read slices one subtree —
  each takes one path or a whole path list in one call — a 2 MB capture
  never enters context. Demand-signals: about to guess field
  names or types from experience; only items[0] seen; file too large or
  single-line minified to read whole; HTML may hide embedded JSON
  (__NEXT_DATA__, JSON-LD, window.* assignments); type flips plausible
  (str|null, string|list, "1,299.00"). Delivers a verified field inventory
  without flooding context. Skip for small config files.
compatibility: Any Python 3.9+ on PATH. Offline, read-only, stdlib only.
metadata:
  version: "1.7.1"
  origin: port of viber parse_dev describe_data/read_sample
  interpreter: python, python3, py -3, or user-specified
---

# Analyzing Response Structure

## Overview

Never guess what a response contains — observe it. `inspect_sample.py` reads captured files with two bounded offline operations, each accepting one path or a whole path list: `describe` prints a path→type→sample map; `read` slices one subtree or resolves a path list into values. Map first, slice second, inventory last; even a 2 MB response stays out of context.

## When to Use — run it BEFORE:

- A parser/extraction task (or "what fields does this return?") references a captured JSON/HTML response you have not mapped.
- The file is large or minified single-line JSON — never read it whole.
- About to write field access from experience ("probably `data.list[].title`") instead of observing.
- Only items[0] seen — variance (optional fields, type flips, nulls) hides in later items.
- The capture is HTML — the real data usually lives in embedded JSON `<script>` blocks.

Skip: small config files — a direct read is cheaper.

## Requirements

Python 3.9+ stdlib only; any interpreter (`python`, `python3`, `py -3`, user-specified). On SyntaxError/version errors switch or ask.

## Core Pattern

1. **Map**: `describe` the file; scan `entries`; note `truncated` / `truncation_reason`.
2. **Descend**: truncated, or need one region? `describe --json-path '<subtree>'`.
3. **Slice**: `read --json-path` the first, a middle, and the LAST element (default 2000 chars covers most object slices); diff key sets and types — item[0]-only inventories are guesses. Many fields at once? `read -p '$.a' -p '$.b'` returns path+type+value for a whole list; many subtrees to map? `describe -p ...` does it in one call.
4. **Prove variance** (REQUIRED whenever array length is ≥100 or unknown — describe reports cardinality; none noted means unknown): histogram the UNION of item keys across ALL items with a tiny stdlib script — a field present in even one item must be listed with its count. Cite the printed counts as evidence; "confirmed" without counts is a guess. Never write "verified across all items" without that scan.
5. **Hunt embedded JSON**: HTML `<script>` JSON appears in the map as `#script[0]`, `#script[1]`, … (annotated with id/type). JS-variable assignments (`window.__X__ = {...}`) are NOT addressed — bounded-read that region raw.
6. **Inventory**: report per the contract; every stated path/type comes from an observed slice or the histogram — mark anything else UNVERIFIED, never guess.

## Quick Reference

Single-quote the file path and the whole `--json-path` value (literal in pwsh/bash; protects `$`, `[`, `"`):

```bash
python <skill_dir>/scripts/inspect_sample.py describe '<file>'
python <skill_dir>/scripts/inspect_sample.py describe '<file>' -p '$.data.items' -p '$.data.page_info'
python <skill_dir>/scripts/inspect_sample.py read '<file>' -p '$.data.items[0]'
python <skill_dir>/scripts/inspect_sample.py read '<file>' -p '$.code' -p '$.data.items[0].title' -p '$.data.items[-1].shop_info.seller_name'
python <skill_dir>/scripts/inspect_sample.py read '<file>' --json-path '$#script[0].props' --limit 2000
python <skill_dir>/scripts/inspect_sample.py read '<file>' --paths-file paths.txt
```

Every command takes one path or a whole list: a single `-p/--json-path` returns that one bounded result; several paths (repeatable `-p`, several paths after one `-p`, or `--paths-file`) return per-path results `{path, ok, ...}` in one call — `read` yields `{path, ok, type, value}` (per-value cap 600 chars in list mode, `--limit` overrides), `describe` yields a full bounded map per path. Paths file format:

```text
# one json_path per line — blank lines and # comment lines are skipped; duplicates deduped (first wins)
$.code
$.data.items[0].title
$.data.items[-1].shop_info.seller_name
```

UTF-8 (BOM tolerated). File lines run first, then any `-p` flags. Total output caps at 80K chars — in `read` list mode ok-results beyond the first 30 come back `"omitted": true` with `value: null`; `describe` caps by size, subtrees past the budget return `"omitted": true` with `describe: null` (split the list across calls, don't raise limits).

`<skill_dir>` = this skill's base directory; absolute fallback `C:\Users\Administrator\.agents\skills\analyzing-response-structure\scripts\inspect_sample.py`. Misses return available keys / the list length; `...[truncated]` means go deeper, not bigger limits.

## Output Contract

Field-inventory table `path | type | variance | notes`, grouped: envelope / per-item fields / optional fields / embedded & derived data / pagination mechanics / cross-source mismatches. Variance column states the evidence basis (samples or full histogram). Downstream parser code is written from this table alone.

Decode notation (this skill's own convention, not a standard syntax): for values inside JSON-encoded strings, write the raw path, then one ` -> ` per decode — `$.rc.ee[1].kb -> logistics.carrier` (decode once); `$.rc.ee[7].kb -> note -> inner_code` (decode twice). Arrow count = `json.loads` count.

## Common Mistakes / Red Flags

- Writing a bare `json.load` explorer that re-derives structure the script already maps — your own script is ONLY for math (full-item histograms, counts, cross-checks); every read/slice/map still goes through describe / read.
- Inventory from item[0]/few slices without the histogram — traps live in later items.
- Trusting display formats: `"1,299.00"` is a string; `0/1` may not be a bool; JSON-encoded string fields need a second parse.
- One pagination mechanism reported when a cursor and numeric paging coexist.
- The urge to read the whole file — that's `describe`'s job.
