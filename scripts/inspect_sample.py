#!/usr/bin/env python3
"""inspect_sample.py — bounded structure inspection for captured response files.

Standalone port of ``describe_data`` / ``read_sample`` from viber's
``crawl_runner/parse_dev.py`` (deep-digest + json_path addressing, 2026-10-12),
stripped of spider/engine dependencies for use as a personal agent skill.

Dependencies: Python 3.9+ standard library ONLY. Nothing to install.
Read-only, offline, stateless.

Intentional deltas vs the parsel-based original (documented; do NOT "fix"):
- Embedded JSON <script> discovery uses stdlib ``html.parser`` instead of
  parsel/lxml:
    * A <script> inside an HTML comment is NOT extracted (tag events are never
      fired inside comments) — same net behavior as lxml.
    * An unclosed <script> swallows the rest of the file as its body (CDATA
      mode); its JSON parse then fails and it is skipped with a note — same as
      a browser would treat it.
    * ``</script>`` inside a JSON string literal terminates the script early —
      identical to browsers and to the parsel original.
- CSS selector mode is intentionally NOT ported. DOM fragments are the agent's
  job via its own bounded read/grep tools; json_path addressing covers every
  structure-inspection need ($ .key  [0]  ["quoted key"]  #script[n]).
- Error/truncation markers are English ("...[truncated]") instead of Chinese.
- Input files are decoded as utf-8-sig with errors="replace" so GBK/BOM-mangled
  captures still describe (with replacement chars) instead of crashing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
SCRIPT_MARKER = "#script["
MAX_DIGEST_KEYS = 50
MAX_DIGEST_ENTRIES = 200
MAX_DIGEST_WIDTH = 50
MAX_DIGEST_DEPTH = 16
MAX_LEAF_SAMPLE = 80
MAX_DIGEST_OUTPUT_CHARS = 24_000
MAX_READ_SAMPLE_CHARS = 20_000
DIGEST_ENTRY_BUDGET = 11_000
DEFAULT_READ_LIMIT = 2000
BATCH_VALUE_LIMIT = 600
BATCH_TOTAL_CHARS = 80_000

_PARSE_FAILED = object()


# --- sample loading ------------------------------------------------------------


def _read_file_text(sample_path: str | Path) -> str:
    raw = Path(sample_path).read_bytes()
    return raw.decode("utf-8-sig", errors="replace")


def _load_sample(sample_path: str | Path) -> Any:
    """Read the file; JSON parses, everything else (raw HTML, GBK bytes, ...)
    degrades to replacement-char text — never raises on content."""
    raw = _read_file_text(sample_path)
    try:
        return json.loads(raw)
    except ValueError:
        return raw  # non-JSON sample (raw HTML / log line / ...) by design


def _looks_like_html(text: str) -> bool:
    head = text[:512].lstrip("\ufeff \t\r\n").lower()
    return (
        head.startswith("<!doctype html")
        or head.startswith("<html")
        or head.startswith("<!--")
        and ("<html" in head or "<!doctype" in head)
    )


class _ScriptCollector(HTMLParser):
    """Collect JSON-bearing <script> elements in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.scripts: list[dict[str, Any]] = []
        self._capturing = False
        self._chunks: list[str] = []
        self._meta: tuple[str | None, str | None] = (None, None)

    def _wants(self, attrs: list[tuple[str, str | None]]) -> bool:
        attrd = {str(k).lower(): str(v or "") for k, v in attrs}
        sid = attrd.get("id")
        stype = attrd.get("type", "").split(";", 1)[0].strip().lower()
        if sid == "__NEXT_DATA__" or stype == "application/json" or stype.endswith("+json"):
            self._meta = (sid, stype or None)
            return True
        return False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and not self._capturing and self._wants(attrs):
            self._capturing = True
            self._chunks = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._capturing:
            self._capturing = False
            try:
                value = json.loads("".join(self._chunks))
            except (TypeError, ValueError):
                return  # malformed embedded JSON: skip with a note, by design
            self.scripts.append({"id": self._meta[0], "type": self._meta[1], "value": value})

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._chunks.append(data)


def _embedded_json_scripts(html: str) -> list[dict[str, Any]]:
    collector = _ScriptCollector()
    collector.feed(html)
    collector.close()
    return collector.scripts


