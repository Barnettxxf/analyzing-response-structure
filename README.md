# analyzing-response-structure

An agent skill for bounded structure inspection of captured HTTP responses
(JSON, HTML, or minified API dumps) — map first, slice second, inventory last,
so even a 2 MB capture stays out of the model's context window.

See [SKILL.md](SKILL.md) for the full method and output contract.

## Install

Copy this directory into your runtime's skills directory, e.g.
`~/.agents/skills/analyzing-response-structure/`. Requires Python 3.9+
(stdlib only, offline, read-only).

The repo root **is** the skill directory (`SKILL.md` at root), so cloning
straight into the skills directory works everywhere:

| Runtime | Install |
|---|---|
| Claude Code (marketplace) | `/plugin marketplace add Barnettxxf/analyzing-response-structure` then `/plugin install analyzing-response-structure@analyzing-response-structure` |
| Claude Code (manual) | `git clone https://github.com/Barnettxxf/analyzing-response-structure ~/.claude/skills/analyzing-response-structure` |
| Codex CLI | `git clone https://github.com/Barnettxxf/analyzing-response-structure ~/.agents/skills/analyzing-response-structure` |
| Gemini CLI / Copilot CLI | same as Codex (`~/.agents/skills/` is the cross-runtime alias) |
| DSH (DeepSeek Harness) | same as Codex — `~/.agents/skills/` is scanned live, no restart needed |

## Usage

```bash
# bounded path->type->sample map (whole file)
python scripts/inspect_sample.py describe capture.json

# one subtree, or a whole path list (values for each path)
python scripts/inspect_sample.py describe capture.json -p '$.data.items'
python scripts/inspect_sample.py read capture.json -p '$.code' -p '$.data.items[0].title'

# bulk form of the same path list (# comments and blank lines allowed)
python scripts/inspect_sample.py read capture.json --paths-file paths.txt

# HTML: JSON-bearing <script> blocks appear as $#script[0], $#script[1], ...
python scripts/inspect_sample.py read page.html -p '$#script[0].props'

# XPath over HTML (server-rendered pages; needs optional `parsel` —
# the command prints its exact install command when missing)
python scripts/inspect_sample.py read page.html -x '//table[@class="product"]//tr' --attr href
```

Single quotes protect `$`, `[`, and `"` in pwsh/bash. List mode caps per-value
output (600 chars default, `--limit` overrides) and total output at 80K chars
(past it, results come back `"omitted": true` — split the list, don't raise
limits).

## Decode notation

Values inside JSON-encoded strings use the skill's own convention: raw path,
then one ` -> ` per `json.loads` — `$.rc.ee[1].kb -> logistics.carrier`
(decode once); `$.rc.ee[7].kb -> note -> inner_code` (decode twice).

## Tests

```bash
python tests/test_inspect_sample.py
```

48 cases, any interpreter (Python 3.9+; `-x` cases auto-skip when the
optional `parsel` package is absent): every case spawns the script as a
subprocess and asserts the agent-facing contract — bounded outputs,
guidance-not-traceback errors, exit codes, embedded-JSON extraction,
per-path batch semantics, and XPath over HTML.

## Status

v1.9.1. Evolved v1.5.0 → v1.9.1 through a multi-model benchmark
(strong/weak tiers, natural vs explicit skill triggering); every iteration
was driven by an observed failure mode with a regression guard.

## License

Dual-licensed under [MIT](LICENSE-MIT) or [Apache-2.0](LICENSE-APACHE) —
choose either at your option.
