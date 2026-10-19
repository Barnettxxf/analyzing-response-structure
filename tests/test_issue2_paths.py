"""CLI regressions for issue #2 path composition and HTML fragments."""

from __future__ import annotations

import json
import tempfile
import unittest
from html import escape
from pathlib import Path
from typing import Any

from test_inspect_sample import HAVE_PARSEL, run


class Issue2PathTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def json_source(self, name: str, value: Any) -> Path:
        source = self.directory / name
        source.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return source

    def assert_json_value(self, text: str, expected: Any) -> None:
        self.assertFalse(text.startswith("[json_path]"), text)
        self.assertEqual(json.loads(text), expected)

    def test_predicate_explicit_first_last_index_is_cardinality_independent(
        self,
    ) -> None:
        for count, index, expected in (
            (1, 0, "first"),
            (1, -1, "first"),
            (2, 0, "first"),
            (2, -1, "last"),
        ):
            with self.subTest(count=count, index=index):
                source = self.json_source(
                    "predicate.json",
                    {
                        "items": [
                            {"label": "s", "value": "first"},
                            {"label": "other", "value": "ignored"},
                        ]
                        + ([{"label": "s", "value": "last"}] if count == 2 else [])
                    },
                )
                result = run(
                    "read", str(source), "-p", f"$.items[?label=s][{index}].value"
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout.strip(), expected)

    def test_predicate_explicit_out_of_range_uses_match_list_length(self) -> None:
        for count in (0, 1, 3):
            source = self.json_source(
                "predicate.json", {"items": [{"label": "s"}] * count}
            )
            for index in (count, -count - 1):
                with self.subTest(count=count, index=index):
                    result = run(
                        "read", str(source), "-p", f"$.items[?label=s][{index}]"
                    )
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )
                    self.assertIn("out of range", result.stdout)
                    self.assertIn(f"list length={count}", result.stdout)
                    self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_predicate_without_index_preserves_unwrap_and_guidance(self) -> None:
        for count in (0, 1, 2):
            with self.subTest(count=count):
                source = self.json_source(
                    "predicate.json",
                    {"items": [{"label": "s", "value": 7}] * count},
                )
                result = run("read", str(source), "-p", "$.items[?label=s].value")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                if count == 1:
                    self.assertEqual(result.stdout.strip(), "7")
                elif count == 0:
                    self.assertIn("matched 0 elements", result.stdout)
                    self.assertIn("available label values", result.stdout)
                else:
                    self.assertIn("2 matches", result.stdout)
                    self.assertIn("append [0]", result.stdout)

    def test_predicate_index_is_stable_across_one_and_many_match_files(self) -> None:
        self.json_source("a.json", {"items": [{"label": "s", "value": 7}]})
        self.json_source(
            "b.json",
            {"items": [{"label": "s", "value": 8}, {"label": "s", "value": 9}]},
        )
        for index, expected in ((0, ["7", "8"]), (-1, ["7", "9"])):
            with self.subTest(index=index):
                result = run(
                    "read",
                    "--files",
                    str(self.directory / "*.json"),
                    "-p",
                    f"$.items[?label=s][{index}].value",
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                rows = json.loads(result.stdout)["results"]
                self.assertEqual(len(rows), 2)
                self.assertEqual([row["ok"] for row in rows], [True, True])
                self.assertEqual([row["value"] for row in rows], expected)

    def test_predicate_index_preserves_null_string_comparison_but_excludes_missing(
        self,
    ) -> None:
        source = self.json_source(
            "null.json",
            {"items": [{"value": None, "tag": "null"}, {"tag": "missing"}]},
        )
        result = run("read", str(source), "-p", "$.items[?value=None][0].tag")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "null")
        source = self.json_source(
            "null.json",
            {
                "items": [
                    {"value": None, "tag": "null"},
                    {"tag": "missing"},
                    {"value": "None", "tag": "literal"},
                ]
            },
        )
        result = run("read", str(source), "-p", "$.items[?value=None][-1].tag")
        self.assertEqual(
            result.stdout.strip(), "literal", result.stdout + result.stderr
        )
        result = run("read", str(source), "-p", "$.items[?value=None][2]")
        self.assertIn("list length=2", result.stdout)

    def test_described_literal_len_key_roundtrips_without_changing_operator(
        self,
    ) -> None:
        source = self.json_source("len.json", {"$len": 99, "normal": 1})
        described = run("describe", str(source))
        self.assertEqual(described.returncode, 0, described.stdout + described.stderr)
        paths = {entry["path"] for entry in json.loads(described.stdout)["entries"]}
        self.assertIn('$["$len"]', paths)
        for selector, expected in (('$["$len"]', 99), ("$.normal", 1), ("$.$len", 2)):
            with self.subTest(selector=selector):
                selected = run("read", str(source), "-p", selector)
                self.assertEqual(
                    selected.returncode, 0, selected.stdout + selected.stderr
                )
                self.assertEqual(json.loads(selected.stdout), expected)

    def test_wildcard_keeps_literal_len_key_distinct_from_operator(self) -> None:
        source = self.json_source("len.json", {"items": [{"$len": 99, "normal": 1}]})
        for selector, expected in (
            ('$.items[*]["$len"]', [99]),
            ("$.items[*].$len", [2]),
            ("$.items.$len", 1),
        ):
            with self.subTest(selector=selector):
                result = run("read", str(source), "-p", selector)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_json_value(result.stdout, expected)

    def test_predicate_wildcard_composes_directly_and_through_json_root(self) -> None:
        source = self.json_source(
            "composed.json",
            {
                "groups": [
                    {"label": "s", "items": [{"id": 7}, {}, {"id": 9}]},
                    {"label": "s", "items": [{"id": 99}]},
                ]
            },
        )
        for arguments in (
            ("-p", "$.groups[?label=s][0].items[*].id"),
            ("--json-root", "$.groups[?label=s][0]", "-p", "$.items[*].id"),
            ("--json-root", "$.groups[?label=s][0].items[*]", "-p", "$.id"),
        ):
            with self.subTest(arguments=arguments):
                result = run("read", str(source), *arguments)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_json_value(result.stdout, [7, None, 9])

    def test_unique_predicate_before_wildcard_still_unwraps(self) -> None:
        source = self.json_source(
            "unique.json", {"groups": [{"label": "s", "items": [{"id": 7}]}]}
        )
        result = run("read", str(source), "-p", "$.groups[?label=s].items[*].id")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_json_value(result.stdout, [7])

    def test_nested_wildcards_and_predicates_keep_null_suffix_positions(self) -> None:
        source = self.json_source(
            "nested.json",
            {
                "groups": [
                    {"items": [{"label": "s", "values": [{"v": 7}, {}]}]},
                    {"items": [{"label": "other", "values": [{"v": 8}]}]},
                    {"items": [{"label": "s", "values": []}]},
                    {
                        "items": [
                            {"label": "s", "values": [{"v": 9}]},
                            {"label": "s", "values": [{"v": 99}]},
                        ]
                    },
                ]
            },
        )
        result = run(
            "read", str(source), "-p", '$.groups[*].items[?label=s][0].values[*]["v"]'
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_json_value(result.stdout, [[7, None], None, [], [9]])

    def test_nested_wildcards_preserve_quoted_keys_and_negative_indexes(self) -> None:
        outer_key, inner_key = 'outer]"\\雪', 'inner["\\key'
        source = self.json_source(
            "quoted.json",
            {
                outer_key: [
                    {"groups": [{inner_key: [{"id": 7}, {"id": 8}]}, {}]},
                    {"groups": [{inner_key: [{"id": 9}]}]},
                ]
            },
        )
        prefix = "$[" + json.dumps(outer_key, ensure_ascii=False) + "]"
        suffix = ".groups[*][" + json.dumps(inner_key) + "][-1].id"
        for selector, expected in (
            (prefix + "[*]" + suffix, [[8, None], [9]]),
            (prefix + "[-1]" + suffix, [9]),
        ):
            with self.subTest(selector=selector):
                result = run("read", str(source), "-p", selector)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_json_value(result.stdout, expected)

    def test_escaped_script_ids_work_with_direct_and_wildcard_selection(self) -> None:
        for script_id in ('app"state', 'cache] "v2" \\ 雪'):
            with self.subTest(script_id=script_id):
                html = (
                    '<html><script id="'
                    + escape(script_id, quote=True)
                    + '">{"items":[{"id":7},{"id":8}]}</script></html>'
                )
                source = self.directory / "script.html"
                source.write_text(html, encoding="utf-8")
                root = "$#script[id=" + json.dumps(script_id, ensure_ascii=False) + "]"
                for suffix, expected in ((".items[0].id", 7), (".items[*].id", [7, 8])):
                    result = run("read", str(source), "-p", root + suffix)
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )
                    self.assert_json_value(result.stdout, expected)
                nested = self.json_source(
                    "scripts.json", {"pages": [html, "plain text"]}
                )
                result = run(
                    "read", str(nested), "-p", "$.pages[*]" + root[1:] + ".items[*].id"
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_json_value(result.stdout, [[7, 8], None])

    def test_ambiguous_predicate_before_wildcard_keeps_guidance(self) -> None:
        source = self.json_source(
            "ambiguous.json", {"items": [{"label": "s"}, {"label": "s"}]}
        )
        result = run("read", str(source), "-p", "$.items[?label=s].children[*].id")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("2 matches", result.stdout)
        self.assertIn("append [0]", result.stdout)
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_fragment_script_discovery_and_selection_roundtrip(self) -> None:
        script = (
            '<script id="state" type="application/json">{"items":[{"id":7}]}</script>'
        )
        for fragment in (
            script,
            "<div>" + script + "</div>",
            "\ufeff \n<!-- captured fragment -->\n" + script,
            "leading text <section>" + script + "</section>",
        ):
            with self.subTest(fragment=fragment):
                source = self.directory / "fragment.html"
                source.write_text(fragment, encoding="utf-8")
                result = run("describe", str(source), "--scripts")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                listing = json.loads(result.stdout)
                self.assertEqual(listing["count"], 1)
                row = listing["scripts"][0]
                self.assertEqual(row["script_id"], "state")
                self.assertFalse(row["parse_error"])
                for root in ("$#script[0]", row["id_path"], row["type_path"]):
                    selected = run("read", str(source), "-p", root + ".items[*].id")
                    self.assertEqual(
                        selected.returncode, 0, selected.stdout + selected.stderr
                    )
                    self.assert_json_value(selected.stdout, [7])

    def test_fragment_json_string_container_discovery_paths_roundtrip(self) -> None:
        fragment = (
            '<div><script id="state" type="application/json">{"id":7}</script></div>'
        )
        source = self.json_source("capture.json", {"response": fragment})
        result = run("describe", str(source), "--scripts", "-p", "$.response")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        listing = json.loads(result.stdout)
        self.assertEqual(listing["count"], 1)
        row = listing["scripts"][0]
        self.assertEqual(row["id_path"], '$.response#script[id="state"]')
        self.assertEqual(row["type_path"], '$.response#script[type="application/json"]')
        for root in (row["id_path"], row["type_path"]):
            selected = run("read", str(source), "-p", root + ".id")
            self.assertEqual(selected.returncode, 0, selected.stdout + selected.stderr)
            self.assert_json_value(selected.stdout, 7)
        described = run("describe", str(source), "-p", "$.response")
        self.assertEqual(described.returncode, 0, described.stdout + described.stderr)
        entries = json.loads(described.stdout)["entries"]
        self.assertIn("$.response#script[0].id", {entry["path"] for entry in entries})

    def test_fragment_script_discovery_rejects_multiple_container_paths(self) -> None:
        fragment = '<script id="state">{"id":7}</script>'
        source = self.json_source("capture.json", {"a": fragment, "b": fragment})
        result = run("describe", str(source), "--scripts", "-p", "$.a", "$.b")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("at most one", result.stdout)
        self.assertIn("-p", result.stdout)
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_fragment_script_discovery_bad_container_is_bounded_guidance(self) -> None:
        source = self.json_source("capture.json", {"response": "plain text"})
        result = run("describe", str(source), "--scripts", "-p", "$.missing")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("available keys", result.stdout)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertLessEqual(len(result.stdout), 24_100)

    def test_fragment_batch_script_summaries_keep_findings_and_non_html_rows(
        self,
    ) -> None:
        captures = {
            "a.html": '<script id="first">{"id":7}</script>',
            "b.html": '<div><script type="application/json">{"id":8}</script></div>',
            "c.html": "plain text",
            "d.html": '<script id="bad">window.state = {id:9};</script>',
        }
        for name, contents in captures.items():
            (self.directory / name).write_text(contents, encoding="utf-8")
        result = run("describe", "--scripts", "--files", str(self.directory / "*.html"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["file_count"], 4)
        rows = {row["file"]: row for row in payload["results"]}
        self.assertNotIn("error", rows["a.html"])
        self.assertNotIn("error", rows["b.html"])
        self.assertEqual(rows["a.html"]["scripts"][0]["id"], "first")
        self.assertTrue(rows["a.html"]["scripts"][0]["parse_ok"])
        self.assertTrue(rows["b.html"]["scripts"][0]["parse_ok"])
        self.assertIn("not HTML", rows["c.html"]["error"])
        self.assertFalse(rows["d.html"]["scripts"][0]["parse_ok"])

    def test_fragment_malformed_scripts_are_findings_never_executed(self) -> None:
        marker = self.directory / "executed.txt"
        code = (
            "require('fs').writeFileSync(" + json.dumps(str(marker)) + ", 'executed');"
        )
        source = self.directory / "unsafe.html"
        source.write_text(
            '<script id="bad">' + code + '</script><script id="good">{"id":7}</script>',
            encoding="utf-8",
        )
        result = run("describe", str(source), "--scripts")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = json.loads(result.stdout)["scripts"]
        self.assertEqual([row["parse_error"] for row in rows], [True, False])
        self.assertIn("writeFileSync", rows[0]["head"])
        bad = run("read", str(source), "-p", '$#script[id="bad"]')
        self.assertEqual(bad.returncode, 0, bad.stdout + bad.stderr)
        self.assertIn("parse_error", bad.stdout)
        good = run("read", str(source), "-p", '$#script[id="good"].id')
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
        self.assert_json_value(good.stdout, 7)
        self.assertFalse(marker.exists())

    def test_fragment_plain_text_and_non_html_remain_bounded_guidance(self) -> None:
        for text in (
            "plain text",
            "&lt;script id=state&gt;{}&lt;/script&gt;",
            '<!-- <script id="state">{}</script> -->',
            "<script",
            "< 2 and > 1",
            "<![not-html]>",
        ):
            with self.subTest(text=text):
                source = self.directory / "plain.txt"
                source.write_text(text, encoding="utf-8")
                described = run("describe", str(source), "--scripts")
                self.assertEqual(
                    described.returncode, 1, described.stdout + described.stderr
                )
                self.assertIn("HTML", described.stdout)
                selected = run("read", str(source), "-p", "$#script[0]")
                self.assertEqual(
                    selected.returncode, 0, selected.stdout + selected.stderr
                )
                self.assertIn("not HTML", selected.stdout)
                for result in (described, selected):
                    self.assertNotIn("Traceback", result.stdout + result.stderr)
                    self.assertLessEqual(len(result.stdout), 24_100)

    @unittest.skipUnless(HAVE_PARSEL, "parsel not installed")
    def test_fragment_script_selection_matches_xpath_in_raw_and_json_container(
        self,
    ) -> None:
        fragment = '<div><script id="state">{"items":[{"id":7}]}</script></div>'
        raw = self.directory / "fragment.html"
        raw.write_text(fragment, encoding="utf-8")
        nested = self.json_source("capture.json", {"response": fragment})
        for source, container in ((raw, "$"), (nested, "$.response")):
            with self.subTest(container=container):
                xpath_args = () if container == "$" else ("-p", container)
                xpath = run(
                    "read", str(source), *xpath_args, "-x", "//script/text()", "--text"
                )
                self.assertEqual(xpath.returncode, 0, xpath.stdout + xpath.stderr)
                selected = run(
                    "read", str(source), "-p", container + '#script[id="state"]'
                )
                self.assertEqual(
                    selected.returncode, 0, selected.stdout + selected.stderr
                )
                self.assert_json_value(selected.stdout, {"items": [{"id": 7}]})
                self.assertEqual(
                    json.loads(json.loads(xpath.stdout)["values"][0]),
                    json.loads(selected.stdout),
                )


if __name__ == "__main__":
    unittest.main()
