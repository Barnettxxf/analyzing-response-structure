---
name: analyzing-response-structure
description: >-
  Use before analyzing captured HTTP responses (JSON, HTML or minified API dumps)
  to discover fields, types or parser mappings. Especially relevant when captures
  are large, HTML embeds JSON, arrays may vary, or field names would otherwise be
  guessed. 用户询问返回字段、数据结构、如何解析时使用；小配置文件无需使用。
compatibility: Python 3.9+; offline, read-only. JSON and HTML summaries use stdlib; XPath requires optional parsel.
metadata:
  version: "1.14.0"
  interpreter: python, python3, py -3, or user-specified
---

# Analyzing Response Structure

## Overview

Observe before writing field access. Use the bundled [inspector](scripts/inspect_sample.py) for bounded discovery: **map → descend → slice → inventory**. Large captures are parsed locally, not dumped into model context.

Python 3.9+ is sufficient except for XPath (`-x`), which requires optional `parsel`. A missing dependency prints the exact install command for the running interpreter. Never execute scripts from a captured page.

## Discovery recipe

1. **Map:** `describe` the capture. HTML also gets `html_structure`: tag and tag.class counts, at most 30 groups each, with omitted-group counts. These are structural observations, not semantic labels.
2. **Descend:** choose `-p '<subtree>'`. For a wide object, `describe --keys` lists immediate key names without descending into values; it reports exact `key_count` and any omissions.
3. **Inspect scripts:** on heavy HTML start with `describe --scripts`. It lists non-empty inline script candidates, id/type/parse status and bounded failure previews; ordinary JavaScript can legitimately have `parse_error` because it is not strict JSON.
4. **Sample variance:** arrays scan all immediate keys/types, including null (`union_keys`, `type_counts`, `scanned_count`); values expand first/middle/last subject to budgets. `sampled_indices` names the emitted sample roots, not a full nested scan. Read additional slices when needed; never infer all values from item[0].
5. **Compare files:** batches can mix detail/listing/error pages. Probe page kinds before applying one schema. Once mappings are observed, implement the extractor; use local scripts for aggregates and cross-checks rather than dumping whole captures.

## Quick reference

Quote file paths and `$` paths literally in the shell (examples use pwsh/bash-compatible single quotes).

```bash
python <skill_dir>/scripts/inspect_sample.py describe 'capture.json'
python <skill_dir>/scripts/inspect_sample.py describe 'capture.json' -p '$.data' --keys
python <skill_dir>/scripts/inspect_sample.py read 'capture.json' -p '$.code' -p '$.data.items[-1]'
python <skill_dir>/scripts/inspect_sample.py describe 'page.html' --scripts
python <skill_dir>/scripts/inspect_sample.py read 'page.html' -p '$#script[id="app-state"].props'
python <skill_dir>/scripts/inspect_sample.py read 'page.html' -x '//h1' -x 'count(//tr)' --text
python <skill_dir>/scripts/inspect_sample.py read 'page.html' -x '//a' -x '//img' --html --show 50
python <skill_dir>/scripts/inspect_sample.py read 'capture.json' -p '$.data.html' -x '//li' --text
python <skill_dir>/scripts/inspect_sample.py read --files 'captures/*.html' -x '//h1/text()'
```

### JSON paths and scripts

The inspector uses a small path grammar, not full JSONPath: `$`, `.key`, `[0]`, `[-1]`, `["unusual.key"]`, `['@type']`, `.@type`, and `#script[N]` or `#script[id="..."]`. Both quote styles work in bracket keys and script IDs; quoted brackets and escaped quotes stay inside the name.

Input paths are capped at 2048 characters. `--keys` may enumerate longer names that cannot be addressed through `-p`; use the observed mapping in a local extractor for that edge case.

Discover IDs from `--scripts` (`id_path` is directly usable). ID matching is exact and case-sensitive; duplicate IDs select the **first inline candidate in document order**, even if that candidate fails JSON parsing. This is an explicit inspector rule, not a guarantee of valid HTML. No framework/site aliases are hard-coded. Unnamed scripts remain index-addressable; positions can vary between files.

Single-path `read` returns a bounded slice (strings are raw text). Repeat `-p`, put several paths after one `-p`, or use `--paths-file` for per-path `{path, ok, type, value|error}` results. A UTF-8 paths file has one path per line; blanks and `#` comments are skipped, then `-p` arguments are appended and duplicates removed.

`read --files '<glob>'` applies **one JSON path or one XPath** across sorted matching files. A one-path `--paths-file` works here too. Results carry a per-file label and absolute `path`; missing fields and file-read failures are per-row errors (batch exit 0 preserves other rows). Invalid arguments or a missing XPath dependency are process errors (exit 1 with guidance). `-p` batches include `type_summary` across processed files. Multi-path/file matrices and XPath containers in file-batch mode are not supported.

### XPath results and limits

`read -x` accepts repeated expressions and returns a separate group for each. One `-p` may select an HTML-string container for all expressions. XPath 1.0 unions (`//a | //b`) also work, but merge nodes in document order rather than creating groups.

- Node sets report `count`, `shown`, `omitted`; single default reads show `matches` with tag/attrs/text, grouped reads show `values`.
- Choose **one output mode for the whole call**: `--text`, `--html` (outer markup; requires element selections), or `--attr name`. Use separate calls for different modes. `//a/@href` already selects attribute values; redundant `--attr href` works, but a conflicting attribute request gives retry guidance.
- Scalar XPath expressions such as `count(...)`, `number(...)`, `string(...)` and `boolean(...)` return typed `value` results. XPath 2.0 functions such as `distinct-values()` are not offered; use a small script for unsupported aggregates.
- `--show N` controls displayed node matches: default **30**, positive values clamped to **1–200**. Counts still cover the complete matching set. For later ranges, narrow XPath with positional predicates.
- `--limit N` controls characters **per value**, not matches: default **2000** for a single JSON/raw slice, **600** for lists/XPath; positive values clamp to **16–20000**. Zero, negatives and non-integers are rejected. `...[truncated]` signals a clipped value.
- Single descriptions/script listings cap at **24000** serialized JSON characters; path-list descriptions, grouped reads and file batches cap at **80000**. In path-list descriptions, omission placeholders do not count as `shown` descriptions. These are output characters, not input bytes/tokens. Read `shown`/`omitted` and `truncation_reason`: totals can be correct even when values/groups are omitted. Split requests or narrow selectors when budgets are reached.

Text XPath can accidentally select script bodies: exclude them with `not(ancestor::script)` where appropriate. Mixed-content elements combine labels and child badges; distinguish direct `text()` from the element's full string value using observed markup.

## Output contract

Produce `path | type | variance | notes`, grouped into envelope, item fields, optional fields, embedded/derived data, navigation and cross-source mismatches. Cite the evidence scope: samples, shallow full-array key counts, or full-value scans. Mark unsupported conclusions **UNVERIFIED**. `pagination_candidates` are key-name observations only, not proven navigation semantics.

**Decode notation:** after an executable raw path, one ` -> ` means one **additional `json.loads` on that selected value**. Resolving `$#script[...]` already parses the script JSON; that implicit parse is not an arrow. Example: `$#script[id="app-state"].encoded -> bucket` means one further decode. Nested encoded strings need additional arrows (`$.meta -> note -> code`). Arrows are report notation, not executable `-p` syntax, and never mean “parse HTML”; record HTML composition separately as a JSON container path plus XPath.
