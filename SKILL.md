---
name: analyzing-response-structure
description: >-
  Use BEFORE analyzing any captured HTTP response (JSON/HTML/minified API
  dump) to learn its data structure and returned fields — when building a
  parser, extractor, or data model, when the user asks what fields a source
  returns or how to parse it, or when the user mentions data-source/API
  integration — inspect the real captured response before writing
  integration code. Offline stdlib script inspect_sample.py: describe
  prints a bounded path→type→sample map, read slices or batches paths.
  Demand-signals: about to guess fields from experience; only items[0]
  seen; file too large or minified to read whole; HTML may hide embedded
  JSON (__NEXT_DATA__, JSON-LD, window.*); type flips plausible. Verified
  field inventory. Skip for small config files.
compatibility: Python 3.9+. Offline, read-only; XPath needs optional parsel.
metadata:
  version: "1.15.0"
  interpreter: python, python3, or py -3
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
- A data-source/API integration task — map the real captured response before writing any client code or field mapping.
- No captures yet, but the source is reachable? Download a few sample responses to local files first (one file per endpoint/page state, and vary the sampled item so variants show up) — then analyze them with this skill; batch mode compares shapes across samples in one call.

Skip: small config files — a direct read is cheaper.

## Requirements

Python 3.9+ stdlib only; any interpreter (`python`, `python3`, `py -3`, user-specified). On SyntaxError/version errors switch or ask. Exception: the `-x` channel (XPath over HTML) uses the optional `parsel` package — when missing, the command prints the exact install command for the running interpreter; every other operation stays stdlib-only.

## Core Pattern

1. **Map**: `describe` the file; scan `entries`; note `truncated` / `truncation_reason`.
2. **Descend**: truncated, or need one region? `describe --json-path '<subtree>'`.
3. **Slice**: `read --json-path` the first, a middle, and the LAST element (default 2000 chars covers most object slices); diff key sets and types — item[0]-only inventories are guesses. Many fields at once? `read -p '$.a' -p '$.b'` returns path+type+value for a whole list; many subtrees to map? `describe -p ...` does it in one call.
4. **Prove variance** (arrays ≥100): describe now reports `union_keys` (occurrence counts + JSON-type distributions incl. null — heterogeneous arrays like page-section lists where every section has a different schema show every variant), `element_types`, `sampled_indices` and `unsampled_count` — cite these as baseline evidence. For cross-field math (sums, min/max, unit conversions) still run a tiny stdlib script; never write "verified across all items" without either the built-in union or your own scan.
5. **Hunt embedded JSON**: every inline `<script>` is a candidate in the map as `#script[0]`, `#script[1]`, … annotated with id/type/parse status — a `parse_error` candidate (JS assignment, malformed JSON) is a finding, not absence; its raw head is readable at that path. JS-variable assignments still need manual decode of the raw head.
6. **Survey shapes across files before writing the extractor**: when several captures of the same kind exist, run the batch matrix (`read --files ... --paths-file regions.txt`) over the key regions — per-path cross-file `type_summaries` expose shape variance (a cell that is a plain URL string in some captures and a nested object/array of URLs in others, etc.). Write the extractor from the union of observed shapes; extracting from one file's shape silently drops the variants that live in the others.
7. **Inventory**: report per the contract; every stated path/type comes from an observed slice or the histogram — mark anything else UNVERIFIED, never guess.

## Quick Reference

Single-quote the file path and the whole `--json-path` value (literal in pwsh/bash; protects `$`, `[`, `"`):

