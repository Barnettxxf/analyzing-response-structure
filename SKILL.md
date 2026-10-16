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
  version: "1.12.0"
  origin: ported from an internal crawler project's parse_dev describe_data/read_sample
  interpreter: python, python3, py -3, or user-specified
---

# Analyzing Response Structure

## Overview

Never guess what a response contains — observe it. `inspect_sample.py` reads captured files with bounded offline operations: `describe` prints a path→type→sample map; `read` slices one subtree or resolves a path list into values; both accept `-p` (JSON paths), `-x` (XPath over HTML — optional parsel dependency), and `read --files` applies one path across a whole glob of files. Map first, slice second, inventory last; even a 2 MB response stays out of context.

## When to Use — run it BEFORE:

- A parser/extraction task (or "what fields does this return?") references a captured JSON/HTML response you have not mapped.
- The file is large or minified single-line JSON — never read it whole.
- About to write field access from experience ("probably `data.list[].title`") instead of observing.
- Only items[0] seen — variance (optional fields, type flips, nulls) hides in later items.
- The capture is HTML — the real data usually lives in embedded JSON `<script>` blocks.

Skip: small config files — a direct read is cheaper.

## Requirements

Python 3.9+ stdlib only; any interpreter (`python`, `python3`, `py -3`, user-specified). On SyntaxError/version errors switch or ask. Exception: the `-x` channel (XPath over HTML) uses the optional `parsel` package — when missing, the command prints the exact install command for the running interpreter; every other operation stays stdlib-only.

## Core Pattern

1. **Map**: `describe` the file; scan `entries`; note `truncated` / `truncation_reason`.
2. **Descend**: truncated, or need one region? `describe --json-path '<subtree>'`.
3. **Slice**: `read --json-path` the first, a middle, and the LAST element (default 2000 chars covers most object slices); diff key sets and types — item[0]-only inventories are guesses. Many fields at once? `read -p '$.a' -p '$.b'` returns path+type+value for a whole list; many subtrees to map? `describe -p ...` does it in one call.
4. **Prove variance** (arrays ≥100): describe now reports `union_keys` (occurrence counts + JSON-type distributions incl. null — heterogeneous arrays like BFF floor lists show every variant), `element_types`, `sampled_indices` and `unsampled_count` — cite these as baseline evidence. For cross-field math (sums, min/max, price×100 comparisons) still run a tiny stdlib script; never write "verified across all items" without either the built-in union or your own scan.
5. **Hunt embedded JSON**: every inline `<script>` is a candidate in the map as `#script[0]`, `#script[1]`, … annotated with id/type/parse status — a `parse_error` candidate (JS assignment, malformed JSON) is a finding, not absence; its raw head is readable at that path. JS-variable assignments still need manual decode of the raw head.
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
python <skill_dir>/scripts/inspect_sample.py read '<file>' -x '//tr[@class="product"]' --attr href
python <skill_dir>/scripts/inspect_sample.py describe '<html>' --scripts
python <skill_dir>/scripts/inspect_sample.py read --files 'captures\detail-*.html' -x '//h1/text()'
```

Every command takes one path or a whole list: a single `-p/--json-path` returns that one bounded result; several paths (repeatable `-p`, several paths after one `-p`, or `--paths-file`) return per-path results `{path, ok, ...}` in one call — `read` yields `{path, ok, type, value}` (per-value cap 600 chars in list mode, `--limit` overrides), `describe` yields a full bounded map per path. Paths file format:

```text
# one json_path per line — blank lines and # comment lines are skipped; duplicates deduped (first wins)
$.code
$.data.items[0].title
$.data.items[-1].shop_info.seller_name
```

UTF-8 (BOM tolerated). File lines run first, then any `-p` flags. Total output caps at 80K chars — in `read` list mode ok-results beyond the first 30 come back `"omitted": true` with `value: null`; `describe` caps by size, subtrees past the budget return `"omitted": true` with `describe: null` (split the list across calls, don't raise limits).

Batch mode: `read --files '<glob>' -p '$.code'` (or one `-x`) applies a single path across every matching file — compact per-file results `{file, ok, type|count, value|error}` in glob-sorted order, same 80K/omitted budget. `--attr name` / `--text` / `--html` work identically in batch (per-file `values` lists, capped at 30 matches/file). Each result carries `file` (relative to the glob root) plus absolute `path`, so same-basename captures never lose identity; `-p` batch mode adds a cross-file `type_summary`. Page-type detection across a mixed batch is one call away (e.g. `-x '//h1/text()'` or `describe --scripts` per pattern).

The `-x` channel runs **XPath** over HTML — server-rendered pages, or an HTML string inside a JSON capture via one `-p` container (`read '<file>' -x '//b' -p '$.html'`). XPath is the only DOM selector offered (parsel/lxml: axes, `contains()`, `text()` nodes — a superset of CSS; **unions (`//a | //b`) are not supported — one expression per call**). Count-first: the envelope reports `count` plus `shown`/`omitted`, and shows the first 30 matches (default blocks carry `tag`/`attrs`/`text`; `--attr name` / `--text` / `--html` switch extraction to value lists; per-value 600 chars, `--limit` overrides). Needs the optional `parsel` package — when missing, the command prints the exact install command for the running interpreter; the `-p`/`#script[n]` JSON modes never need it. Noise control: text-node queries should exclude scripts with `not(ancestor::script)` (e.g. `//td/text()[not(ancestor::script)]`), and on heavy SSR pages (giant `__NEXT_DATA__`-style state) enumerate script blocks first with `describe --scripts` instead of walking the whole map.

`<skill_dir>` = this skill's base directory; absolute fallback `~/.agents/skills/analyzing-response-structure/scripts/inspect_sample.py`. Misses return available keys / the list length; `...[truncated]` means go deeper, not bigger limits; long string samples show `…[snip]…` between head and tail.

## Output Contract

Field-inventory table `path | type | variance | notes`, grouped: envelope / per-item fields / optional fields / embedded & derived data / pagination mechanics / cross-source mismatches. Variance column states the evidence basis (samples or full histogram). Downstream parser code is written from this table alone. Describe output may include `pagination_candidates` — observational matches on key names (`page`/`cursor`/`next`/`offset`/`has_more`/`total`/`token` patterns, capped at 20) with path/type/sample; semantics are NOT inferred, report them as candidates.

Decode notation (this skill's own convention, not a standard syntax): for values inside JSON-encoded strings, write the raw path, then one ` -> ` per decode — `$.rc.ee[1].kb -> logistics.carrier` (decode once); `$.rc.ee[7].kb -> note -> inner_code` (decode twice). Arrow count = `json.loads` count.

## Common Mistakes / Red Flags

- Writing a bare `json.load` explorer that re-derives structure the script already maps — your own script is ONLY for math (full-item histograms, counts, cross-checks); every read/slice/map still goes through describe / read.
- Inventory from item[0]/few slices without the histogram — traps live in later items.
- Trusting display formats: `"1,299.00"` is a string; `0/1` may not be a bool; JSON-encoded string fields need a second parse.
- One pagination mechanism reported when a cursor and numeric paging coexist.
- Multi-file batches: comparing only the first file before generalizing — a batch can mix page kinds (listing vs detail, SSR vs API); check one representative file per distinct page type.
- The urge to read the whole file — that's `describe`'s job.
