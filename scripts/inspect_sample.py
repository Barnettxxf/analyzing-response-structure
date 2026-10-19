#!/usr/bin/env python3
"""Bounded, read-only, offline inspection of captured JSON and HTML.

Python 3.9+ stdlib handles JSON paths, inline-script discovery and HTML structure
counts. XPath is optional and uses parsel/lxml. No scripts from captures run.
Script candidates are non-empty closed inline <script> elements; comments and
external-only scripts are excluded. A literal </script> ends the element even
inside a JSON string, as in HTML parsing. Inputs use utf-8-sig with replacement
characters for undecodable bytes, never an implicit claim of the right encoding.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import glob
import json
import math
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, NoReturn

IDENTIFIER_RE = re.compile(r"^[@A-Za-z_$][@A-Za-z0-9_$]*$")
PREDICATE_RE = re.compile(r"\?([@A-Za-z_$][@A-Za-z0-9_$]*)=(.*)\Z", re.DOTALL)
SCRIPT_MARKER = "#script["
MAX_DIGEST_KEYS = 50
MAX_DIGEST_ENTRIES = 200
MAX_DIGEST_WIDTH = 50
MAX_DIGEST_DEPTH = 16
MAX_LEAF_SAMPLE = 80
MAX_UNION_KEYS = 40
LIMIT_MIN = 16  # 截断标记 14 字符 + 至少 2 字符内容——低于此值输出会退化成纯标记碎片
PAGINATION_KEY_RE = re.compile(
    r"(?:^|[_\-\s])(?:next|cursor|page|offset|has_more|hasmore|hasnext|total|token)(?:$|[_\-\s])",
    re.I,
)
PAGINATION_CANDIDATE_CAP = 20
MAX_DIGEST_OUTPUT_CHARS = 24_000

# Per-entry budget for describe maps. Default kept at the historical 11000: a
# field rollback experiment (v1.16.2) showed a 900 default degraded a
# describe-heavy agent (66-70/80 vs 80/80 on rollback). Per-path control is
# still available opt-in via --limit (e.g. --limit 900 for wide multi-path
# surveys that must fit one call).
DIGEST_ENTRY_BUDGET = 11_000
MAX_ENTRY_BUDGET = 11_000
MAX_READ_SAMPLE_CHARS = 20_000
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
    document_hint = (
        head.startswith("<!doctype html")
        or head.startswith("<html")
        or head.startswith("<!--")
        and ("<html" in head or "<!doctype" in head)
    )
    if "<" not in text:
        return False
    probe = _HTMLFragmentProbe()
    try:
        probe.feed(text)
        probe.close()
    except (AssertionError, NotImplementedError):
        # Invalid marked declarations are detection misses. HTMLParser uses
        # NotImplementedError on 3.9 and AssertionError on newer interpreters.
        return False
    return document_hint or probe.has_element


class _HTMLFragmentProbe(HTMLParser):
    """Recognize actual elements, not escaped markup or tags inside comments."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.has_element = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.has_element = True


class _ScriptCollector(HTMLParser):
    """Collect JSON-bearing <script> elements in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.scripts: list[dict[str, Any]] = []
        self._capturing = False
        self._chunks: list[str] = []
        self._meta: tuple[str | None, str | None] = (None, None)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script" and not self._capturing:
            attrd = {str(k).lower(): str(v or "") for k, v in attrs}
            self._meta = (attrd.get("id") or None, attrd.get("type") or None)
            self._capturing = True
            self._chunks = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._capturing:
            self._capturing = False
            text = "".join(self._chunks)
            if not text.strip():
                return  # src-only / empty include: no inline content to inspect
            entry: dict[str, Any] = {
                "id": self._meta[0],
                "type": self._meta[1],
                "length": len(text),
                "byte_length": len(text.encode("utf-8")),
            }
            try:
                entry["value"] = json.loads(text)
                entry["ok"] = True
            except (TypeError, ValueError):
                entry["ok"] = False
                entry["parse_error"] = True
                entry["head"] = text.strip()[:160]
            self.scripts.append(entry)

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._chunks.append(data)


class _HTMLStructure(HTMLParser):
    """Count element/tag.class presence, never copy body text or attribute values."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.element_count = 0
        self.tags: dict[str, int] = {}
        self.classes: dict[str, int] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.element_count += 1
        self.tags[tag] = self.tags.get(tag, 0) + 1
        for key, value in attrs:
            if key == "class" and value:
                for name in set(value.split()):
                    label = f"{tag}.{name}"
                    self.classes[label] = self.classes.get(label, 0) + 1
                break


def _html_structure(html: str) -> dict[str, Any]:
    counter = _HTMLStructure()
    counter.feed(html)
    counter.close()
    tags = dict(sorted(counter.tags.items(), key=lambda pair: (-pair[1], pair[0]))[:30])
    classes = dict(
        sorted(counter.classes.items(), key=lambda pair: (-pair[1], pair[0]))[:30]
    )
    result: dict[str, Any] = {
        "element_count": counter.element_count,
        "distinct_tags": len(counter.tags),
        "distinct_tag_classes": len(counter.classes),
        "tag_counts": tags,
        "class_counts": classes,
    }
    # Long custom tag/class names must not displace the entire path map.
    while len(json.dumps(result, ensure_ascii=False)) > 3800 and (classes or tags):
        (classes if classes else tags).popitem()
    result.update(
        tag_groups_omitted=len(counter.tags) - len(tags),
        class_groups_omitted=len(counter.classes) - len(classes),
    )
    result["truncated"] = bool(
        result["tag_groups_omitted"] or result["class_groups_omitted"]
    )
    return result


def _embedded_json_scripts(html: str) -> list[dict[str, Any]]:
    collector = _ScriptCollector()
    collector.feed(html)
    collector.close()
    return collector.scripts


def _parse_limit(raw: Any, default: int) -> tuple[int, str | None]:
    """Shared limit parser for every output channel. Returns (limit, error).

    Clamped to LIMIT_MIN..MAX_READ_SAMPLE_CHARS — below the floor the truncation
    marker alone would consume the quota and values degenerate to "..".
    """
    if raw is None:
        return default, None
    try:
        limit = int(str(raw).strip())
    except ValueError:
        return 0, "[read_sample] limit must be a positive integer"
    if limit < 1:
        return 0, "[read_sample] limit must be a positive integer"
    return min(max(limit, LIMIT_MIN), MAX_READ_SAMPLE_CHARS), None


# --- json_path grammar ----------------------------------------------------------


def _path_bracket_end(path: str, start: int) -> int:
    """Find ] outside either quote style; escaped quotes/brackets stay in a key."""
    quote = ""
    escaped = False
    for index in range(start, len(path)):
        char = path[index]
        if escaped:
            escaped = False
        elif quote:
            if char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in ("'", '"'):
            quote = char
        elif char == "]":
            return index
    return -1


def _path_string(raw: str) -> Any:
    """Decode a quoted path key, never execute expressions from a path."""
    if len(raw) < 2 or raw[0] not in ("'", '"') or raw[-1] != raw[0]:
        return _PARSE_FAILED
    try:
        value = json.loads(raw) if raw[0] == '"' else ast.literal_eval(raw)
    except (SyntaxError, TypeError, ValueError):
        return _PARSE_FAILED
    return value if isinstance(value, str) else _PARSE_FAILED


def _tokenize_json_path(json_path: str) -> list[tuple[str, Any]] | str:
    """Small path grammar: ``$``, ``.identifier``, ``[integer]``, ``[JSON-string]``,
    ``[?key=value]`` (unique-element predicate), ``.$len`` and ``#script[n]``."""
    if not isinstance(json_path, str) or not json_path.startswith("$"):
        return "path must start with $"
    if len(json_path) > 2048:
        return "path too long; use a bounded path from describe"
    digit_index = re.compile(r"[0-9]{1,18}\Z")
    neg_index = re.compile(r"-[0-9]{1,18}\Z")
    tokens: list[tuple[str, Any]] = []
    index = 1
    while index < len(json_path):
        if json_path.startswith(SCRIPT_MARKER, index):
            start = index + len(SCRIPT_MARKER)
            end = _path_bracket_end(json_path, start)
            raw = json_path[start:end] if end >= 0 else ""
            if end < 0:
                return "unclosed script selector"
            if digit_index.fullmatch(raw):
                tokens.append(("script", int(raw)))
            elif raw.startswith("id="):
                sid = _path_string(raw[3:])
                if sid is _PARSE_FAILED:
                    return 'script selector needs a quoted id, e.g. #script[id="state"]'
                tokens.append(("script_id", sid))
            elif raw.startswith("type="):
                stype = _path_string(raw[5:])
                if stype is _PARSE_FAILED:
                    return (
                        "script selector needs a quoted type, "
                        'e.g. #script[type="application/json"]'
                    )
                tokens.append(("script_type", stype))
            else:
                return (
                    'script selector must be #script[N], #script[id="..."], '
                    'or #script[type="..."]'
                )
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
            if key == "$len":
                tokens.append(("len_op", None))
            else:
                tokens.append(("key", key))
            index = end
            continue
        if json_path[index] == "[":
            end = _path_bracket_end(json_path, index + 1)
            if end < 0:
                return "unclosed [] path segment"
            raw = json_path[index + 1 : end]
            if raw.startswith("?"):
                match = PREDICATE_RE.fullmatch(raw)
                if match is None:
                    return "[] predicate must be [?key=value] with an identifier key and a value"
                pkey, pvalue = match.group(1), match.group(2)
                if pvalue[:1] in ("'", '"'):
                    decoded = _path_string(pvalue)
                    if decoded is _PARSE_FAILED:
                        return (
                            "[] predicate value must be a quoted string or a bareword"
                        )
                    pvalue = decoded
                tokens.append(("pred", (pkey, pvalue)))
                index = end + 1
                continue
            if raw == "*":
                tokens.append(("wildcard", None))
            elif digit_index.fullmatch(raw):
                tokens.append(("index", int(raw)))
            elif neg_index.fullmatch(raw):
                tokens.append(("index", int(raw)))
            else:
                key = _path_string(raw)
                if key is _PARSE_FAILED:
                    return "[] accepts an integer index, [*], or a single-/double-quoted string key"
                tokens.append(("key", key))
            index = end + 1
            continue
        return f"invalid path at position {index}"
    return tokens