```bash
python <skill_dir>/scripts/inspect_sample.py describe '<file>'
python <skill_dir>/scripts/inspect_sample.py describe '<file>' -p '$.data.items' -p '$.data.page_info'
python <skill_dir>/scripts/inspect_sample.py read '<file>' -p '$.data.items[0]'
python <skill_dir>/scripts/inspect_sample.py read '<file>' -p '$.code' -p '$.data.items[0].title' -p '$.data.items[-1].shop.name'
python <skill_dir>/scripts/inspect_sample.py read '<file>' --json-path '$#script[0].props' --limit 2000
python <skill_dir>/scripts/inspect_sample.py read '<file>' --paths-file paths.txt
python <skill_dir>/scripts/inspect_sample.py read '<file>' -x '//tr[@class="product"]' --attr href
python <skill_dir>/scripts/inspect_sample.py describe '<html>' --scripts
python <skill_dir>/scripts/inspect_sample.py read --files 'captures\detail-*.html' -x '//h1/text()'
python <skill_dir>/scripts/inspect_sample.py read '<file>' --grep 'kw1' --grep 'kw2' --context 100
python <skill_dir>/scripts/inspect_sample.py read --files 'captures\detail-*.html' -p '$.code' -p '$.data.total'
python <skill_dir>/scripts/inspect_sample.py read --files 'captures\detail-*.html' --exclude 'index.html' -p '$.code'
python <skill_dir>/scripts/inspect_sample.py read '<file>' -p '$.code' -p '$.label' --value-only
```

Every command takes one path or a whole list: a single `-p/--json-path` returns that one bounded result; several paths (repeatable `-p`, several paths after one `-p`, or `--paths-file`) return per-path results `{path, ok, ...}` in one call — `read` yields `{path, ok, type, value}` (per-value cap 600 chars in list mode, `--limit` overrides), `describe` yields a full bounded map per path. Paths file format:

```text
# one json_path per line — blank lines and # comment lines are skipped; duplicates deduped (first wins)
$.code
$.data.items[0].title
$.data.items[-1].shop.name
```

