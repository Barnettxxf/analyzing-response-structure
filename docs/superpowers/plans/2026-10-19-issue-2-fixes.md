# Issue #2 Correctness and Portability Implementation Plan

> **For agentic workers:** execute task-by-task with test-driven-development. Keep the skill's single-file distribution and existing defaults; do not push or close the issue before independent verification.

**Goal:** Fix all eight issue #2 groups plus the portable recursion-error test, without site-specific rules or capture execution.

**Architecture:** Canonical batch-source identity; a compact-map-specific bounded-output formatter; one token resolver for ordinary paths and wildcard recursion; post-trim accounting for actual emitted results; original-text Unicode match spans; explicit HTML-fragment recognition for script operations.

**Tech Stack:** Python 3.9+ stdlib; optional parsel for XPath only; unittest CLI regressions.

**Spec:** https://github.com/Barnettxxf/analyzing-response-structure/issues/2 and independent reproduction evidence at E:\Temp\dsa-test\ars-issue2-bavws8ba\ISSUE2_ASSESSMENT.md.

## Global Constraints

- Defaults unchanged: describe entry budget 11000; 24K describe / 80K batch caps; read limits clamp 16–20000.
- Offline, read-only input handling; never evaluate capture JavaScript or add a site/framework selector alias.
- Add regression tests and observe failures before implementation; existing contracts stay unless the issue requires their correction.
- No viber application changes; all implementation in this skill repository.
- No new dependency required by JSON/script/grep paths.
- macOS execution must not be claimed without an actual macOS run. Symlink tests may skip on hosts lacking privilege, with a stated reason.

## Review Focus

- Arbitrary file names (`results`, `scripts`, `hits`, `error`, metadata-like names) must remain data, never be interpreted as protocol fields.
- Predicate match counts 0/1/many and wildcard nesting must preserve consistent path semantics and source order.
- Generated paths must round-trip literal `$len`, quotes, brackets and script IDs without reparsing semantic changes.
- Trimming must reconcile emitted counts and truncated-value indexes, even after an error row or the last nested item.
- Relative globs, listed files, symlink aliases and cross-drive paths must retain unique canonical source identity.

---

## Task 1: Source identity and compact output (#1/#2)

**Files:** scripts/inspect_sample.py; tests/test_issue2_output.py.

- [x] Add failing CLI tests: sibling bare-glob same basenames retain both sources, alias-root files keep correct names, reserved filenames never crash, container `--limit` is honored, large and small sibling values coexist.
- [x] Canonicalize source files and display root together; fall back to absolute identity for cross-drive cases and detect compact key collisions before insertion.
- [x] Add dedicated compact formatting, not `_fit_output_budget`'s envelope-row guesses. Small values retain JSON types. Oversized strings/containers become marked previews bounded per value; containers use the normal read JSON-text preview contract. Preserve displayable sibling values.
- [x] Bound full serialized compact output to 80K with explicit omission metadata in a reserved field guaranteed not to overwrite data, or emit bounded guidance with exit 1 only when metadata alone cannot fit. Do not infer status from user-controlled keys.
- [x] Run targeted regressions and existing compact/batch tests.

## Task 2: Shared path semantics (#3/#4/#5)

**Files:** scripts/inspect_sample.py; tests/test_issue2_paths.py.

- [x] Add failing tests for explicit predicate indexing at cardinalities 0/1/many, `[-1]`, out-of-range, described `$len` key round-trip, quoted wildcard keys, escaped script ID + wildcard, predicate + wildcard + json-root, nested projections.
- [x] Resolve parsed tokens directly using a shared resolver rather than a second wildcard-prefix implementation/string re-encoding. Explicit predicate index always acts on the match list, including a single match. No-index unique match still unwraps; zero/multiple misses retain useful guidance.
- [x] Quote reserved `$len` literal field names in generated property paths; preserve key versus operator token identity.
- [x] Keep wildcard suffix miss behavior (`null` slot), script ID/type selection rules, limits and negative indexes.
- [x] Run targeted and all existing path regressions.

## Task 3: Coverage accounting and Unicode grep (#6/#7)

**Files:** scripts/inspect_sample.py; tests/test_issue2_output.py.

- [x] Add failing tests using the exact matrix and grouped-XPath fixtures from issue #2; Unicode `İprice`, multiline and repeated/overlap searches.
- [x] Refresh matrix top-level shown/omitted after trimming, using the documented count of files with emitted path results. Refresh XPath clipped-value indexes/counts after items are removed.
- [x] Use literal case-insensitive matching with spans from the original input, not positions in a length-changing lowercase copy; preserve existing multi-keyword ordering/dedup and non-overlap semantics per keyword.
- [x] Run targeted coverage/grep regression tests.

## Task 4: HTML fragments and portable error tests (#8 and portability)

**Files:** scripts/inspect_sample.py; tests/test_issue2_paths.py; tests/test_v114_contract.py.

- [x] Add failing tests for a top-level script fragment, div-contained scripts, JSON string fields containing fragments, batch summaries, and XPath parity; plain text/non-HTML must still get guidance.
- [x] Recognize legitimate fragments for explicit script discovery/selection without executing scripts. Preserve malformed-script reporting and single-file/batch behavior.
- [x] Replace the assumption that exactly 6000 nested arrays always exceed every interpreter's recursion threshold. Test the error-handling boundary deterministically and separately test that actual deep input either yields a bounded preview or bounded error guidance, never traceback.
- [x] Add canonical temporary-root and real symlink checks where available; retain portable skips where unsupported.

## Task 5: Integration, documentation and independent review

**Files:** SKILL.md, README.md, tests (only affected assertions); implementation file if integration fixes needed.

- [x] Document compact container preview/omission shape and clarified path/coverage behavior, minimally and generically. Keep original methodology intact.
- [x] Run full tests using python, python3 and py -3, reporting actual versions/skips.
- [x] Run Ruff format/check and Pyright. Fix genuine analysis failures; do not disguise a complexity error as a clean typecheck.
- [x] Re-run E:\Temp\dsa-test\issue2_review_probe.py against the fixed build and inspect all original symptom evidence.
- [x] Independent reviewer replays issue fixtures and new combinations; fix findings with tests and re-review when necessary.
- [x] Commit verified fixes; report commit, scope, tests, remaining platform limitations. Remote push/issue closure only after the verification gate and appropriate user direction.