# --- json_path grammar ----------------------------------------------------------


def _tokenize_json_path(json_path: str) -> list[tuple[str, Any]] | str:
    """Small path grammar: ``$``, ``.identifier``, ``[integer]``, ``[JSON-string]``, ``#script[n]``."""
    if not isinstance(json_path, str) or not json_path.startswith("$"):
        return "path must start with $"
    if len(json_path) > 2048:
        return "path too long; use a bounded path from describe"
    digit_index = re.compile(r"[0-9]{1,18}\Z")
    tokens: list[tuple[str, Any]] = []
    index = 1
    while index < len(json_path):
        if json_path.startswith(SCRIPT_MARKER, index):
            start = index + len(SCRIPT_MARKER)
            end = json_path.find("]", start)
            raw = json_path[start:end] if end >= 0 else ""
            if end < 0 or not digit_index.fullmatch(raw):
                return "script selector must be #script[non-negative integer]"
            tokens.append(("script", int(raw)))
            index = end + 1
            continue
        if json_path[index] == ".":
            start = index + 1
            end = start
            while end < len(json_path) and json_path[end] not in ".[":
                if json_path.startswith(SCRIPT_MARKER, end):
                    break
                end += 1
            key = json_path[start:end]
            if not key or not IDENTIFIER_RE.fullmatch(key):
                return f"invalid property segment: {json_path[start:end]!r}"
            tokens.append(("key", key))
            index = end
            continue
        if json_path[index] == "[":
            end = index + 1
            quoted = False
            escaped = False
            while end < len(json_path):
                char = json_path[end]
                if escaped:
                    escaped = False
                elif char == "\\" and quoted:
                    escaped = True
                elif char == '"':
                    quoted = not quoted
                elif char == "]" and not quoted:
                    break
                end += 1
            if end >= len(json_path):
                return "unclosed [] path segment"
            raw = json_path[index + 1 : end]
            if digit_index.fullmatch(raw):
                tokens.append(("index", int(raw)))
            else:
                try:
                    key = json.loads(raw)
                except (TypeError, ValueError):
                    return "[] accepts a non-negative integer index or a JSON double-quoted string key"
                if not isinstance(key, str):
                    return "object key must be a JSON string"
                tokens.append(("key", key))
            index = end + 1
            continue
        return f"invalid path at position {index}"
    return tokens


def _bounded_text(text: str, limit: int, *, truncated: bool = False) -> str:
    if len(text) <= limit and not truncated:
        return text
    if limit <= 1:
        return text[:limit]
    marker = "...[truncated]"
    return text[: max(0, limit - len(marker))] + marker[:limit]


def _bounded_json_text(value: Any, limit: int) -> str:
    """Incrementally serialize at most limit characters, never materialize huge JSON text."""
    encoder = json.JSONEncoder(ensure_ascii=False, indent=1)
    chunks: list[str] = []
    size = 0
    for chunk in encoder.iterencode(value):
        remaining = limit - size
        if len(chunk) > remaining:
            if remaining > 0:
                chunks.append(chunk[:remaining])
            return _bounded_text("".join(chunks), limit, truncated=True)
        chunks.append(chunk)
        size += len(chunk)
    return "".join(chunks)


def _path_error(message: str) -> str:
    return _bounded_text(f"[json_path] {message}", MAX_READ_SAMPLE_CHARS)


def _property_path(path: str, key: str) -> str:
    if IDENTIFIER_RE.fullmatch(key):
        return f"{path}.{key}"
    return f"{path}[{json.dumps(key, ensure_ascii=False)}]"