UTF-8 (BOM tolerated). File lines run first, then any `-p` flags. Total output caps at 80K chars — `read` list mode reports displayed/omitted path results in `shown`/`omitted`; `describe` caps by size, subtrees past the budget return `"omitted": true` with `describe: null` placeholders when those fit (split the list across calls, don't raise limits).

Batch mode: `read --files '<glob or literal path>' -p '$.code'` (or one `-x`) applies a single path across every matching file — compact per-file results `{file, ok, type|count, value|error}` in glob-sorted order, same 80K/omitted budget. `--attr name` / `--text` / `--html` work identically in batch (per-file `values` lists, default 30 matches/file; configurable via `--show`). Each result carries `file` (relative to the glob root) plus absolute `path`, so same-basename captures never lose identity; `-p` batch mode adds a cross-file `type_summary`. Page-type detection across a mixed batch is one call away (e.g. `-x '//h1/text()'` or `describe --scripts` per pattern). `--files` accepts literal file paths as well as globs (no glob required); `--exclude '<name>'` (repeatable) drops matching files after collection. Multiple `-p` paths (or a multi-line `--paths-file`) turn batch mode into a matrix: nested per-file results `{file, path, shown, omitted, results: [{path, ok, type, value|error}]}` plus per-path cross-file `type_summaries` — surface shape variance (string vs object/array cells) before writing the extractor.

The `-x` channel runs **XPath** over HTML — server-rendered pages, or an HTML string inside a JSON capture via one `-p` container (`read '<file>' -x '//b' -p '$.html'`). XPath is the only DOM selector offered (parsel/lxml: axes, `contains()`, `text()` nodes — a superset of CSS; **unions (`//a | //b`) merge nodes in document order; repeat `-x` for separate grouped results**). Count-first: the envelope reports `count` plus `shown`/`omitted`, and shows the first 30 matches by default (default blocks carry `tag`/`attrs`/`text`; `--attr name` / `--text` / `--html` switch extraction to value lists; per-value 600 chars, `--limit` overrides). Needs the optional `parsel` package — when missing, the command prints the exact install command for the running interpreter; the `-p`/`#script[n]` JSON modes never need it. Noise control: text-node queries should exclude scripts with `not(ancestor::script)` (e.g. `//td/text()[not(ancestor::script)]`), and on heavy SSR pages (giant `__NEXT_DATA__`-style state) enumerate script blocks first with `describe --scripts` instead of walking the whole map.

`<skill_dir>` = this skill's base directory; absolute fallback `~/.agents/skills/analyzing-response-structure/scripts/inspect_sample.py`. Misses return available keys / the list length; `...[truncated]` means go deeper, not bigger limits; long string samples show `…[snip]…` between head and tail.

## Output Contract

Field-inventory table `path | type | variance | notes`, grouped: envelope / per-item fields / optional fields / embedded & derived data / pagination mechanics / cross-source mismatches. Variance column states the evidence basis (samples or full histogram). Downstream parser code is written from this table alone. Describe output may include `pagination_candidates` — observational matches on key names (`page`/`cursor`/`next`/`offset`/`has_more`/`total`/`token` patterns, capped at 20) with path/type/sample; semantics are NOT inferred, report them as candidates.

Decode notation (this skill's own convention, not a standard syntax): for values inside JSON-encoded strings, write the raw path, then one ` -> ` per decode — `$.rc.ee[1].kb -> logistics.carrier` (decode once); `$.rc.ee[7].kb -> note -> inner_code` (decode twice). Arrow count = `json.loads` count.

The count starts **after resolving the selected raw path**: file parsing and the script's implicit JSON parse in `$#script[...]` do not add arrows. For example, `$#script[id="app-state"].encoded -> bucket` requires one additional `json.loads` on `encoded`. Arrows are report notation, not executable `-p` syntax, and do not mean HTML parsing; record HTML composition as a container path plus XPath.

## Common Mistakes / Red Flags

- Writing a bare `json.load` explorer that re-derives structure the script already maps — your own script is ONLY for math (full-item histograms, counts, cross-checks); every read/slice/map still goes through describe / read.
- Inventory from item[0]/few slices without the histogram — traps live in later items.
- Trusting display formats: `"1,234.56"` is a string; `0/1` may not be a bool; JSON-encoded string fields need a second parse.
- One pagination mechanism reported when a cursor and numeric paging coexist.
- Multi-file batches: comparing only the first file before generalizing — a batch can mix page kinds (listing vs detail, SSR vs API); check one representative file per distinct page type.
- The urge to read the whole file — that's `describe`'s job.
- Concluding null/missing without positive search coverage — before concluding a field is absent: search synonyms and variants of the evidence wording (the displayed phrase often differs from the literal field name), inspect the surroundings of the related value (its table headers, labels, adjacent notes), not just the field name itself, and state which regions were searched and which were not. "My search found nothing" is not evidence that the page lacks the value.

## Additional Commands and Contracts (v1.13–v1.14)

These additions extend the commands; they do not replace the Core Pattern or the read/slice/map discipline above.

```bash
python <skill_dir>/scripts/inspect_sample.py describe '<file>' -p '$.data' --keys
python <skill_dir>/scripts/inspect_sample.py read '<html>' -p '$#script[id="app-state"].props'
python <skill_dir>/scripts/inspect_sample.py read --files 'captures/*.html' -p '$#script[id="app-state"].props.count'
python <skill_dir>/scripts/inspect_sample.py read '<html>' -x '//h1' -x 'count(//tr)' --text
python <skill_dir>/scripts/inspect_sample.py read '<html>' -x '//a' --html --show 50
python <skill_dir>/scripts/inspect_sample.py read '<html>' --grep 'kw' --context 100
python <skill_dir>/scripts/inspect_sample.py read '<html>' --grep 'kw1' --grep 'kw2' --context 100
python <skill_dir>/scripts/inspect_sample.py read --files 'captures/*.html' --grep 'kw' --show 5
python <skill_dir>/scripts/inspect_sample.py read --files 'captures/*.html' -p '$.code' -p '$.title'
python <skill_dir>/scripts/inspect_sample.py read --files 'captures/*.html' --exclude 'index.html' -p '$.code'
```

- **Keyword wrap-up search:** `--grep 'kw'` is a case-insensitive literal search over the same raw text view plain `read` prints — script/JSON content is included, so hits need surrounding-text judgment before becoming evidence. Repeat `--grep` for multiple keywords: hits merge in document order, each tagged with its `keyword`, and per-keyword `keywords` subtotals are reported (literal matching — `a|b` alternation is not regex; pass repeated `--grep` instead). Each hit carries a 0-based character `offset` into that view, `line`, and bounded `context` (`--context N` characters per side, default 80, clamp 0–1000). `--show` caps displayed hits (default 30, clamp 1–200); `count` always covers the full match set. Works per file or with `--files` for the wrap-up pass over unconfirmed fields; pair with the red-flag discipline above — synonyms/variants (the displayed wording often differs from the field-style shorthand: percentages inserted mid-phrase, different term order, or a different word for the same concept), surrounding labels and headers, searched-vs-unsearched disclosure — and re-verify promising hits against the DOM/JSON before recording them as evidence.

- **Stable script paths:** use an observed `script_id` from `describe --scripts`; its `id_path` can be passed directly to `read -p`. `$#script[id="..."]` matches any exact, case-sensitive HTML id, with the first inline candidate in document order winning even when malformed. No site/framework aliases. Unnamed scripts remain index-addressable. Ordinary JavaScript can have `parse_error` because it is not strict JSON; it is not proof of absent data. Never execute captured scripts.
- **Keys and structure:** `describe --keys` lists immediate object key names without descending; `key_count`, `shown` and `omitted_count` disclose coverage. It combines with `-p` (single or multiple) and `--paths-file`: `describe '<file>' --keys -p '$.a' -p '$.b'` returns one key-name list per path in a single call — the cheap way to survey wide subtrees before descending. HTML `describe` adds bounded tag and tag.class histograms (at most 30 groups each with omission counts). Array `type_counts`/`scanned_count` cover the shallow key/type scan; `sampled_indices` names emitted first/middle/last roots, not all nested values. Path input is capped at 2048 characters; longer enumerated names may not be addressable via `-p`.
- **Special keys:** both `["@type"]` and `['@type']` work, as does `.@type`; quoted brackets and escaped quotes remain part of a key or script id.
- **Grouped/scalar XPath:** repeated `-x` expressions return separate groups. Node-set groups carry `count`/`shown`/`omitted`/`values`; scalar `count()`, `number()`, `string()` and `boolean()` return typed `value`. XPath 2.0 functions such as `distinct-values()` are not offered; use a script for unsupported math.
- **Output modes:** choose one of `--text`, `--html`, `--attr` for the entire grouped call; use separate calls for different modes. `--html` requires element matches. An attribute XPath already extracts values: `//a/@href` with redundant `--attr href` works; conflicting attribute/text requests return retry guidance.
- **Limits:** `--show N` controls matches, default 30, positive values clamp to 1–200. `--limit N` controls characters per value, default 2000 for one raw/JSON slice or 600 for lists/XPath, positive values clamp to 16–20000. Zero/negative/non-integer values are rejected. Single descriptions/script listings cap at 24K serialized JSON characters; path-list descriptions, grouped reads and file batches at 80K. These are output characters, not bytes/tokens. Omission placeholders do not count as shown descriptions; truncated-value counts only cover emitted values.
- **File batches:** accept one XPath, one JSON path (compact flat rows), or multiple JSON paths (nested per-file matrix with per-path `type_summaries`); a `--paths-file` works in all three forms. Missing fields/file-read errors are per-row failures with exit 0, preserving other rows. Invalid arguments or missing XPath dependency are process errors with exit 1. XPath containers (a `-p` selecting HTML inside each file) are not supported in file-batch mode — extract the HTML strings first, then run grouped `-x` per file.
