# analyzing-response-structure

A generic agent skill for bounded inspection of captured HTTP responses
(JSON or HTML): map first, slice second, inventory last. Captures are parsed
locally, not dumped into the model's context.

See [SKILL.md](SKILL.md) for the workflow, path grammar and evidence contract.

## Install

The repository root is the skill directory. Requires Python 3.9+; core JSON
inspection and HTML summaries are stdlib-only, offline and read-only.
XPath requires optional `parsel`; a missing dependency prints an install
command for the active interpreter.

| Runtime | Install |
|---|---|
| Claude Code (marketplace) | `/plugin marketplace add Barnettxxf/analyzing-response-structure`, then `/plugin install analyzing-response-structure@analyzing-response-structure` |
| Claude Code (manual) | `git clone https://github.com/Barnettxxf/analyzing-response-structure ~/.claude/skills/analyzing-response-structure` |
| Codex CLI | `git clone https://github.com/Barnettxxf/analyzing-response-structure ~/.agents/skills/analyzing-response-structure` |
| Gemini CLI / Copilot CLI / DSH | Same cross-runtime `~/.agents/skills/` location |

## Usage

```bash
# Bounded recursive map; or immediate key names only.
python scripts/inspect_sample.py describe capture.json
python scripts/inspect_sample.py describe capture.json -p '$.data' --keys

# One slice, multiple paths, or a UTF-8 paths file.
python scripts/inspect_sample.py read capture.json -p '$.data.items[-1]'
python scripts/inspect_sample.py read capture.json -p '$.code' -p '$.data.items[0]'
python scripts/inspect_sample.py read capture.json --paths-file paths.txt

# Array projection: for each element, get the named field.
python scripts/inspect_sample.py read capture.json -p '$.data.items[*].title'

# Compact multi-path output: flat {path: value} without structural envelope.
python scripts/inspect_sample.py read capture.json -p '$.code' -p '$.label' --value-only

# Discover script IDs before using a stable, case-sensitive ID selector.
python scripts/inspect_sample.py describe page.html --scripts
python scripts/inspect_sample.py read page.html -p '$#script[id="app-state"].props'

# Group XPath probes (optional parsel). One output mode applies to all groups.
python scripts/inspect_sample.py read page.html -x '//h1' -x 'count(//tr)' --text
python scripts/inspect_sample.py read page.html -x '//a' --attr href --show 50

# Multi-file probe: one path or XPath across all matching captures.
python scripts/inspect_sample.py read --files 'captures/*.html' -x '//h1/text()'

# Wrap-up: bounded keyword-context search over raw captures (per file or batch).
python scripts/inspect_sample.py read page.html --grep 'kw' --context 100
python scripts/inspect_sample.py read --files 'captures/*.html' --grep 'kw' --grep 'PCN'
python scripts/inspect_sample.py read --files 'captures/*.html' --exclude 'detail-01.html' -p '$.code' -p '$.title'
```

Quote paths literally in your shell. `--limit` caps characters **per value**
(default 2000 for one raw/JSON slice; 600 for path lists/XPath; clamp 16–20000).
`--show` caps displayed XPath matches (default 30; clamp 1–200), not total counts.
Descriptions cap at 24000 output characters; grouped reads/file batches at
80000 serialized JSON characters. Check omission metadata and split requests
when a budget is reached. Input size and tokens are not these budgets.

HTML `describe` includes bounded tag/class histograms. Arrays expose shallow
union-key counts, JSON type frequencies including null, and first/middle/last
value samples with actual emitted sample indices. Neither is a complete
semantic/value scan.

## Decode notation

A ` -> ` means one additional `json.loads` **after resolving the raw path**.
Script selectors already parse script JSON, so
`$#script[id="app-state"].encoded -> bucket` has one further decode.
Arrows are report notation, not executable path syntax, and do not denote HTML
parsing; HTML-in-JSON uses a container path plus XPath.

## Tests

```bash
python -m unittest discover -s tests
```

154 behavioral regression cases at this revision. XPath cases skip explicitly
when `parsel` is absent; dependency guidance is exercised on an interpreter
without it. Tests spawn the real CLI and assert values, exit codes, coverage
metadata and output budgets. Synthetic fixtures stay small and portable;
external real-capture field tests are reported separately, not treated as gold
truth merely because two model outputs agree.

## Version

v1.16.0: everything in v1.15 — stable script-ID paths, quoted special keys,
safe attribute extraction, grouped XPath, keys-only maps, HTML structure
counts, keyword-context search, multi-keyword grep, batch multi-path
matrix with cross-file type summaries, and --exclude — plus [*] wildcard
array projection. No site-specific selectors or framework aliases.

## License

Dual-licensed under [MIT](LICENSE-MIT) or [Apache-2.0](LICENSE-APACHE).