def _select_json_path(sample: Any, json_path: str) -> tuple[bool, Any]:
    """Resolve to the original subtree, distinguishing data from bounded path errors."""
    tokens = _tokenize_json_path(json_path)
    if isinstance(tokens, str):
        return False, _path_error(tokens)
    node = sample
    prefix = "$"
    for kind, value in tokens:
        if kind == "key":
            if not isinstance(node, dict):
                return False, _path_error(
                    f"{prefix} is {type(node).__name__}, cannot read object key {value!r}"
                )
            if value not in node:
                keys = ", ".join(str(key)[:64] for key in list(node.keys())[:20]) or "(no keys)"
                return False, _path_error(f"{prefix}.{value} does not exist; available keys: {keys}")
            node = node[value]
            prefix = _property_path(prefix, value)
        elif kind == "index":
            if not isinstance(node, list):
                return False, _path_error(f"{prefix} is not a list; cannot index [{value}]")
            if value >= len(node):
                return False, _path_error(
                    f"{prefix}[{value}] out of range; list length={len(node)}"
                )
            node = node[value]
            prefix += f"[{value}]"
        elif kind == "script":
            if not isinstance(node, str) or not _looks_like_html(node):
                return False, _path_error(f"{prefix} is not HTML; cannot select embedded JSON script")
            scripts = _embedded_json_scripts(node)
            if value >= len(scripts):
                available = (
                    ", ".join(
                        f"#script[{idx}](id={str(script['id'])[:64]!r})"
                        for idx, script in enumerate(scripts[:10])
                    )
                    or "no parseable JSON script"
                )
                return False, _path_error(
                    f"{prefix} has no script[{value}]; available: {available}"
                )
            node = scripts[value]["value"]
            prefix += f"{SCRIPT_MARKER}{value}]"
    return True, node


def _resolve_json_path(sample: Any, json_path: str, limit: int) -> str:
    found, node = _select_json_path(sample, json_path)
    if not found:
        return _bounded_text(node, limit)
    return _bounded_text(node, limit) if isinstance(node, str) else _bounded_json_text(node, limit)


# --- describe (bounded deep digest) ---------------------------------------------