def _bounded_text(text: str, limit: int, *, truncated: bool = False) -> str:
    if len(text) <= limit and not truncated:
        return text
    marker = "...[truncated]"
    if limit <= len(marker):
        return text[:limit]  # 内容优先：配额比标记还短时，给内容头而不是标记碎片
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


def _format_pred_value(value: str) -> str:
    """Re-serialize a predicate value so it re-tokenizes to itself."""
    if value == "" or "]" in value or value[0] in ("'", '"'):
        return json.dumps(value, ensure_ascii=False)
    return value


def _tokens_to_path(tokens: list[tuple[str, Any]]) -> str:
    """Reconstruct a human-readable path string from parsed tokens."""
    parts = ["$"]
    for kind, value in tokens:
        if kind == "key":
            parts.append(_property_path("", value))
        elif kind == "index":
            parts.append(f"[{value}]")
        elif kind == "wildcard":
            parts.append("[*]")
        elif kind == "script":
            parts.append(f"#script[{value}]")
        elif kind == "script_id":
            parts.append(f"#script[id={json.dumps(value, ensure_ascii=False)}]")
        elif kind == "script_type":
            parts.append(f"#script[type={json.dumps(value, ensure_ascii=False)}]")
        elif kind == "pred":
            pkey, pvalue = value
            parts.append(f"[?{pkey}={_format_pred_value(pvalue)}]")
        elif kind == "len_op":
            parts.append(".$len")
    return "".join(parts)


def _path_error(message: str) -> str:
    return _bounded_text(f"[json_path] {message}", MAX_READ_SAMPLE_CHARS)


def _property_path(path: str, key: str) -> str:
    if key != "$len" and IDENTIFIER_RE.fullmatch(key):
        return f"{path}.{key}"
    return f"{path}[{json.dumps(key, ensure_ascii=False)}]"


def _select_json_path(sample: Any, json_path: str) -> tuple[bool, Any]:
    """Parse once, then resolve tokens without changing key/operator identity."""
    tokens = _tokenize_json_path(json_path)
    if isinstance(tokens, str):
        return False, _path_error(tokens)
    return _resolve_path_tokens(sample, tokens)


def _resolve_path_tokens(
    sample: Any, tokens: list[tuple[str, Any]], start: int = 0, prefix: str = "$"
) -> tuple[bool, Any]:
    """Resolve ordinary segments and wildcard suffixes with the same semantics."""
    node = sample
    for token_index in range(start, len(tokens)):
        kind, value = tokens[token_index]
        if kind == "wildcard":
            if not isinstance(node, list):
                return False, _path_error(
                    f"{prefix} is not a list; wildcard [*] requires array (got {_json_type_name(node)})"
                )
            results = []
            for element in node:
                found, selected = _resolve_path_tokens(
                    element, tokens, token_index + 1, prefix + "[*]"
                )
                # A suffix miss keeps its source position, including nested [*].
                results.append(selected if found else None)
            return True, results
        elif kind == "key":
            if not isinstance(node, dict):
                hint = (
                    "; this file is HTML — embedded JSON lives in $#script[n] "
                    "(describe --scripts lists them; across a batch use "
                    "describe --scripts --files '<glob>' plus "
                    "--json-root '$#script[...]')"
                    if isinstance(node, str) and _looks_like_html(node)
                    else ""
                )
                return False, _path_error(
                    f"{prefix} is {type(node).__name__}, cannot read object key {value!r}{hint}"
                )
            if value not in node:
                keys = (
                    ", ".join(str(key)[:64] for key in list(node.keys())[:20])
                    or "(no keys)"
                )
                return False, _path_error(
                    f"{prefix}.{value} does not exist; available keys: {keys}"
                )
            node = node[value]
            prefix = _property_path(prefix, value)
        elif kind == "index":
            if not isinstance(node, list):
                return False, _path_error(
                    f"{prefix} is not a list; cannot index [{value}]"
                )
            idx = value if value >= 0 else len(node) + value
            if idx >= len(node) or idx < 0:
                return False, _path_error(
                    f"{prefix}[{value}] out of range; list length={len(node)}"
                )
            node = node[idx]
            prefix += f"[{value}]"
        elif kind == "pred":
            pkey, pvalue = value
            if not isinstance(node, list):
                return False, _path_error(
                    f"{prefix} is {_json_type_name(node)}, not a list; "
                    f"[?{pkey}={pvalue}] filters array elements"
                )
            matches = [
                el
                for el in node
                # pkey must exist: str(el.get(pkey)) would otherwise turn every
                # missing key into a "None" match.
                if isinstance(el, dict) and pkey in el and str(el[pkey]) == pvalue
            ]
            nxt = tokens[token_index + 1] if token_index + 1 < len(tokens) else None
            if nxt is not None and nxt[0] == "index":
                # An explicit index addresses the match list at every cardinality.
                node = matches
                prefix += f"[?{pkey}={_format_pred_value(pvalue)}]"
            elif len(matches) == 1:
                node = matches[0]  # unique match: auto-unwrap, keep resolving
                prefix += f"[?{pkey}={_format_pred_value(pvalue)}]"
            elif not matches:
                seen_values: list[str] = []
                for el in node:
                    if isinstance(el, dict) and pkey in el:
                        text = str(el[pkey])[:64]
                        if text not in seen_values:
                            seen_values.append(text)
                listing = ", ".join(seen_values[:10]) or "(none)"
                return False, _path_error(
                    f"{prefix}[?{pkey}={pvalue}] matched 0 elements; "
                    f"available {pkey} values: {listing}"
                )
            else:
                return False, _path_error(
                    f"{prefix}: {len(matches)} matches for [?{pkey}={pvalue}]; "
                    "use a stricter predicate, or append [0] to select one"
                )
        elif kind == "len_op":
            if not isinstance(node, (list, dict, str)):
                return False, _path_error(
                    f"{prefix} is {_json_type_name(node)}; "
                    "$len needs a list, dict or string"
                )
            node = len(node)
            prefix += ".$len"
        elif kind in ("script", "script_id", "script_type"):
            if not isinstance(node, str) or not _looks_like_html(node):
                return False, _path_error(
                    f"{prefix} is not HTML; cannot select embedded JSON script"
                )
            scripts = _embedded_json_scripts(node)
            if kind == "script":
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
                cand = scripts[value]
            elif kind == "script_id":
                cand = next((s for s in scripts if s.get("id") == value), None)
                if cand is None:
                    available = (
                        ", ".join(f"id={s['id']!r}" for s in scripts if s.get("id"))
                        or "(no id-attributed scripts)"
                    )
                    return False, _path_error(
                        f'no script[id="{value}"]; available: {available}'
                    )
            else:  # script_type: unique match unwraps, multi-match lists indexes
                typed = [
                    (idx, s) for idx, s in enumerate(scripts) if s.get("type") == value
                ]
                if not typed:
                    seen_types: list[str] = []
                    for s in scripts:
                        if s.get("type") and s["type"] not in seen_types:
                            seen_types.append(str(s["type"])[:64])
                    available = ", ".join(seen_types[:10]) or "(no typed scripts)"
                    return False, _path_error(
                        f'no script[type="{value}"]; available types: {available}'
                    )
                if len(typed) > 1:
                    listing = ", ".join(f"#script[{idx}]" for idx, _ in typed[:10])
                    return False, _path_error(
                        f'{len(typed)} scripts match #script[type="{value}"]: '
                        f"{listing}; select one by #script[n] index"
                    )
                cand = typed[0][1]
            if not cand.get("ok"):
                return False, _path_error(
                    f"{prefix} parse_error: script content is not JSON — head: {cand.get('head', '')[:120]}"
                )
            node = cand["value"]
            prefix += _tokens_to_path([(kind, value)])[
                1:
            ]  # display only, never reparse
    return True, node


def _resolve_json_path(sample: Any, json_path: str, limit: int) -> str:
    found, node = _select_json_path(sample, json_path)
    if not found:
        return _bounded_text(node, limit)
    return (
        _bounded_text(node, limit)
        if isinstance(node, str)
        else _bounded_json_text(node, limit)
    )


# --- describe (bounded deep digest) ---------------------------------------------


