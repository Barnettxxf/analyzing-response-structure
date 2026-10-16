#!/usr/bin/env python3
"""Regression tests for scripts/inspect_sample.py (stdlib-only, any Python 3.9+).

Run with any interpreter from anywhere:
    python tests/test_inspect_sample.py
Every case spawns the script as a subprocess and asserts the agent-facing
contract: bounded outputs, guidance-not-traceback errors, exit codes, and the
embedded-JSON extraction rules documented in the script header.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SKILL_DIR / "scripts" / "inspect_sample.py"

JSON_SAMPLE = {
    "code": 0,
    "message": "success",
    "data": {
        "category": "kbd",
        "items": [
            {"id": 1, "price": "1,299.00", "rating": 4.5},
            {"id": 2, "price": None, "extra": {"a": 1}, "tags": ["x", "y"]},
            {"id": 3, "price": "10.00", "tags": "solo"},
        ],
        "pagination": {"page": 1, "has_more": True},
    },
    "next_cursor": "abc",
}

HTML_SAMPLE = """<!DOCTYPE html>
<html>
<head>
<script id="__NEXT_DATA__" type="application/json">{"props": {"x": 1}}</script>
<script type="application/ld+json">{"@type": "Product", "name": "kbd"}</script>
<!-- <script type="application/json">{"commented": true}</script> -->
</head>
<body>
<script>window.__CFG__ = {"assigned": true};</script>
<script type="application/json">not json at all</script>
<p>hello</p>
</body>
</html>
"""


try:
    import parsel  # noqa: F401

    HAVE_PARSEL = True
except ImportError:
    HAVE_PARSEL = False

HTML_PAGE = """<html><body>
<ul class="prices">
  <li class="price">$1</li>
  <li class="price">$2</li>
  <li class="price">$3</li>