def _digest_entry(path: str, value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {"path": path, "type": "object", "key_count": len(value)}
    if isinstance(value, list):
        element_type = type(value[0]).__name__ if value else "unknown"
        return {"path": path, "type": f"list<{element_type}>", "cardinality": len(value)}
    if isinstance(value, str):
        if _looks_like_html(value):
            return {"path": path, "type": "html", "size": len(value)}
        return {"path": path, "type": "str", "sample": value[:MAX_LEAF_SAMPLE]}
    if value is None:
        return {"path": path, "type": "null", "sample": "null"}
    if isinstance(value, bool):
        return {"path": path, "type": "bool", "sample": str(value).lower()}
    if isinstance(value, (int, float)):
        return {"path": path, "type": type(value).__name__, "sample": str(value)}
    return {"path": path, "type": type(value).__name__}


def _append_digest_entry(
    entry: dict[str, Any], entries: list[dict[str, Any]], state: dict[str, Any]
) -> bool:
    if len(entries) >= MAX_DIGEST_ENTRIES:
        state["truncated"] = True
        state["stop"] = True
        state["reason"] = "entry_limit"
        state["omitted"] += 1
        return False
    size = len(json.dumps(entry, ensure_ascii=False)) + 1
    if state["entry_chars"] + size > DIGEST_ENTRY_BUDGET:
        state["truncated"] = True
        state["stop"] = True
        state["reason"] = "output_limit"
        state["omitted"] += 1
        return False
    entries.append(entry)
    state["entry_chars"] += size
    return True


def _mark_digest_truncated(entry: dict[str, Any], state: dict[str, Any], omitted: int) -> None:
    entry["truncated"] = True
    state["truncated"] = True
    state["reason"] = state["reason"] or "depth_limit"
    state["omitted"] += max(1, omitted)


def _walk_digest(
    path: str,
    value: Any,
    entries: list[dict[str, Any]],
    *,
    depth: int,
    state: dict[str, Any],
    annotations: dict[str, Any] | None = None,
) -> None:
    if state["stop"]:
        state["omitted"] += 1
        return

    entry = _digest_entry(path, value)
    if annotations:
        entry.update(annotations)

    children: list[tuple[str, Any]] = []
    omitted = 0
    if isinstance(value, dict):
        keys = list(value.keys())
        if depth >= MAX_DIGEST_DEPTH and keys:
            _mark_digest_truncated(entry, state, len(keys))
        else:
            for key in keys[:MAX_DIGEST_WIDTH]:
                if not isinstance(key, str):
                    continue
                child_path = _property_path(path, key)
                if len(child_path) > 512:
                    omitted += 1
                    continue
                children.append((child_path, value[key]))
            omitted += max(0, len(keys) - MAX_DIGEST_WIDTH)
            if omitted:
                entry["more"] = f"+{omitted} more"
                state["truncated"] = True
                state["reason"] = state["reason"] or "width_or_path_limit"
                state["omitted"] += omitted
    elif isinstance(value, list):
        if value and depth >= MAX_DIGEST_DEPTH:
            _mark_digest_truncated(entry, state, len(value))
        elif value:
            children.append((f"{path}[0]", value[0]))
            if len(value) > 1:
                entry["sample_count"] = 1
    elif isinstance(value, str) and _looks_like_html(value):
        if depth >= MAX_DIGEST_DEPTH:
            scripts = _embedded_json_scripts(value)
            if scripts:
                _mark_digest_truncated(entry, state, len(scripts))
        else:
            for index, script in enumerate(_embedded_json_scripts(value)):
                script_path = f"{path}{SCRIPT_MARKER}{index}]"
                annotation = {"script_id": script["id"], "script_type": script["type"]}
                children.append((script_path, script["value"]))
                state.setdefault("annotations", {})[script_path] = annotation

    if not _append_digest_entry(entry, entries, state):
        return
    if state["truncated"] and not children:
        return
    for child_index, (child_path, child) in enumerate(children):
        if state["stop"]:
            state["omitted"] += len(children) - child_index
            return
        _walk_digest(
            child_path,
            child,
            entries,
            depth=depth + 1,
            state=state,
            annotations=state.get("annotations", {}).get(child_path),
        )


def _legacy_shape_summary(sample: Any) -> dict[str, Any]:
    """Keep the pre-digest shape/keys API while avoiding repr() of huge payloads."""
    response = sample.get("response", sample) if isinstance(sample, dict) else sample
    if isinstance(response, dict):
        keys = []
        for key, value in list(response.items())[:MAX_DIGEST_KEYS]:
            entry: dict[str, Any] = {
                "name": str(key)[:MAX_LEAF_SAMPLE],
                "type": type(value).__name__,
            }
            if isinstance(value, str) and _looks_like_html(value):
                entry["type"] = "html"
                entry["size"] = len(value)
            elif not isinstance(value, (dict, list)):
                entry["sample"] = str(value)[:MAX_LEAF_SAMPLE]
            keys.append(entry)
        return {"shape": "object", "keys": keys}
    if isinstance(response, list):
        summary: dict[str, Any] = {"shape": "array", "cardinality": len(response)}
        if response:
            first = response[0]
            summary["sample"] = (
                str(first)[:MAX_LEAF_SAMPLE]
                if not isinstance(first, (dict, list))
                else f"<{type(first).__name__} omitted; see entries>"
            )
        return summary
    if isinstance(response, str):
        return {"shape": "scalar", "sample": response[:MAX_LEAF_SAMPLE]}
    return {"shape": "scalar", "sample": str(response)[:MAX_LEAF_SAMPLE]}


def describe_data(sample_path: str | Path, *, json_path: str = "") -> dict[str, Any]:
    """Return a bounded deep path map compatible with ``read --json-path``."""
    sample = _load_sample(sample_path)
    if json_path:
        found, sample = _select_json_path(sample, json_path)
        if not found:
            return {"error": sample, "entries": [], "truncated": False}
    state: dict[str, Any] = {
        "truncated": False,
        "stop": False,
        "reason": None,
        "omitted": 0,
        "entry_chars": 0,
        "annotations": {},
    }
    entries: list[dict[str, Any]] = []
    root_path = json_path or "$"
    _walk_digest(root_path, sample, entries, depth=0, state=state)
    summary: dict[str, Any] = (
        {"shape": "text", "sample": sample[:MAX_LEAF_SAMPLE]}
        if isinstance(sample, str)
        else _legacy_shape_summary(sample)
    )
    while (
        max(len(json.dumps(summary, ensure_ascii=False)), len(str(summary))) > 8_000
        and summary.get("keys")
    ):
        summary["keys"].pop()
        state["truncated"] = True
        state["omitted"] += 1
        state["reason"] = "output_limit"
    result = {
        **summary,
        "entries": entries,
        "truncated": state["truncated"],
        "omitted_count": state["omitted"],
    }
    if state["reason"]:
        result["truncation_reason"] = state["reason"]
    while (
        max(len(json.dumps(result, ensure_ascii=False)), len(str(result))) > MAX_DIGEST_OUTPUT_CHARS
        and entries
    ):
        entries.pop()
        result["truncated"] = True
        result["omitted_count"] += 1
        result["truncation_reason"] = "output_limit"
    return result


# --- read (bounded slice) --------------------------------------------------------


def read_sample(sample_path: str | Path, limit: int = DEFAULT_READ_LIMIT, json_path: str = "") -> str | None:
    """Read a bounded raw or selected sample fragment (all modes honor ``limit``).

    ``json_path`` copies a path emitted by ``describe``: ``$``, ``.identifier``,
    ``[0]``, ``["non.simple key"]``, ``#script[n]`` (enters HTML-embedded JSON).
    A top-level ``response`` key (captured-dump convention) is treated as the payload.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        return "[read_sample] limit must be a positive integer"
    limit = min(limit, MAX_READ_SAMPLE_CHARS)
    sample = _load_sample(sample_path)
    if json_path:
        return _resolve_json_path(sample, json_path, limit)
    if isinstance(sample, dict) and "response" in sample:
        resp = sample["response"]
        text = resp if isinstance(resp, str) else _bounded_json_text(resp, limit)
    elif isinstance(sample, str):
        text = sample
    else:
        text = _read_file_text(sample_path)
    return _bounded_text(text, limit)


def _json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def batch_read(sample_path: str | Path, paths: list[str], limit: int) -> dict:
    """Resolve many json_paths in one pass.

    Returns {"file", "count", "results": [{path, ok, type, value} | {path, ok, error}]}.
    Per-path failures (bad grammar, missing key, bad index) never abort the batch —
    they come back as ok:false with the same guidance text `read` would print.
    """
    sample = _load_sample(sample_path)
    results = []
    for jp in paths:
        found, node = _select_json_path(sample, jp)
        if not found:
            results.append({"path": jp, "ok": False, "error": _bounded_text(node, 400)})
            continue
        if isinstance(node, str):
            value = _bounded_text(node, limit, truncated=len(node) > limit)
        else:
            value = _bounded_json_text(node, limit)
        results.append({"path": jp, "ok": True, "type": _json_type_name(node), "value": value})
    return {"file": str(sample_path), "count": len(results), "results": results}


def batch_describe_data(sample_path: str | Path, paths: list[str]) -> dict:
    """Describe many subtrees in one pass.

    Returns {"file", "count", "results": [{path, ok, describe} | {path, ok, error}]}.
    Per-path misses never abort the batch. Total output is size-capped at
    BATCH_TOTAL_CHARS: once the budget is exhausted, remaining paths come back
    as ok entries with omitted=True and describe=None — split the path list
    across calls instead of raising limits.
    """
    results: list[dict[str, Any]] = []
    total = len(json.dumps({"file": str(sample_path), "count": 0, "results": []}, ensure_ascii=False))
    exhausted = False
    for jp in paths:
        if exhausted:
            results.append({"path": jp, "ok": True, "omitted": True, "describe": None})
            continue
        describe = describe_data(sample_path, json_path=jp)
        if "error" in describe:
            results.append({"path": jp, "ok": False, "error": describe["error"]})
            continue
        blob = json.dumps(describe, ensure_ascii=False)
        if total + len(blob) > BATCH_TOTAL_CHARS:
            exhausted = True
            results.append({"path": jp, "ok": True, "omitted": True, "describe": None})
            continue
        total += len(blob) + 1
        results.append({"path": jp, "ok": True, "describe": describe})
    return {"file": str(sample_path), "count": len(results), "results": results}


# --- select (CSS over HTML; optional parsel dependency) --------------------------


SELECT_MATCH_SHOW = 30


def _select_install_hint() -> str:
    return (
        "select needs the optional dependency parsel for this interpreter — "
        f"run: {sys.executable} -m pip install parsel"
    )


def select_data(
    sample_path: str | Path,
    selector: str,
    *,
    json_path: str = "",
    attr: str | None = None,
    want_text: bool = False,
    want_html: bool = False,
    limit: int,
) -> dict[str, Any] | None:
    """CSS-select over HTML with count-first bounded output.

    Returns None when the optional parsel dependency is missing (caller prints
    the install hint); otherwise an envelope honouring the batch caps: total
    match count, first SELECT_MATCH_SHOW matches/values, per-value ``limit``.
    """
    try:
        from parsel import Selector
    except ImportError:
        return None
    sample = _load_sample(sample_path)
    if json_path:
        found, node = _select_json_path(sample, json_path)
        if not found:
            return {"error": _bounded_text(node, 400)}
        if not isinstance(node, str):
            return {
                "error": (
                    f"{json_path} resolved to {_json_type_name(node)}, not a string — "
                    "select needs an HTML string (point --json-path at an HTML field)"
                )
            }
        html = node
    elif isinstance(sample, str):
        html = sample
    else:
        return {
            "error": (
                f"{sample_path} parsed as JSON — select needs HTML; pass -p/--json-path "
                "pointing at an HTML string field, or use describe/read"
            )
        }
    matches = Selector(text=html).css(selector)
    total = len(matches)
    shown = matches[:SELECT_MATCH_SHOW]

    def text_of(m) -> str:
        return " ".join(" ".join(m.css("::text").getall()).split())

    def bounded(s: str) -> str:
        return _bounded_text(s, limit)

    if attr is not None:
        values = [bounded(m.attrib.get(attr, "")) for m in shown]
    elif want_html:
        values = [bounded(m.get() or "") for m in shown]
    elif want_text:
        values = [bounded(text_of(m)) for m in shown]
    else:
        values = None
    payload: dict[str, Any] = {
        "file": str(sample_path),
        "selector": selector,
        "count": total,
        "shown": len(shown),
        "omitted": total - len(shown),
    }
    if values is not None:
        payload["values"] = values
    else:
        payload["matches"] = [
            {
                "index": index,
                "tag": m.root.tag,
                "attrs": dict(m.root.attrib),
                "text": bounded(text_of(m)),
            }
            for index, m in enumerate(shown)
        ]
    return payload


# --- CLI -------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # noqa: D401 - argparse hook
        # Agent contract: never a usage dump / traceback; one guidance line, exit 1.
        print(f"[inspect_sample] {message} — run with --help for usage")
        raise SystemExit(1)


def _gather_paths(args: argparse.Namespace) -> list[str]:
    """Collect --paths-file lines then --json-path values (any shape), deduped.

    ``--json-path`` uses action="append" + nargs="*", so one occurrence may
    carry several paths (``--json-path $.a $.b``) and repeated flags stack.
    """
    paths: list[str] = []
    if getattr(args, "paths_file", None):
        pf = Path(args.paths_file)
        if not pf.is_file():
            print(f"[inspect_sample] paths file not found: {args.paths_file}")
            raise SystemExit(1)
        for line in pf.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                paths.append(line)
    for group in (args.json_path or []):
        if isinstance(group, list):
            paths.extend(p.strip() for p in group if isinstance(p, str) and p.strip())
        elif isinstance(group, str) and group.strip():
            paths.append(group.strip())
    return list(dict.fromkeys(paths))


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(prog="inspect_sample.py", description="Bounded structure inspection of a captured response file.")
    sub = parser.add_subparsers(dest="command", required=True)
    p_describe = sub.add_parser(
        "describe", aliases=["batch_describe"],
        help="bounded path->type->sample map: whole file, one subtree, or a whole path list")
    p_describe.add_argument("file", help="response file (JSON or HTML)")
    p_describe.add_argument("-p", "--json-path", action="append", nargs="*", default=[],
                            help="one or more subtree json_paths per occurrence; repeatable")
    p_describe.add_argument("--paths-file", default=None,
                            help="text file with one json_path per line (# comments and blank lines allowed)")
    p_read = sub.add_parser(
        "read", aliases=["batch", "batch_read"],
        help="bounded slice of one subtree, or per-path values for a whole path list")
    p_read.add_argument("file", help="response file (JSON or HTML)")
    p_read.add_argument("-p", "--json-path", action="append", nargs="*", default=[],
                        help="one or more json_paths per occurrence; repeatable")
    p_read.add_argument("--paths-file", default=None,
                        help="text file with one json_path per line (# comments and blank lines allowed)")
    p_read.add_argument("--limit", default=None,
                        help=f"max characters per result (single mode default {DEFAULT_READ_LIMIT}; list mode default {BATCH_VALUE_LIMIT}; 1..{MAX_READ_SAMPLE_CHARS})")
    p_select = sub.add_parser("select", help="CSS-select over HTML (count-first bounded output; optional parsel dependency)")
    p_select.add_argument("file", help="HTML file, or a JSON capture paired with --json-path")
    p_select.add_argument("selector", help="CSS selector")
    p_select.add_argument("--json-path", default="",
                          help="optional: address an HTML string field inside a JSON capture")
    sel_group = p_select.add_mutually_exclusive_group()
    sel_group.add_argument("--attr", default=None, help="extract this attribute from every match")
    sel_group.add_argument("--text", action="store_true", help="extract each match's text content")
    sel_group.add_argument("--html", action="store_true", help="extract each match's outer HTML")
    p_select.add_argument("--limit", default=None,
                          help=f"max characters per value (default {BATCH_VALUE_LIMIT})")
    args = parser.parse_args(argv)

    path = Path(args.file)
    if not path.is_file():
        print(f"[inspect_sample] file not found: {args.file}")
        return 1
    try:
        if args.command in ("describe", "batch_describe"):
            legacy_batch = args.command == "batch_describe"
            paths = _gather_paths(args)
            if not paths:
                if legacy_batch:
                    print("[inspect_sample] no paths given — use -p/--json-path or --paths-file")
                    return 1
                print(json.dumps(describe_data(path), ensure_ascii=False))
                return 0
            if len(paths) == 1 and not legacy_batch:
                print(json.dumps(describe_data(path, json_path=paths[0]), ensure_ascii=False))
                return 0
            print(json.dumps(batch_describe_data(path, paths), ensure_ascii=False))
            return 0
        if args.command in ("read", "batch", "batch_read"):
            legacy_batch = args.command != "read"
            paths = _gather_paths(args)
            if args.limit is None:
                limit = BATCH_VALUE_LIMIT if (legacy_batch or len(paths) > 1) else DEFAULT_READ_LIMIT
            else:
                try:
                    limit = int(str(args.limit).strip())
                except ValueError:
                    print("[read_sample] limit must be a positive integer")
                    return 1
            if not paths:
                if legacy_batch:
                    print("[inspect_sample] no paths given — use -p/--json-path or --paths-file")
                    return 1
                out = read_sample(path, min(limit, MAX_READ_SAMPLE_CHARS))
                if out is not None:
                    print(out)
                return 0
            if len(paths) == 1 and not legacy_batch:
                out = read_sample(path, min(limit, MAX_READ_SAMPLE_CHARS), json_path=paths[0])
                if out is not None:
                    print(out)
                return 0
            payload = batch_read(path, paths, min(limit, MAX_READ_SAMPLE_CHARS))
            blob = json.dumps(payload, ensure_ascii=False)
            if len(blob) > BATCH_TOTAL_CHARS:
                kept = 0
                for r in payload["results"]:
                    if r.get("ok"):
                        kept += 1
                        if kept > 30:
                            r["omitted"] = True
                            r["value"] = None
                blob = json.dumps(payload, ensure_ascii=False)
            print(blob)
            return 0
        if args.command == "select":
            limit = BATCH_VALUE_LIMIT
            if args.limit is not None:
                try:
                    limit = int(str(args.limit).strip())
                except ValueError:
                    print("[read_sample] limit must be a positive integer")
                    return 1
            payload = select_data(
                path,
                args.selector,
                json_path=args.json_path,
                attr=args.attr,
                want_text=args.text,
                want_html=args.html,
                limit=limit,
            )
            if payload is None:
                print(f"[inspect_sample] {_select_install_hint()}")
                return 1
            if "error" in payload:
                print(json.dumps(payload, ensure_ascii=False))
                return 1
            blob = json.dumps(payload, ensure_ascii=False)
            if len(blob) > BATCH_TOTAL_CHARS and payload.get("matches"):
                for entry in payload["matches"][10:]:
                    entry["text"] = None
                    entry["omitted"] = True
                blob = json.dumps(payload, ensure_ascii=False)
            print(blob)
            return 0
        print(f"[inspect_sample] unknown command: {args.command}")
        return 1
    except OSError as exc:
        print(f"[inspect_sample] cannot read {args.file}: {exc}")
        return 1


if __name__ == "__main__":
    if sys.version_info < (3, 9):
        print(
            "[inspect_sample] Python 3.9+ required; found "
            + ".".join(str(part) for part in sys.version_info[:3])
        )
        sys.exit(1)
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass  # benign: non-standard stream (e.g. captured buffer) without reconfigure
    sys.exit(main())