def _digest_entry(path: str, value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {"path": path, "type": "object", "key_count": len(value)}
    if isinstance(value, list):
        element_type = type(value[0]).__name__ if value else "unknown"
        return {
            "path": path,
            "type": f"list<{element_type}>",
            "cardinality": len(value),
        }
    if isinstance(value, str):
        if _looks_like_html(value):
            return {"path": path, "type": "html", "size": len(value)}
        if len(value) > MAX_LEAF_SAMPLE * 2:
            head = value[:MAX_LEAF_SAMPLE]
            tail = value[-MAX_LEAF_SAMPLE // 2 :]
            return {
                "path": path,
                "type": "str",
                "sample": head + " …[snip]… " + tail,
                "length": len(value),
            }
        return {"path": path, "type": "str", "sample": value[:MAX_LEAF_SAMPLE]}
    if value is None:
        return {"path": path, "type": "null", "sample": "null"}
    if isinstance(value, bool):
        return {"path": path, "type": "bool", "sample": str(value).lower()}
    if isinstance(value, (int, float)):
        return {"path": path, "type": type(value).__name__, "sample": str(value)}
    return {"path": path, "type": type(value).__name__}


def _array_enrichment(value: list) -> dict[str, Any]:
    """Bounded heterogeneity digest for arrays: element types, union keys with
    occurrence counts and JSON-type distributions (null counted), and the
    first/middle/last candidate indices. The shallow key/type scan covers ALL
    elements; emitted value samples are reconciled after output-budget trimming."""
    n = len(value)
    enrichment: dict[str, Any] = {}
    type_counter: dict[str, int] = {}
    for el in value:
        name = type(el).__name__
        type_counter[name] = type_counter.get(name, 0) + 1
    enrichment["element_types"] = dict(
        sorted(type_counter.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    if any(isinstance(el, dict) for el in value):
        union: dict[str, dict[str, Any]] = {}
        for el in value:
            if not isinstance(el, dict):
                continue
            for k, v in el.items():
                slot = union.setdefault(k, {"count": 0, "types": {}})
                slot["count"] += 1
                tname = _json_type_name(v)
                slot["types"][tname] = slot["types"].get(tname, 0) + 1
        ordered = sorted(union.items(), key=lambda kv: (-kv[1]["count"], kv[0]))
        shown = ordered[:MAX_UNION_KEYS]
        enrichment["union_keys"] = {
            k: {
                "count": c["count"],
                "types": sorted(c["types"]),
                "type_counts": dict(sorted(c["types"].items())),
            }
            for k, c in shown
        }
        if len(ordered) > MAX_UNION_KEYS:
            enrichment["union_keys_truncated"] = True
            enrichment["union_keys_omitted"] = len(ordered) - MAX_UNION_KEYS
    mid = n // 2
    sampled = sorted({0, mid, n - 1}) if n else []
    enrichment["sampled_indices"] = sampled
    enrichment["unsampled_count"] = n - len(sampled)
    enrichment["scanned_count"] = n
    return enrichment


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
    if state["entry_chars"] + size > state.get("entry_budget", DIGEST_ENTRY_BUDGET):
        state["truncated"] = True
        state["stop"] = True
        state["reason"] = "output_limit"
        state["omitted"] += 1
        return False
    entries.append(entry)
    state["entry_chars"] += size
    return True


def _mark_digest_truncated(
    entry: dict[str, Any], state: dict[str, Any], omitted: int
) -> None:
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
                if (
                    PAGINATION_KEY_RE.search(key)
                    and len(state.get("pagination_candidates", []))
                    < PAGINATION_CANDIDATE_CAP
                ):
                    cv = value[key]
                    if isinstance(cv, (str, int, float, bool)) or cv is None:
                        state.setdefault("pagination_candidates", []).append(
                            {
                                "path": _property_path(path, key),
                                "type": _json_type_name(cv),
                                "sample": _bounded_text(str(cv), 60),
                            }
                        )
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
            entry.update(_array_enrichment(value))
            children.extend(
                (f"{path}[{index}]", value[index]) for index in entry["sampled_indices"]
            )
            entry["sample_count"] = len(children)
    elif isinstance(value, str) and _looks_like_html(value):
        if depth >= MAX_DIGEST_DEPTH:
            scripts = _embedded_json_scripts(value)
            if scripts:
                _mark_digest_truncated(entry, state, len(scripts))
        else:
            for index, script in enumerate(_embedded_json_scripts(value)):
                script_path = f"{path}{SCRIPT_MARKER}{index}]"
                annotation = {"script_id": script["id"], "script_type": script["type"]}
                if script.get("ok"):
                    children.append((script_path, script["value"]))
                else:
                    annotation["parse_error"] = True
                    children.append((script_path, script.get("head", "")))
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


def _describe_keys(sample: Any, root_path: str) -> dict[str, Any]:
    if not isinstance(sample, dict):
        return {
            "error": f"--keys needs an object; {root_path} is {_json_type_name(sample)}"
        }
    result: dict[str, Any] = {
        "path": root_path,
        "type": "object",
        "key_count": len(sample),
        "keys": [],
        "shown": 0,
        "omitted_count": len(sample),
        "truncated": False,
    }
    size = len(json.dumps(result, ensure_ascii=False))
    for key in sample:
        extra = len(json.dumps(key, ensure_ascii=False)) + 2
        if size + extra > MAX_DIGEST_OUTPUT_CHARS - 256:
            break
        result["keys"].append(key)
        size += extra
    shown = len(result["keys"])
    result.update(
        shown=shown, omitted_count=len(sample) - shown, truncated=shown < len(sample)
    )
    if result["truncated"]:
        result["truncation_reason"] = "output_limit"
    return result


def describe_data(
    sample_path: str | Path,
    *,
    json_path: str = "",
    keys_only: bool = False,
    entry_budget: int | None = None,
) -> dict[str, Any]:
    """Return a bounded deep path map compatible with ``read --json-path``."""
    sample = _load_sample(sample_path)
    if json_path:
        found, sample = _select_json_path(sample, json_path)
        if not found:
            return {"error": sample, "entries": [], "truncated": False}
    if keys_only:
        return _describe_keys(sample, json_path or "$")
    return _describe_value(sample, json_path or "$", entry_budget=entry_budget)


def _refresh_array_samples(entries: list[dict[str, Any]]) -> None:
    paths = {entry["path"] for entry in entries}
    for entry in entries:
        if "cardinality" not in entry:
            continue
        emitted = [
            index
            for index in entry.get("sampled_indices", [])
            if f"{entry['path']}[{index}]" in paths
        ]
        entry.update(
            sampled_indices=emitted,
            sample_count=len(emitted),
            unsampled_count=entry["cardinality"] - len(emitted),
        )


def _describe_value(
    sample: Any, root_path: str, entry_budget: int | None = None
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "truncated": False,
        "stop": False,
        "reason": None,
        "omitted": 0,
        "entry_chars": 0,
        "annotations": {},
        "entry_budget": entry_budget if entry_budget else DIGEST_ENTRY_BUDGET,
    }
    entries: list[dict[str, Any]] = []
    _walk_digest(root_path, sample, entries, depth=0, state=state)
    _refresh_array_samples(entries)
    summary: dict[str, Any] = (
        {"shape": "text", "sample": sample[:MAX_LEAF_SAMPLE]}
        if isinstance(sample, str)
        else _legacy_shape_summary(sample)
    )
    while max(
        len(json.dumps(summary, ensure_ascii=False)), len(str(summary))
    ) > 8_000 and summary.get("keys"):
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
    if isinstance(sample, str) and _looks_like_html(sample):
        result["html_structure"] = _html_structure(sample)
    if state["reason"]:
        result["truncation_reason"] = state["reason"]
    if state.get("pagination_candidates"):
        result["pagination_candidates"] = state["pagination_candidates"]
        result["pagination_note"] = (
            "observational candidates only — key names matched next/cursor/page/offset/"
            "has_more/total/token patterns; semantics are NOT inferred"
        )
    while (
        max(len(json.dumps(result, ensure_ascii=False)), len(str(result)))
        > MAX_DIGEST_OUTPUT_CHARS
        and entries
    ):
        entries.pop()
        _refresh_array_samples(entries)
        result["truncated"] = True
        result["omitted_count"] += 1
        result["truncation_reason"] = "output_limit"
    return result


# --- read (bounded slice) --------------------------------------------------------


def read_sample(
    sample_path: str | Path, limit: int = DEFAULT_READ_LIMIT, json_path: str = ""
) -> str | None:
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
        results.append(
            {"path": jp, "ok": True, "type": _json_type_name(node), "value": value}
        )
    return {
        "file": str(sample_path),
        "count": len(results),
        "shown": len(results),
        "omitted": 0,
        "results": results,
    }


def batch_describe_data(
    sample_path: str | Path,
    paths: list[str],
    *,
    keys_only: bool = False,
    entry_budget: int | None = None,
) -> dict:
    """Describe subtrees from one file load, charging the full serialized envelope.

    Omitted descriptions are not successful observations. Metadata placeholders
    are retained when they fit; top-level coverage counts exclude placeholders.
    """
    sample = _load_sample(sample_path)
    results: list[dict[str, Any]] = []
    payload: dict[str, Any] = {
        "file": str(sample_path),
        "count": len(paths),
        "shown": len(paths),
        "omitted": len(paths),
        "results": results,
    }
    # Maximum-width counts plus room for final truncation flags.
    total = len(json.dumps(payload, ensure_ascii=False)) + 100
    exhausted = False
    for jp in paths:
        found, node = _select_json_path(sample, jp)
        if not found:
            row = {"path": jp, "ok": False, "error": _bounded_text(node, 400)}
        elif exhausted:
            row = {"path": jp, "omitted": True, "describe": None}
        else:
            describe = (
                _describe_keys(node, jp)
                if keys_only
                else _describe_value(node, jp, entry_budget=entry_budget)
            )
            row = (
                {"path": jp, "ok": False, "error": describe["error"]}
                if "error" in describe
                else {"path": jp, "ok": True, "describe": describe}
            )
        cost = len(json.dumps(row, ensure_ascii=False)) + 2
        if total + cost > BATCH_TOTAL_CHARS:
            exhausted = True
            row = {"path": jp, "omitted": True, "describe": None}
            cost = len(json.dumps(row, ensure_ascii=False)) + 2
        if total + cost > BATCH_TOTAL_CHARS:
            break
        total += cost
        results.append(row)
    shown = sum(not row.get("omitted", False) for row in results)
    payload.update(shown=shown, omitted=len(paths) - shown)
    if payload["omitted"]:
        payload.update(truncated=True, truncation_reason="output_limit")
    return payload


# --- -x channel (XPath over HTML; optional parsel dependency) ---------------------


SELECT_MATCH_SHOW = 30
MAX_MATCH_SHOW = 200
GREP_CONTEXT_DEFAULT = 80
GREP_CONTEXT_MAX = 1000


def _grep_hits(
    text: str, keywords: list[str], show: int, context: int
) -> dict[str, Any]:
    """Case-insensitive literal matches with bounded raw-text context windows.

    Offsets are 0-based characters into the same raw text view that plain
    ``read`` prints, so a hit can be re-sliced or XPath-located afterwards.
    Multiple keywords merge in document order; overlapping occurrences from
    different keywords are each reported (one keyword may be a substring of
    another). Script/style content is intentionally included: hits need
    surrounding-text judgment, and excluding them would hide JSON-LD/payload
    evidence.
    """
    # lower() can expand Unicode characters (e.g. İ), invalidating raw offsets.
    # Escaped literal matches on the original text provide trustworthy spans.
    found: list[tuple[int, int, str]] = []
    for keyword in keywords:
        for match in re.finditer(re.escape(keyword), text, re.IGNORECASE):
            found.append((match.start(), match.end(), keyword))
    found.sort(key=lambda item: (item[0], item[2]))
    keyword_counts: dict[str, int] = {}
    for _, _, original in found:
        keyword_counts[original] = keyword_counts.get(original, 0) + 1
    hits: list[dict[str, Any]] = []
    for index, (idx, end, original) in enumerate(found[:show]):
        hits.append(
            {
                "index": index,
                "offset": idx,
                "line": text.count("\n", 0, idx) + 1,
                "keyword": original,
                "context": text[max(0, idx - context) : min(len(text), end + context)],
            }
        )
    result: dict[str, Any] = {
        "count": len(found),
        "shown": len(hits),
        "omitted": len(found) - len(hits),
        "keywords": dict(sorted(keyword_counts.items())),
        "hits": hits,
    }
    if len(found) > len(hits):
        result.update(truncated=True, truncation_reason="match_limit")
    return result


def _xpath_install_hint() -> str:
    return (
        "the -x channel needs the optional dependency parsel for this interpreter — "
        f"run: {sys.executable} -m pip install parsel"
    )


def _resolve_html_source(sample: Any, container: str):
    """Resolve the HTML source: whole text sample, or a $-addressed HTML string.

    Returns (html, None) or (None, error_dict).
    """
    if container:
        found, node = _select_json_path(sample, container)
        if not found:
            return None, {"error": _bounded_text(node, 400)}
        if not isinstance(node, str):
            return None, {
                "error": (
                    f"{container} resolved to {_json_type_name(node)}, not a string — "
                    "the -x container must be an HTML string field"
                )
            }
        return node, None
    if isinstance(sample, str):
        return sample, None
    return None, {
        "error": (
            "parsed as JSON — the -x channel needs HTML; pair -x with a single "
            "-p pointing at an HTML string field, or use describe/read"
        )
    }


def _html_root(sample: Any, container: str = "") -> tuple[Any, dict[str, str] | None]:
    """Parse once per document; (None, None) denotes the optional dependency miss."""
    html, error = _resolve_html_source(sample, container)
    if error is not None:
        return None, error
    try:
        from parsel import Selector
    except ImportError:
        return None, None
    return Selector(text=html or "<html></html>").root, None


def _xpath_text(node: Any) -> str:
    text = str(node) if isinstance(node, str) else str(node.xpath("string(.)"))
    return " ".join(text.split())


def _xpath_result(
    root: Any,
    xpath: str,
    *,
    limit: int,
    show: int,
    attr: str | None = None,
    html_mode: bool = False,
    values_only: bool = False,
) -> dict[str, Any]:
    """One extraction contract for single, grouped and cross-file XPath reads.

    Evaluate the native result before parsel stringifies scalars: XPath strings
    and node-set text/attributes must not be confused just because both are str.
    """
    import lxml.etree as etree

    try:
        result = root.xpath(xpath, smart_strings=True)
    except (etree.XPathError, ValueError, TypeError) as exc:
        return {
            "error": _bounded_text(f"XPath error: {exc} — check the expression", 400)
        }
    if not isinstance(result, list):
        if attr is not None or html_mode:
            return {
                "error": "XPath returns a scalar; remove --attr/--html (use --text or no output flag)"
            }
        if isinstance(result, float) and not math.isfinite(result):
            return {
                "error": "XPath returned NaN/Infinity; check the numeric conversion"
            }
        value = _bounded_text(str(result), limit) if isinstance(result, str) else result
        payload = {"value": value, "type": _json_type_name(result)}
        if isinstance(result, str) and len(result) > limit:
            payload.update(truncated=True, truncation_reason="value_limit")
        return payload

    selected = result[:show]
    items: list[Any] = []
    truncated_value_indices: list[int] = []
    for index, node in enumerate(selected):
        if not isinstance(node, str) and not hasattr(node, "tag"):
            return {
                "error": "XPath returned unsupported node values; select elements, text() or @attributes"
            }
        if attr is not None:
            attributes = getattr(node, "attrib", None)
            if attributes is not None:
                value = attributes.get(attr, "")
            elif (
                getattr(node, "is_attribute", False)
                and getattr(node, "attrname", None) == attr
            ):
                value = str(node)  # /@href already extracted the requested href.
            else:
                return {
                    "error": (
                        f"--attr {attr} needs elements or matching /@{attr} results; "
                        "XPath already returns text/attributes. Remove --attr and use --text, "
                        "or select the element before extracting an attribute."
                    )
                }
        elif html_mode:
            if isinstance(node, str):
                return {
                    "error": "--html needs element matches for outer markup; select elements or use --text for attribute/text values"
                }
            value = etree.tostring(
                node, encoding="unicode", method="html", with_tail=False
            )
        else:
            value = _xpath_text(node)
        if len(value) > limit:
            truncated_value_indices.append(index)
        text = _bounded_text(value, limit)
        if values_only or attr is not None or html_mode:
            items.append(text)
        else:
            tag = getattr(node, "tag", "#text")
            items.append(
                {
                    "index": index,
                    "tag": tag if isinstance(tag, str) else "#comment",
                    "attrs": {
                        key: _bounded_text(val, limit)
                        for key, val in getattr(node, "attrib", {}).items()
                    },
                    "text": text,
                }
            )
    payload = {
        "count": len(result),
        "shown": len(items),
        "omitted": len(result) - len(items),
        "values" if values_only or attr is not None or html_mode else "matches": items,
    }
    if len(result) > len(items):
        payload.update(truncated=True, truncation_reason="match_limit")
    if truncated_value_indices:
        payload["truncated_value_indices"] = truncated_value_indices
        payload["values_truncated"] = len(truncated_value_indices)
    return payload


def _fit_output_budget(
    payload: dict[str, Any], budget: int = BATCH_TOTAL_CHARS
) -> dict[str, Any]:
    """Cap actual serialized JSON characters, preserving counts for every omission."""
    while len(json.dumps(payload, ensure_ascii=False)) > budget:
        payload.update(truncated=True, truncation_reason="output_limit")
        rows = payload.get("results", payload.get("scripts", payload.get("hits")))
        row = rows[-1] if rows else payload
        items = row.get("values", row.get("matches"))
        if items:
            items.pop()
            row.update(
                shown=len(items),
                omitted=row["count"] - len(items),
                truncated=True,
                truncation_reason="output_limit",
            )
            _refresh_clipped_values(row, len(items))
        elif rows:
            rows.pop()
            total = payload.get("file_count", payload.get("count", 0))
            payload.update(shown=len(rows), omitted=total - len(rows))
        else:
            return {
                "error": "result metadata exceeds the output budget; use shorter paths or fewer expressions",
                "truncated": True,
                "truncation_reason": "output_limit",
            }
    return payload


def _compact_text(value: str, limit: int) -> str:
    """Bound decoded text and its JSON-escaped content without losing a marker."""
    text = _bounded_text(value, limit)
    if len(json.dumps(text, ensure_ascii=False)) <= limit + 2:
        return text
    marker = "...[truncated]"
    lo, hi = 0, min(len(text), limit - len(marker))
    best = marker
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = text[:mid] + marker
        if len(json.dumps(candidate, ensure_ascii=False)) <= limit + 2:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _compact_value(value: Any, limit: int) -> Any:
    """Keep small native JSON values; oversized values become marked previews."""
    if isinstance(value, str):
        return _compact_text(value, limit)
    if isinstance(value, (dict, list)):
        preview = _bounded_json_text(value, limit + 1)
        if len(preview) > limit:
            return _compact_text(_bounded_text(preview, limit, truncated=True), limit)
    return value


def _free_metadata_key(payload: dict[str, Any], name: str) -> str:
    """Metadata must never overwrite arbitrary file/path names in a data map."""
    key = name
    suffix = 1
    while key in payload:
        key = f"{name}#{suffix}"
        suffix += 1
    return key


def _fit_compact_budget(
    values: dict[str, Any],
    *,
    hint: str | None = None,
    budget: int = BATCH_TOTAL_CHARS,
    path_maps: set[str] | None = None,
) -> tuple[dict[str, Any], int]:
    """Pack known file/path maps without interpreting arbitrary data keys."""
    payload = dict(values)
    if hint:
        payload[_free_metadata_key(payload, "__hint__")] = _bounded_text(hint, 600)
    if len(json.dumps(payload, ensure_ascii=False)) <= budget:
        return payload, 0

    # Reserve real omission metadata before packing. An oversized early entry
    # is skipped, not allowed to evict every later displayable small sibling.
    metadata_key = _free_metadata_key(payload, "__compact__")
    total = len(values)
    packed = {key: value for key, value in payload.items() if key not in values}
    coverage: dict[str, Any] = {
        "count": total,
        "shown": 0,
        "omitted": total,
        "truncated": True,
        "truncation_reason": "output_limit",
    }
    packed[metadata_key] = coverage
    if len(json.dumps(packed, ensure_ascii=False)) > budget:
        return {
            "error": "compact result metadata exceeds the output budget; split the request",
            "truncated": True,
            "truncation_reason": "output_limit",
        }, 1
    shown = 0
    for key, value in values.items():
        coverage.update(shown=shown + 1, omitted=total - shown - 1)
        if path_maps is not None and key in path_maps:
            # The caller explicitly identifies successful multi-path rows.
            # Fit their path entries into the remaining encoded slot rather
            # than treating the whole file as one indivisible value. No shape
            # or reserved-name guesses are made about captured data objects.
            packed[key] = {}
            slot_budget = budget - len(json.dumps(packed, ensure_ascii=False)) + 2
            nested, code = _fit_compact_budget(value, budget=slot_budget)
            if code:
                packed.pop(key)
                coverage.update(shown=shown, omitted=total - shown)
                continue
            packed[key] = nested
        else:
            packed[key] = value
        if len(json.dumps(packed, ensure_ascii=False)) <= budget:
            shown += 1
        else:
            packed.pop(key)
            coverage.update(shown=shown, omitted=total - shown)
    return packed, 0


def _compact_paths(
    sample: Any, paths: list[str], effective_paths: list[str], limit: int
) -> tuple[dict[str, Any], bool]:
    values: dict[str, Any] = {}
    any_found = False
    for short, effective in zip(paths, effective_paths):
        found, node = _select_json_path(sample, effective)
        values[short] = _compact_value(node, limit) if found else None
        any_found = any_found or found
    return values, any_found


def _read_compact_batch(
    files: list[str],
    root: str | None,
    paths: list[str],
    effective_paths: list[str],
    limit: int,
) -> tuple[dict[str, Any], int]:
    values: dict[str, Any] = {}
    path_maps: set[str] = set()
    all_missed_html = bool(files)
    for file in files:
        label = _display_rel(file, root)
        if label in values:
            return {
                "error": "duplicate compact file identity; use canonical file paths",
                "truncated": False,
            }, 1
        try:
            sample = _load_sample(file)
        except OSError as exc:
            values[label] = {"__error__": _bounded_text(str(exc), 200)}
            all_missed_html = False
            continue
        selected, found = _compact_paths(sample, paths, effective_paths, limit)
        values[label] = selected[paths[0]] if len(paths) == 1 else selected
        if len(paths) > 1:
            path_maps.add(label)
        if found or not (isinstance(sample, str) and _looks_like_html(sample)):
            all_missed_html = False
    hint = (
        "all paths missed and every file is an HTML string — embedded JSON lives in inline scripts; "
        "run describe --scripts --files '<glob>' to list them, then re-read with "
        "--json-root '$#script[...]' and short -p paths"
        if all_missed_html
        else None
    )
    return _fit_compact_budget(values, hint=hint, path_maps=path_maps)


def _refresh_clipped_values(row: dict[str, Any], length: int) -> None:
    """Only emitted values contribute clipped-value evidence."""
    if "truncated_value_indices" in row:
        row["truncated_value_indices"] = [
            i for i in row["truncated_value_indices"] if i < length
        ]
        row["values_truncated"] = len(row["truncated_value_indices"])


def _refresh_matrix_coverage(payload: dict[str, Any]) -> None:
    """A file is shown only when at least one of its path results is emitted."""
    shown = sum(bool(row.get("results")) for row in payload["results"])
    payload.update(shown=shown, omitted=payload["file_count"] - shown)


# --- CLI -------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # Agent contract: never a usage dump / traceback; one guidance line, exit 1.
        if "not allowed with argument" in message and any(
            flag in message for flag in ("--html", "--text", "--attr")
        ):
            message += "; choose one output mode for the entire grouped call; use separate calls for different modes"
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
    for group in args.json_path or []:
        if isinstance(group, list):
            paths.extend(p.strip() for p in group if isinstance(p, str) and p.strip())
        elif isinstance(group, str) and group.strip():
            paths.append(group.strip())
    return list(dict.fromkeys(paths))


def _apply_json_root(
    args: argparse.Namespace, paths: list[str]
) -> tuple[list[str], list[str]] | str:
    """Compose every gathered $-path onto a common ``--json-root`` prefix.

    Returns ``(short_paths, effective_paths)`` — report the short ones, resolve
    the effective ones — or a prefixed guidance line for the caller to print.
    String concat (root + p[1:]) never guesses a separator, so every short path
    must be a well-formed $-path first: a bare ``.a`` would silently turn
    ``$.data`` into ``$.dataa``.
    """
    root = getattr(args, "json_root", None)
    if not root:
        return paths, paths
    if not paths:
        return (
            "[inspect_sample] --json-root composes onto -p paths; "
            "add at least one -p $-path"
        )
    tokens = _tokenize_json_path(root)
    if isinstance(tokens, str):
        return (
            f"[inspect_sample] --json-root is not a valid path ({tokens}); "
            "e.g. --json-root '$#script[type=\"application/json\"]'"
        )
    bare = [p for p in paths if not p.startswith("$")]
    if bare:
        return (
            "[inspect_sample] --json-root composes onto $-paths; these lack the "
            f"leading $: {', '.join(bare[:5])}"
        )
    malformed = []
    for p in paths:
        if isinstance(_tokenize_json_path(p), str):
            malformed.append(p)
    if malformed:
        return (
            "[inspect_sample] --json-root composes only onto well-formed $-paths; "
            f"check: {', '.join(malformed[:5])}"
        )
    return paths, [root + p[1:] for p in paths]


def _collect_batch_files(
    args: argparse.Namespace,
) -> tuple[list[str], str | None, list[str]] | str:
    """Shared --files/--list-file collection for `read` and `describe --scripts` batches.

    Returns (files, glob_root, patterns) — glob-sorted, deduped, existing files
    (after --exclude) — or a prefixed guidance line for the caller to print.
    """
    if getattr(args, "files", None) and getattr(args, "list_file", None):
        return "[inspect_sample] use --files or --list-file, not both"
    excludes = [
        pat.strip()
        for occ in (getattr(args, "exclude", None) or [])
        for pat in (occ if isinstance(occ, list) else [occ])
        if isinstance(pat, str) and pat.strip()
    ]

    def _excluded(candidate: str) -> bool:
        return any(
            fnmatch.fnmatch(os.path.basename(candidate), pat)
            or fnmatch.fnmatch(candidate, pat)
            for pat in excludes
        )

    if getattr(args, "list_file", None):
        literal_files: list[str] = []
        with open(args.list_file, encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    literal_files.append(line)
        if excludes:
            # apply-after-collection: --exclude filters listed files too
            literal_files = [p for p in literal_files if not _excluded(p)]
        if not literal_files:
            return (
                f"[inspect_sample] no files remain after excludes: {args.list_file} | excluded by: {', '.join(excludes)}"
                if excludes
                else f"[inspect_sample] --list-file {args.list_file} contains no file paths"
            )
        missing = [p for p in literal_files if not os.path.isfile(p)]
        if missing:
            return "[inspect_sample] --list-file paths not found: " + ", ".join(
                missing[:10]
            )
        files = sorted({str(Path(f).resolve()) for f in literal_files})
        return files, _batch_display_root(files), [f"--list-file {args.list_file}"]
    patterns = [pat for occ in (args.files or []) for pat in occ]
    if not patterns:
        return "[inspect_sample] batch mode needs at least one --files glob pattern"
    files = sorted(
        {
            str(Path(f).resolve())
            for pat in patterns
            for f in glob.glob(pat)
            if Path(f).is_file()
        }
    )
    if excludes:
        files = [f for f in files if not _excluded(f)]
    if not files:
        return (
            f"[inspect_sample] no files remain after excludes: {', '.join(patterns)} | excluded by: {', '.join(excludes)}"
            if excludes
            else f"[inspect_sample] no files match: {', '.join(patterns)}"
        )
    return files, _batch_display_root(files), patterns


def _batch_display_root(files: list[str]) -> str:
    """Use the common canonical parent, identically for globs and file lists."""
    parents = [str(Path(file).resolve().parent) for file in files]
    try:
        return os.path.commonpath(parents)
    except ValueError:
        # Different Windows drives have no shared root; _display_rel retains
        # absolute identity for files on the other drives.
        return parents[0]


def _display_rel(file_path: str, root: str | None) -> str:
    """Per-file display name; cross-drive files keep their absolute path.

    os.path.relpath raises ValueError when file and root sit on different
    Windows drives — the batch must keep working with unique identities.
    """
    if not root:
        return Path(file_path).name
    try:
        return os.path.relpath(file_path, root)
    except ValueError:
        return file_path


def _read_files(args: argparse.Namespace, show: int, context: int) -> int:
    """Execute one validated batch channel, sharing source collection/limits."""
    legacy_batch = args.command != "read"
    if legacy_batch:
        print(
            "[inspect_sample] batch mode (--files) is a plain `read` feature — batch aliases do not carry it"
        )
        return 1
    if getattr(args, "files", None) and getattr(args, "list_file", None):
        print("[inspect_sample] use --files or --list-file, not both")
        return 1
    paths = _gather_paths(args)
    rooted = _apply_json_root(args, paths)
    if isinstance(rooted, str):
        print(rooted)
        return 1
    paths, effective_paths = rooted
    collected = _collect_batch_files(args)
    if isinstance(collected, str):
        print(collected)
        return 1
    files, glob_root, patterns = collected
    xpaths = [
        x.strip()
        for occ in (args.xpath or [])
        for x in (occ if isinstance(occ, list) else [occ])
        if isinstance(x, str) and x.strip()
    ]
    xpaths = list(dict.fromkeys(xpaths))
    if args.value_only and len(xpaths) > 1:
        print(
            "[inspect_sample] --value-only adds nothing to grouped -x: "
            "multi -x groups are already values-only"
        )
        return 1
    if args.grep is not None and (paths or xpaths):
        print(
            "[inspect_sample] batch --grep takes no -p path or -x expression — drop --files for grouped --grep"
        )
        return 1
    if xpaths and paths:
        print(
            "[inspect_sample] batch -x takes no -p container — extract the HTML strings first, then run -x per file"
        )
        return 1
    if not xpaths and not paths and args.grep is None:
        print(
            "[inspect_sample] batch mode needs at least one -p path (or one -x XPath)"
        )
        return 1
    if args.value_only and paths:
        limit, limit_err = _parse_limit(args.limit, BATCH_VALUE_LIMIT)
        if limit_err:
            print(limit_err)
            return 1
        vo_payload, code = _read_compact_batch(
            files, glob_root, paths, effective_paths, limit
        )
        print(json.dumps(vo_payload, ensure_ascii=False))
        return code
    if args.grep is not None:
        grep_results = []
        for fp in files:
            rel = _display_rel(fp, glob_root)
            row = {"file": rel, "path": fp}
            try:
                row.update(
                    ok=True,
                    **_grep_hits(_read_file_text(fp), args.grep, show, context),
                )
            except OSError as exc:
                row.update(
                    ok=False,
                    error=_bounded_text(f"cannot read {fp}: {exc}", 400),
                )
            grep_results.append(row)
        payload = {
            "patterns": patterns,
            "file_count": len(files),
            "shown": len(grep_results),
            "omitted": len(files) - len(grep_results),
            "results": grep_results,
        }
        print(json.dumps(_fit_output_budget(payload), ensure_ascii=False))
        return 0
    limit, limit_err = _parse_limit(args.limit, BATCH_VALUE_LIMIT)
    if limit_err:
        print(limit_err)
        return 1
    if len(xpaths) > 1:
        # Grouped multi-expression batch: every file row carries one
        # values-only group per -x (same shape as the single-file
        # grouped -x channel), so counts stay per-expression.
        grouped_results: list[dict[str, Any]] = []
        for fp in files:
            rel = _display_rel(fp, glob_root)
            grow: dict[str, Any] = {
                "file": rel,
                "path": fp,
                "ok": True,
                "groups": [],
            }
            try:
                sample = _load_sample(fp)
            except OSError as exc:
                grow.update(
                    ok=False,
                    error=_bounded_text(f"cannot read {fp}: {exc}", 400),
                )
                grouped_results.append(grow)
                continue
            root, err = _html_root(sample)
            if root is None and err is None:
                print(f"[inspect_sample] {_xpath_install_hint()}")
                return 1
            if err is not None:
                grow.update(ok=False, error=_bounded_text(err.get("error", ""), 300))
            else:
                for xp in xpaths:
                    result = _xpath_result(
                        root,
                        xp,
                        limit=limit,
                        show=show,
                        attr=args.attr,
                        html_mode=args.html,
                        values_only=True,
                    )
                    grow["groups"].append(
                        {"xpath": xp, "ok": "error" not in result, **result}
                    )
            grouped_results.append(grow)
        payload = {
            "patterns": patterns,
            "file_count": len(files),
            "shown": len(grouped_results),
            "omitted": len(files) - len(grouped_results),
            "results": grouped_results,
        }
        while len(json.dumps(payload, ensure_ascii=False)) > BATCH_TOTAL_CHARS:
            payload.update(truncated=True, truncation_reason="output_limit")
            victims = [r for r in payload["results"] if r.get("groups")]
            if not victims:
                if not payload["results"]:
                    print(
                        json.dumps(
                            _fit_output_budget(
                                {
                                    "error": "result metadata exceeds the output budget; use fewer expressions or shorter paths",
                                    "truncated": True,
                                    "truncation_reason": "output_limit",
                                }
                            ),
                            ensure_ascii=False,
                        )
                    )
                    return 1
                payload["results"].pop()
                payload["shown"] = len(payload["results"])
                payload["omitted"] = len(files) - payload["shown"]
                continue
            row = victims[-1]
            group = row["groups"][-1]
            items = group.get("values")
            if items:
                items.pop()
                group.update(
                    shown=len(items),
                    omitted=group["count"] - len(items),
                    truncated=True,
                )
                _refresh_clipped_values(group, len(items))
                if "truncation_reason" not in group:
                    # a group already value-limited by --show keeps
                    # its original match_limit disclosure
                    group["truncation_reason"] = "output_limit"
            else:
                row["groups"].pop()
                if not row["groups"]:
                    # mirror _fit_output_budget: a row whose last
                    # group was popped leaves the payload entirely
                    payload["results"].remove(row)
                    payload["shown"] = len(payload["results"])
                    payload["omitted"] = len(files) - payload["shown"]
        print(json.dumps(_fit_output_budget(payload), ensure_ascii=False))
        return 0
    results = []
    shown = 0
    type_summary: dict[str, int] = {}
    type_summaries: dict[str, dict[str, int]] = {}
    if len(paths) > 1:
        # Multi-path matrix: nested per-file results with per-path
        # cross-file type summaries surfacing shape variance.
        matrix_results: list[dict[str, Any]] = []
        for fp in files:
            rel = _display_rel(fp, glob_root)
            row: dict[str, Any] = {
                "file": rel,
                "path": fp,
                "ok": True,
                "shown": len(paths),
                "omitted": 0,
                "results": [],
            }
            try:
                sample = _load_sample(fp)
            except OSError as exc:
                row.update(
                    ok=False,
                    error=_bounded_text(f"cannot read {fp}: {exc}", 400),
                    shown=0,
                    omitted=len(paths),
                )
                matrix_results.append(row)
                continue
            for jp, ep in zip(paths, effective_paths):
                found, node = _select_json_path(sample, ep)
                if not found:
                    row["results"].append(
                        {
                            "path": jp,
                            "ok": False,
                            "error": _bounded_text(node, 300),
                        }
                    )
                    continue
                tname = _json_type_name(node)
                per_path = type_summaries.setdefault(jp, {})
                per_path[tname] = per_path.get(tname, 0) + 1
                row["results"].append(
                    {
                        "path": jp,
                        "ok": True,
                        "type": tname,
                        "value": _bounded_text(
                            node
                            if isinstance(node, str)
                            else _bounded_json_text(node, limit),
                            limit,
                        ),
                        **(
                            {"len": len(node)}
                            if isinstance(node, list)
                            else {"key_count": len(node)}
                            if isinstance(node, dict)
                            else {}
                        ),
                    }
                )
            shown_paths = len(row["results"])
            row.update(shown=shown_paths, omitted=len(paths) - shown_paths)
            if row["omitted"]:
                row.update(truncated=True, truncation_reason="output_limit")
            matrix_results.append(row)
        payload = {
            "patterns": patterns,
            "file_count": len(files),
            "path_count": len(paths),
            "shown": sum(1 for row in matrix_results if row.get("shown")),
            "omitted": len(files)
            - sum(1 for row in matrix_results if row.get("shown")),
            "results": matrix_results,
        }
        if type_summaries:
            payload["type_summaries"] = {
                jp: dict(sorted(types.items())) for jp, types in type_summaries.items()
            }
        while len(json.dumps(payload, ensure_ascii=False)) > BATCH_TOTAL_CHARS:
            payload.update(truncated=True, truncation_reason="output_limit")
            victims = [r for r in payload["results"] if r.get("shown")]
            if not victims:
                if not payload["results"]:
                    print(
                        json.dumps(
                            _fit_output_budget(
                                {
                                    "error": "result metadata exceeds the output budget; use fewer paths or shorter paths",
                                    "truncated": True,
                                    "truncation_reason": "output_limit",
                                }
                            ),
                            ensure_ascii=False,
                        )
                    )
                    return 1
                payload["results"].pop()
                payload["shown"] = sum(
                    1 for row in payload["results"] if row.get("shown")
                )
                payload["omitted"] = len(files) - payload["shown"]
                continue
            row = victims[-1]
            row["results"].pop()
            row["shown"] = len(row["results"])
            row["omitted"] = len(paths) - row["shown"]
            row.update(truncated=True, truncation_reason="output_limit")
            _refresh_matrix_coverage(payload)
        _refresh_matrix_coverage(payload)
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    for fp in files:
        rel = _display_rel(fp, glob_root)
        row: dict[str, Any] = {"file": rel, "path": fp}
        try:
            sample = _load_sample(fp)
        except OSError as exc:
            row.update(
                ok=False,
                error=_bounded_text(f"cannot read {fp}: {exc}", 400),
            )
            results.append(row)
            shown += 1
            continue
        if xpaths:
            root, err = _html_root(sample)
            if root is None and err is None:
                print(f"[inspect_sample] {_xpath_install_hint()}")
                return 1
            elif err is not None:
                row.update(ok=False, error=_bounded_text(err.get("error", ""), 300))
            else:
                result = _xpath_result(
                    root,
                    xpaths[0],
                    limit=limit,
                    show=show,
                    attr=args.attr,
                    html_mode=args.html,
                    values_only=True,
                )
                row.update(result, ok="error" not in result)
        else:
            found, node = _select_json_path(sample, effective_paths[0])
            if not found:
                row.update(ok=False, error=_bounded_text(node, 300))
            else:
                tname = _json_type_name(node)
                type_summary[tname] = type_summary.get(tname, 0) + 1
                row.update(
                    ok=True,
                    type=tname,
                    value=_bounded_text(
                        node
                        if isinstance(node, str)
                        else _bounded_json_text(node, limit),
                        limit,
                    ),
                )
                if isinstance(node, list):
                    row["len"] = len(node)
                elif isinstance(node, dict):
                    row["key_count"] = len(node)
        shown += 1
        results.append(row)
    payload = {
        "patterns": patterns,
        "file_count": len(files),
        "shown": shown,
        "omitted": len(files) - shown,
        "results": results,
    }
    if type_summary:
        payload["type_summary"] = type_summary
    print(json.dumps(_fit_output_budget(payload), ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:  # noqa: C901, PLR0912 — single-file CLI dispatch
    parser = _Parser(
        prog="inspect_sample.py",
        description="Bounded structure inspection of a captured response file.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_describe = sub.add_parser(
        "describe",
        aliases=["batch_describe"],
        help="bounded path->type->sample map: whole file, one subtree, a path list, or XPath over HTML",
    )
    p_describe.add_argument(
        "file",
        nargs="?",
        default=None,
        help="response file (JSON or HTML); omit only in --scripts --files batch mode",
    )
    p_describe.add_argument(
        "-p",
        "--json-path",
        action="append",
        nargs="*",
        default=[],
        help="one or more subtree json_paths per occurrence; repeatable",
    )
    p_describe.add_argument(
        "-x",
        "--xpath",
        default=None,
        help="XPath over HTML instead of $ paths (optional parsel; pair with one -p container for JSON captures)",
    )
    p_describe.add_argument(
        "--paths-file",
        default=None,
        help="text file with one json_path per line (# comments and blank lines allowed)",
    )
    p_describe.add_argument(
        "--files",
        action="append",
        nargs="*",
        default=[],
        help="one or more glob patterns per occurrence; with --scripts: compact per-file script summaries",
    )
    p_describe.add_argument(
        "--list-file",
        default=None,
        help="literal file paths, one per line (# comments/blanks skipped) — with --scripts: batch the listed files",
    )
    p_describe.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="with --files: exclude collected files whose name matches this pattern (repeatable)",
    )
    describe_modes = p_describe.add_mutually_exclusive_group()
    describe_modes.add_argument(
        "--scripts",
        action="store_true",
        help="HTML only: enumerate inline script candidates and stable id_paths without walking them",
    )
    describe_modes.add_argument(
        "--keys",
        action="store_true",
        help="object only: bounded key names without descending into values; works with -p and path lists",
    )
    p_describe.add_argument(
        "--limit",
        default=None,
        help=f"per-path entry budget for describe (default {DIGEST_ENTRY_BUDGET}; clamp 200..{MAX_ENTRY_BUDGET})",
    )
    p_read = sub.add_parser(
        "read",
        aliases=["batch", "batch_read"],
        help="bounded slice of one subtree, per-path values for a path list, or XPath matches over HTML",
    )
    p_read.add_argument(
        "file",
        nargs="?",
        default=None,
        help="response file (JSON or HTML); omit only in --files batch mode",
    )
    p_read.add_argument(
        "-p",
        "--json-path",
        action="append",
        nargs="*",
        default=[],
        help="one or more json_paths per occurrence; repeatable",
    )
    p_read.add_argument(
        "-x",
        "--xpath",
        action="append",
        nargs="*",
        default=None,
        help="XPath over HTML instead of $ paths (optional parsel); repeatable = grouped per-expression results; one -p container allowed for JSON captures",
    )
    p_read.add_argument(
        "--paths-file",
        default=None,
        help="text file with one json_path per line (# comments and blank lines allowed)",
    )
    p_read.add_argument(
        "--json-root",
        default=None,
        help="common path prefix composed onto every -p path ($-paths only; output keeps reporting the short paths); not with -x/--grep",
    )
    p_read.add_argument(
        "--files",
        action="append",
        nargs="*",
        default=[],
        help="one or more glob patterns per occurrence; repeatable — every matching file is processed",
    )
    x_group = p_read.add_mutually_exclusive_group()
    x_group.add_argument(
        "--attr", default=None, help="with -x: extract this attribute from every match"
    )
    x_group.add_argument(
        "--text", action="store_true", help="with -x: extract each match's text content"
    )
    x_group.add_argument(
        "--html", action="store_true", help="with -x: extract each match's outer HTML"
    )
    p_read.add_argument(
        "--value-only",
        action="store_true",
        help="with -p (multi-path or --files batch): output only {path: value} pairs without structural metadata",
    )
    p_read.add_argument(
        "--limit",
        default=None,
        help=f"characters per value (single default {DEFAULT_READ_LIMIT}; lists/XPath {BATCH_VALUE_LIMIT}; positive values clamp {LIMIT_MIN}..{MAX_READ_SAMPLE_CHARS})",
    )
    for command in (p_read, p_describe):
        command.add_argument(
            "--show",
            default=None,
            help=f"with -x: matches shown per expression (default {SELECT_MATCH_SHOW}; clamp 1..{MAX_MATCH_SHOW})",
        )
    p_read.add_argument(
        "--grep",
        action="append",
        default=None,
        help="case-insensitive literal keyword search over the raw text view (bounded context hits; repeatable = merged multi-keyword hits; works with --files)",
    )
    p_read.add_argument(
        "--list-file",
        default=None,
        help="literal file paths, one per line (# comments/blanks skipped) — batch an arbitrary file subset without glob",
    )
    p_read.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="with --files: exclude collected files whose name matches this pattern (repeatable)",
    )
    p_read.add_argument(
        "--context",
        default=None,
        help=f"with --grep: context characters on each side of a hit (default {GREP_CONTEXT_DEFAULT}; clamp 0..{GREP_CONTEXT_MAX})",
    )
    args = parser.parse_args(argv)
    args.grep = getattr(args, "grep", None)
    args.context = getattr(args, "context", None)
    if args.xpath is not None:
        groups = [[args.xpath]] if isinstance(args.xpath, str) else args.xpath
        if any(
            not group or any(not expression.strip() for expression in group)
            for group in groups
        ):
            print(
                "[inspect_sample] -x needs a non-empty XPath expression in every occurrence"
            )
            return 1
    if args.grep is not None:
        if any(not keyword.strip() for keyword in args.grep):
            print("[inspect_sample] --grep needs a non-empty literal keyword")
            return 1
        seen: dict[str, str] = {}  # lowercase → first-occurring original form
        for keyword in args.grep:
            seen.setdefault(keyword.lower(), keyword)
        args.grep = list(seen.values())  # case-insensitive dedupe, preserve casing
        if args.xpath or args.attr is not None or args.text or args.html:
            print(
                "[inspect_sample] --grep is its own channel; it cannot be combined with -p/-x/--attr/--text/--html"
            )
            return 1
    context = GREP_CONTEXT_DEFAULT
    if args.context is not None:
        if args.grep is None:
            print("[inspect_sample] --context bounds --grep hits; pair it with --grep")
            return 1
        try:
            context = int(str(args.context).strip())
        except ValueError:
            print("[inspect_sample] --context must be a non-negative integer")
            return 1
        if context < 0:
            print("[inspect_sample] --context must be a non-negative integer")
            return 1
        context = min(context, GREP_CONTEXT_MAX)
    show = SELECT_MATCH_SHOW
    if args.show is not None:
        if not args.xpath and args.grep is None:
            print(
                "[inspect_sample] --show controls XPath/grep matches; pair it with -x or --grep"
            )
            return 1
        try:
            show = int(str(args.show).strip())
        except ValueError:
            print("[inspect_sample] --show must be a positive integer")
            return 1
        if show < 1:
            print("[inspect_sample] --show must be a positive integer")
            return 1
        show = min(show, MAX_MATCH_SHOW)

    if (
        not args.file
        and not getattr(args, "files", None)
        and not getattr(args, "list_file", None)
    ):
        print(
            "[inspect_sample] file is required (or use --files/--list-file for batch mode)"
        )
        return 1
    path = Path(args.file) if args.file else None
    if path is not None and not path.is_file():
        print(f"[inspect_sample] file not found: {args.file}")
        return 1
    try:
        if args.command in ("describe", "batch_describe"):
            legacy_batch = args.command == "batch_describe"
            desc_budget = DIGEST_ENTRY_BUDGET
            if getattr(args, "limit", None) is not None and hasattr(args, "keys"):
                try:
                    desc_budget = int(str(args.limit).strip())
                except ValueError:
                    print("[inspect_sample] --limit must be a positive integer")
                    return 1
                if desc_budget < 200:
                    desc_budget = 200
                desc_budget = min(desc_budget, MAX_ENTRY_BUDGET)
            if args.keys and args.xpath:
                print(
                    "[inspect_sample] --keys describes JSON object keys; use -p, not -x"
                )
                return 1
            if getattr(args, "files", None) or getattr(args, "list_file", None):
                if legacy_batch:
                    print(
                        "[inspect_sample] batch mode (--files) is a plain `describe` feature — batch aliases do not carry it"
                    )
                    return 1
                if not getattr(args, "scripts", False):
                    print(
                        "[inspect_sample] describe --files/--list-file batch mode needs --scripts (compact per-file script summaries)"
                    )
                    return 1
                if (
                    _gather_paths(args)
                    or getattr(args, "paths_file", None)
                    or getattr(args, "limit", None) is not None
                ):
                    print(
                        "[inspect_sample] describe --scripts --files takes no "
                        "-p/--paths-file/--limit — script summaries carry no subtree paths"
                    )
                    return 1
                if path is not None:
                    print(
                        "[inspect_sample] describe --scripts --files takes no positional file — pass only the glob or list"
                    )
                    return 1
                collected = _collect_batch_files(args)
                if isinstance(collected, str):
                    print(collected)
                    return 1
                files, glob_root, _patterns = collected
                script_rows: list[dict[str, Any]] = []
                for fp in files:
                    rel = _display_rel(fp, glob_root)
                    srow: dict[str, Any] = {"file": rel, "path": fp, "scripts": []}
                    try:
                        sample = _load_sample(fp)
                    except OSError as exc:
                        srow["error"] = _bounded_text(f"cannot read {fp}: {exc}", 200)
                        script_rows.append(srow)
                        continue
                    if not (isinstance(sample, str) and _looks_like_html(sample)):
                        srow["error"] = (
                            "not HTML — --scripts enumerates embedded JSON <script> blocks in HTML captures"
                        )
                        script_rows.append(srow)
                        continue
                    srow["scripts"] = [
                        {
                            "index": index,
                            "id": s["id"],
                            "type": s["type"],
                            "parse_ok": bool(s.get("ok")),
                            "length": s["length"],
                        }
                        for index, s in enumerate(_embedded_json_scripts(sample))
                    ]
                    script_rows.append(srow)
                payload = {
                    "file_count": len(files),
                    "shown": len(script_rows),
                    "omitted": len(files) - len(script_rows),
                    "results": script_rows,
                }
                print(json.dumps(_fit_output_budget(payload), ensure_ascii=False))
                return 0
            assert (
                path is not None
            )  # Only read and describe --scripts accept --files without a positional file.
            if getattr(args, "scripts", False):
                containers = _gather_paths(args)
                if len(containers) > 1:
                    print(
                        "[inspect_sample] --scripts takes at most one -p container path"
                    )
                    return 1
                script_root = containers[0] if containers else "$"
                sample = _load_sample(path)
                if containers:
                    found, sample = _select_json_path(sample, script_root)
                    if not found:
                        print(json.dumps({"error": sample}, ensure_ascii=False))
                        return 1
                if not (isinstance(sample, str) and _looks_like_html(sample)):
                    print(
                        json.dumps(
                            {
                                "error": "this file did not parse as HTML — --scripts enumerates embedded JSON <script> blocks in HTML captures"
                            },
                            ensure_ascii=False,
                        )
                    )
                    return 1
                scripts = _embedded_json_scripts(sample)
                listing = []
                for index, s in enumerate(scripts):
                    row: dict[str, Any] = {
                        "index": index,
                        "script_id": s["id"],
                        "script_type": s["type"],
                        "length": s["length"],
                        "parse_error": bool(s.get("parse_error")),
                    }
                    if s.get("id") is not None:
                        row["id_path"] = (
                            f"{script_root}#script[id={json.dumps(s['id'], ensure_ascii=False)}]"
                        )
                    if s.get("type") is not None:
                        row["type_path"] = (
                            f"{script_root}#script[type={json.dumps(s['type'], ensure_ascii=False)}]"
                        )
                    if s.get("ok"):
                        row["json_bytes"] = s["byte_length"]
                    else:
                        row["head"] = s.get("head", "")
                    listing.append(row)
                payload = {
                    "file": str(path),
                    "count": len(listing),
                    "shown": len(listing),
                    "omitted": 0,
                    "scripts": listing,
                }
                print(
                    json.dumps(
                        _fit_output_budget(payload, MAX_DIGEST_OUTPUT_CHARS),
                        ensure_ascii=False,
                    )
                )
                return 0
            if args.xpath:
                if legacy_batch:
                    print(
                        "[inspect_sample] the -x channel is a plain `describe` feature — batch aliases do not carry it"
                    )
                    return 1
                containers = _gather_paths(args)
                if len(containers) > 1:
                    print(
                        "[inspect_sample] the -x channel takes at most one -p container path"
                    )
                    return 1
                sample = _load_sample(path)
                root, err = _html_root(sample, containers[0] if containers else "")
                if root is None and err is None:
                    print(f"[inspect_sample] {_xpath_install_hint()}")
                    return 1
                if err is not None:
                    print(json.dumps(err, ensure_ascii=False))
                    return 1
                result = _xpath_result(
                    root, args.xpath, limit=BATCH_VALUE_LIMIT, show=show
                )
                if "error" in result:
                    print(json.dumps(result, ensure_ascii=False))
                    return 1
                print(
                    json.dumps(
                        _describe_value(result, "$", entry_budget=desc_budget),
                        ensure_ascii=False,
                    )
                )
                return 0
            paths = _gather_paths(args)
            if not paths:
                if legacy_batch:
                    print(
                        "[inspect_sample] no paths given — use -p/--json-path or --paths-file"
                    )
                    return 1
                result = describe_data(
                    path, keys_only=args.keys, entry_budget=desc_budget
                )
                print(json.dumps(result, ensure_ascii=False))
                return 1 if args.keys and "error" in result else 0
            if len(paths) == 1 and not legacy_batch:
                result = describe_data(
                    path,
                    json_path=paths[0],
                    keys_only=args.keys,
                    entry_budget=desc_budget,
                )
                print(json.dumps(result, ensure_ascii=False))
                return 1 if args.keys and "error" in result else 0
            print(
                json.dumps(
                    batch_describe_data(
                        path, paths, keys_only=args.keys, entry_budget=desc_budget
                    ),
                    ensure_ascii=False,
                )
            )
            return 0
        if args.command in ("read", "batch", "batch_read"):
            legacy_batch = args.command != "read"
            if getattr(args, "json_root", None) and (
                args.xpath or args.grep is not None
            ):
                print(
                    "[inspect_sample] --json-root composes $-paths only; "
                    "it cannot be combined with -x/--grep"
                )
                return 1
            if (args.attr is not None or args.text or args.html) and not args.xpath:
                print(
                    "[inspect_sample] --attr/--text/--html extract from matched HTML elements — pair them with -x"
                )
                return 1
            if getattr(args, "files", None) or getattr(args, "list_file", None):
                return _read_files(args, show, context)
            assert path is not None  # --files branches above always return.
            legacy_batch = args.command != "read"
            if args.attr is not None or args.text or args.html:
                if not args.xpath:
                    print(
                        "[inspect_sample] --attr/--text/--html extract from matched HTML elements — pair them with -x"
                    )
                    return 1
                if legacy_batch:
                    print(
                        "[inspect_sample] the -x channel is a plain `read` feature — batch aliases do not carry it"
                    )
                    return 1
            if args.grep is not None:
                if _gather_paths(args):
                    print(
                        "[inspect_sample] --grep searches the raw text view; it takes no -p path"
                    )
                    return 1
                result = _grep_hits(_read_file_text(path), args.grep, show, context)
                payload = {"file": str(path), "grep": args.grep, **result}
                print(json.dumps(_fit_output_budget(payload), ensure_ascii=False))
                return 0
            if args.xpath:
                containers = _gather_paths(args)
                if len(containers) > 1:
                    print(
                        "[inspect_sample] the -x channel takes at most one -p container path"
                    )
                    return 1
                limit, limit_err = _parse_limit(args.limit, BATCH_VALUE_LIMIT)
                if limit_err:
                    print(limit_err)
                    return 1
                sample = _load_sample(path)
                xpaths = [
                    x.strip()
                    for occ in (args.xpath or [])
                    for x in (occ if isinstance(occ, list) else [occ])
                    if isinstance(x, str) and x.strip()
                ]
                xpaths = list(dict.fromkeys(xpaths))
                if not xpaths:
                    print("[inspect_sample] -x needs at least one XPath expression")
                    return 1
                root, src_err = _html_root(sample, containers[0] if containers else "")
                if src_err is not None:
                    print(json.dumps(src_err, ensure_ascii=False))
                    return 1
                if root is None:
                    print(f"[inspect_sample] {_xpath_install_hint()}")
                    return 1
                if len(xpaths) == 1:
                    result = _xpath_result(
                        root,
                        xpaths[0],
                        limit=limit,
                        show=show,
                        attr=args.attr,
                        html_mode=args.html,
                        values_only=args.text,
                    )
                    payload = {"file": str(path), "xpath": xpaths[0], **result}
                    print(json.dumps(_fit_output_budget(payload), ensure_ascii=False))
                    return 1 if "error" in result else 0
                results = []
                for xp in xpaths:
                    result = _xpath_result(
                        root,
                        xp,
                        limit=limit,
                        show=show,
                        attr=args.attr,
                        html_mode=args.html,
                        values_only=True,
                    )
                    results.append({"xpath": xp, "ok": "error" not in result, **result})
                payload = {
                    "file": str(path),
                    "count": len(xpaths),
                    "shown": len(results),
                    "omitted": 0,
                    "results": results,
                }
                print(json.dumps(_fit_output_budget(payload), ensure_ascii=False))
                return 0
            paths = _gather_paths(args)
            rooted = _apply_json_root(args, paths)
            if isinstance(rooted, str):
                print(rooted)
                return 1
            paths, effective_paths = rooted
            default_limit = (
                BATCH_VALUE_LIMIT
                if (legacy_batch or len(paths) > 1)
                else DEFAULT_READ_LIMIT
            )
            limit, limit_err = _parse_limit(args.limit, default_limit)
            if limit_err:
                print(limit_err)
                return 1
            if args.value_only:
                sample = _load_sample(path)
                vo, _ = _compact_paths(sample, paths, effective_paths, limit)
                vo_payload, code = _fit_compact_budget(vo)
                print(json.dumps(vo_payload, ensure_ascii=False))
                return code
            if not paths:
                if legacy_batch:
                    print(
                        "[inspect_sample] no paths given — use -p/--json-path or --paths-file"
                    )
                    return 1
                out = read_sample(path, min(limit, MAX_READ_SAMPLE_CHARS))
                if out is not None:
                    print(out)
                return 0
            if len(paths) == 1 and not legacy_batch:
                out = read_sample(
                    path,
                    min(limit, MAX_READ_SAMPLE_CHARS),
                    json_path=effective_paths[0],
                )
                if out is not None:
                    print(out)
                return 0
            payload = batch_read(
                path, effective_paths, min(limit, MAX_READ_SAMPLE_CHARS)
            )
            for result_row, short in zip(payload["results"], paths):
                result_row["path"] = short  # report the original short paths
            print(json.dumps(_fit_output_budget(payload), ensure_ascii=False))
            return 0
        print(f"[inspect_sample] unknown command: {args.command}")
        return 1
    except OSError as exc:
        print(f"[inspect_sample] cannot read {args.file}: {exc}")
        return 1
    except RecursionError:
        print(
            "[inspect_sample] JSON too deeply nested for this interpreter; use --keys or --grep to survey instead of descending"
        )
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
            getattr(_stream, "reconfigure")(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass  # benign: non-standard stream (e.g. captured buffer) without reconfigure
    sys.exit(main())