</ul>
<a href="/p/1">one</a><a href="/p/2">two</a>
<div class="card"><h2>Card A</h2></div>
<div class="card"><h2>Card B</h2></div>
</body></html>"""


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


class InspectSampleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.json_file = self.dir / "api.json"
        self.json_file.write_text(json.dumps(JSON_SAMPLE), encoding="utf-8")
        self.html_file = self.dir / "page.html"
        self.html_file.write_text(HTML_SAMPLE, encoding="utf-8")
        self.html_page = self.dir / "shop.html"
        self.html_page.write_text(HTML_PAGE, encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # --- describe -----------------------------------------------------------

    def test_describe_json_shape_and_bound(self):
        res = run("describe", str(self.json_file))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertLessEqual(len(res.stdout), 24_100)  # ceiling 24k + newline slack
        payload = json.loads(res.stdout)
        self.assertEqual(payload["shape"], "object")
        paths = {entry["path"] for entry in payload["entries"]}
        self.assertIn("$", paths)
        self.assertIn("$.data.items[0]", paths)
        self.assertFalse(payload["truncated"])

    def test_describe_html_extracts_only_real_json_scripts(self):
        res = run("describe", str(self.html_file))
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        script_roots = {
            e["path"]: e
            for e in payload["entries"]
            if re.fullmatch(r"\$#script\[\d+\]", e["path"])
        }
        self.assertEqual(set(script_roots), {"$#script[0]", "$#script[1]"})
        self.assertEqual(script_roots["$#script[0]"]["script_id"], "__NEXT_DATA__")
        self.assertEqual(script_roots["$#script[1]"]["script_type"], "application/ld+json")
        # commented-out script, JS assignment (window.__CFG__), and malformed
        # JSON script must all be excluded — exactly 2 collected.

    def test_describe_json_path_subtree(self):
        res = run("describe", str(self.json_file), "--json-path", "$.data.pagination")
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        paths = {entry["path"] for entry in payload["entries"]}
        self.assertIn("$.data.pagination.has_more", paths)

    def test_describe_large_array_stays_bounded(self):
        big = self.dir / "big.json"
        big.write_text(
            json.dumps({"data": {"items": [{"id": i, "v": i % 7} for i in range(3000)]}}),
            encoding="utf-8",
        )
        res = run("describe", str(big))
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertLessEqual(len(payload["entries"]), 200)
        self.assertLessEqual(len(res.stdout), 24_100)

    def test_bom_and_non_utf8_degrade_without_crash(self):
        bom = self.dir / "bom.json"
        bom.write_bytes(b"\xef\xbb\xbf" + json.dumps({"ok": 1}).encode("utf-8"))
        gbk = self.dir / "gbk.log"
        gbk.write_bytes("价格表：<html>".encode("gbk"))
        for path in (bom, gbk):
            res = run("describe", str(path))
            self.assertEqual(res.returncode, 0, res.stderr)

    # --- read ---------------------------------------------------------------

    def test_read_deep_child(self):
        res = run("read", str(self.json_file), "--json-path", "$.data.items[1].extra")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout), {"a": 1})

    def test_read_truncation_marker_and_clamp(self):
        res = run("read", str(self.json_file), "--json-path", "$.data.items[0]", "--limit", "20")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(res.stdout.strip().endswith("...[truncated]"))
        huge = run("read", str(self.json_file), "--limit", "999999")
        self.assertEqual(huge.returncode, 0)
        self.assertLessEqual(len(huge.stdout), 20_100)

    def test_read_default_limit_is_2000(self):
        res = run("read", str(self.json_file))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertLessEqual(len(res.stdout), 2100)

    def test_read_path_miss_returns_keys_hint_exit0(self):
        res = run("read", str(self.json_file), "--json-path", "$.data.itemz")
        self.assertEqual(res.returncode, 0)  # guidance text, not a process error
        self.assertIn("available keys", res.stdout)
        self.assertIn("items", res.stdout)

    def test_read_index_out_of_range_returns_length(self):
        res = run("read", str(self.json_file), "--json-path", "$.data.items[99]")
        self.assertEqual(res.returncode, 0)
        self.assertIn("list length=3", res.stdout)

    def test_read_script_selector_out_of_range_lists_available(self):
        res = run("read", str(self.html_file), "--json-path", "$#script[5]")
        self.assertEqual(res.returncode, 0)
        self.assertIn("available:", res.stdout)
        self.assertIn("#script[0]", res.stdout)

    def test_read_dump_response_convention(self):
        dump = self.dir / "dump.json"
        dump.write_text(
            json.dumps({"task": {"fid": "f1"}, "response": "<html>body</html>"}),
            encoding="utf-8",
        )
        res = run("read", str(dump), "--limit", "50")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("<html>body</html>", res.stdout)

    # --- CLI contract ---------------------------------------------------------

    def test_missing_file_exit1(self):
        res = run("describe", str(self.dir / "nope.json"))
        self.assertEqual(res.returncode, 1)
        self.assertIn("file not found", res.stdout)

    def test_invalid_limit_exit1(self):
        res = run("read", str(self.json_file), "--limit", "abc")
        self.assertEqual(res.returncode, 1)
        self.assertIn("limit", res.stdout)

    def test_unknown_flag_single_line_exit1(self):
        res = run("describe", str(self.json_file), "--selector", "h1")
        self.assertEqual(res.returncode, 1)  # --selector intentionally not ported
        self.assertLessEqual(len(res.stdout.strip().splitlines()), 2)
        self.assertNotIn("Traceback", res.stdout)
        self.assertNotIn("usage:", res.stdout)

    def test_no_args_exit1(self):
        res = run()
        self.assertEqual(res.returncode, 1)
        self.assertNotIn("Traceback", res.stdout)

    def test_directory_argument_exit1(self):
        res = run("describe", str(self.dir))
        self.assertEqual(res.returncode, 1)
        self.assertNotIn("Traceback", res.stdout)

    def test_empty_file_degrades(self):
        empty = self.dir / "empty.json"
        empty.write_text("", encoding="utf-8")
        res = run("describe", str(empty))
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_invalid_json_degrades_to_text(self):
        bad = self.dir / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        res = run("describe", str(bad))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("text", res.stdout)

    def test_read_leaf_scalar(self):
        res = run("read", str(self.json_file), "--json-path", "$.code")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "0")

    def test_read_composed_response_path(self):
        dump = self.dir / "dump2.json"
        dump.write_text(
            json.dumps({"response": {"items": [{"a": 1}, {"b": 2}]}}), encoding="utf-8"
        )
        res = run("read", str(dump), "--json-path", "$.response.items[1]")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout), {"b": 2})

    def test_batch_multi_path_with_types(self):
        res = run("batch", str(self.json_file), "--json-path", "$.code",
                  "--json-path", "$.data.items[1].price",
                  "--json-path", "$.data.items[1].extra.a")
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertEqual(payload["count"], 3)
        by = {r["path"]: r for r in payload["results"]}
        self.assertEqual(by["$.code"]["type"], "integer")
        self.assertEqual(by["$.data.items[1].price"]["type"], "null")
        self.assertEqual(by["$.data.items[1].extra.a"]["value"], "1")

    def test_batch_not_found_is_per_path(self):
        res = run("batch", str(self.json_file), "--json-path", "$.code",
                  "--json-path", "$.data.nope")
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertTrue(payload["results"][0]["ok"])
        self.assertFalse(payload["results"][1]["ok"])
        self.assertIn("available keys", payload["results"][1]["error"])

    def test_batch_paths_file_skips_comments(self):
        pf = self.dir / "paths.txt"
        pf.write_text("# comment\n\n$.code\n   \n$.message\n", encoding="utf-8")
        res = run("batch", str(self.json_file), "--paths-file", str(pf))
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertEqual(payload["count"], 2)

    def test_batch_value_truncation(self):
        long_val = self.dir / "long.json"
        long_val.write_text(json.dumps({"k": "x" * 500}), encoding="utf-8")
        res = run("batch", str(long_val), "--json-path", "$.k", "--limit", "20")
        self.assertEqual(res.returncode, 0)
        self.assertIn("...[truncated]", res.stdout)

    def test_batch_no_paths_exit1(self):
        res = run("batch", str(self.json_file))
        self.assertEqual(res.returncode, 1)
        self.assertNotIn("Traceback", res.stdout)

    def test_batch_read_canonical_name(self):
        res = run("batch_read", str(self.json_file), "--json-path", "$.code")
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["type"], "integer")

    def test_batch_describe_multi_and_miss(self):
        res = run("batch_describe", str(self.json_file),
                  "--json-path", "$.data.items[0]", "--json-path", "$.data.nope")
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertEqual(payload["count"], 2)
        by = {r["path"]: r for r in payload["results"]}
        self.assertTrue(by["$.data.items[0]"]["ok"])
        self.assertIn("$.data.items[0].id", {e["path"] for e in by["$.data.items[0]"]["describe"]["entries"]})
        self.assertFalse(by["$.data.nope"]["ok"])
        self.assertIn("available keys", by["$.data.nope"]["error"])

    def test_batch_describe_paths_file(self):
        pf = self.dir / "subtrees.txt"
        pf.write_text("# subtrees\n\n$.data.items[0]\n   \n$.data.pagination\n", encoding="utf-8")
        res = run("batch_describe", str(self.json_file), "--paths-file", str(pf))
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertEqual(payload["count"], 2)
        self.assertTrue(all(r["ok"] for r in payload["results"]))

    def test_batch_describe_output_budget(self):
        fat = self.dir / "fat.json"
        fat.write_text(
            json.dumps({f"b{i}": {f"k{j}": "v" * 100 for j in range(60)} for i in range(12)}),
            encoding="utf-8",
        )
        res = run("batch_describe", str(fat), "--json-path", * [f"$.b{i}" for i in range(12)])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertLessEqual(len(res.stdout), 83_000)
        payload = json.loads(res.stdout)
        self.assertEqual(payload["count"], 12)
        omitted = [r for r in payload["results"] if r.get("omitted")]
        kept = [r for r in payload["results"] if r.get("describe") is not None]
        self.assertGreaterEqual(len(kept), 3)
        self.assertGreaterEqual(len(omitted), 1)
        self.assertTrue(all(r["describe"] is None for r in omitted))

    # --- unified batch surface (v1.7.0: every command takes one path or a list) ---

    def test_read_multi_path_envelope(self):
        res = run("read", str(self.json_file), "-p", "$.code", "-p", "$.data.items[1].price")
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertEqual(payload["count"], 2)
        by = {r["path"]: r for r in payload["results"]}
        self.assertEqual(by["$.code"]["type"], "integer")
        self.assertEqual(by["$.data.items[1].price"]["type"], "null")

    def test_read_multi_paths_after_one_flag(self):
        res = run("read", str(self.json_file), "-p", "$.code", "$.message")
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertEqual(payload["count"], 2)
        self.assertTrue(all(r["ok"] for r in payload["results"]))

    def test_describe_multi_path_envelope(self):
        res = run("describe", str(self.json_file), "-p", "$.data.items[0]", "-p", "$.data.nope")
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        by = {r["path"]: r for r in payload["results"]}
        self.assertTrue(by["$.data.items[0]"]["ok"])
        self.assertIn("$.data.items[0].id", {e["path"] for e in by["$.data.items[0]"]["describe"]["entries"]})
        self.assertFalse(by["$.data.nope"]["ok"])

    def test_read_single_path_stays_compact(self):
        res = run("read", str(self.json_file), "-p", "$.data.items[1].extra")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout), {"a": 1})  # one path = plain slice, no envelope

    # --- select (CSS over HTML; optional parsel dependency) -------------------

    @unittest.skipUnless(HAVE_PARSEL, "parsel not installed")
    def test_select_count_and_default_matches(self):
        res = run("select", str(self.html_page), ".price")
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertEqual(payload["count"], 3)
        self.assertEqual([m["text"] for m in payload["matches"]], ["$1", "$2", "$3"])
        self.assertEqual(payload["matches"][0]["tag"], "li")
        self.assertIn("class", payload["matches"][0]["attrs"])

    @unittest.skipUnless(HAVE_PARSEL, "parsel not installed")
    def test_select_attr_values(self):
        res = run("select", str(self.html_page), "a", "--attr", "href")
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["values"], ["/p/1", "/p/2"])

    @unittest.skipUnless(HAVE_PARSEL, "parsel not installed")
    def test_select_html_and_text_modes(self):
        html_mode = json.loads(run("select", str(self.html_page), ".card", "--html").stdout)
        self.assertIn("<h2>Card A</h2>", html_mode["values"][0])
        text_mode = json.loads(run("select", str(self.html_page), ".card", "--text").stdout)
        self.assertEqual(text_mode["values"], ["Card A", "Card B"])

    @unittest.skipUnless(HAVE_PARSEL, "parsel not installed")
    def test_select_value_truncation(self):
        long_val = self.dir / "long.html"
        long_val.write_text('<p class="t">' + "x" * 2000 + "</p>", encoding="utf-8")
        res = run("select", str(long_val), ".t", "--text")
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertTrue(payload["values"][0].endswith("...[truncated]"))

    @unittest.skipUnless(HAVE_PARSEL, "parsel not installed")
    def test_select_json_path_html_string(self):
        dump = self.dir / "wrapped.json"
        dump.write_text(json.dumps({"html": "<b>bold</b><i>it</i>"}), encoding="utf-8")
        res = run("select", str(dump), "b", "--json-path", "$.html")
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["matches"][0]["tag"], "b")

    @unittest.skipUnless(HAVE_PARSEL, "parsel not installed")
    def test_select_on_json_file_needs_json_path(self):
        res = run("select", str(self.json_file), ".price")
        self.assertEqual(res.returncode, 1)
        self.assertIn("parsed as JSON", res.stdout)
        self.assertNotIn("Traceback", res.stdout)

    @unittest.skipIf(HAVE_PARSEL, "parsel installed — guidance path not exercised")
    def test_select_missing_dependency_guidance(self):
        res = run("select", str(self.html_page), ".price")
        self.assertEqual(res.returncode, 1)
        self.assertIn("pip install parsel", res.stdout)
        self.assertNotIn("Traceback", res.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
