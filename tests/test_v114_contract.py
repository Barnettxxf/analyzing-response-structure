"""Behavioral regressions from cross-file and grouped inspection feedback."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from html import escape
from pathlib import Path
from unittest.mock import patch

from test_inspect_sample import HAVE_PARSEL, HTML_PAGE, HTML_SAMPLE, SCRIPT, run


class V114ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.html = self.dir / "page.html"
        self.html.write_text(HTML_PAGE, encoding="utf-8")

    def test_script_id_is_stable_across_files_and_paths_file(self) -> None:
        for filename, before, after in (
            ("a.html", "", "<script>bad trailing js</script>"),
            ("b.html", "<script>var x=1;</script><script>{}</script>", ""),
        ):
            (self.dir / filename).write_text(
                "<html><body>"
                + before
                + '<script id="payload">{"kind":"detail","n":7}</script>'
                + after
                + "</body></html>",
                encoding="utf-8",
            )
        paths = self.dir / "paths.txt"
        paths.write_text('$#script[id="payload"].n\n', encoding="utf-8")
        res = run(
            "read", "--files", str(self.dir / "[ab].html"), "--paths-file", str(paths)
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["file_count"], 2)
        self.assertTrue(all(row["ok"] for row in data["results"]))
        self.assertEqual([row["value"] for row in data["results"]], ["7", "7"])

    def test_script_id_handles_closing_bracket_and_escaped_quote(self) -> None:
        sid = 'cache] "v2" \\ 雪'
        self.html.write_text(
            '<html><script id="'
            + escape(sid, quote=True)
            + '">{"found":9}</script></html>',
            encoding="utf-8",
        )
        path = "$#script[id=" + json.dumps(sid, ensure_ascii=False) + "].found"
        res = run("read", str(self.html), "-p", path)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(res.stdout.strip(), "9")

    def test_script_id_single_quoted_value(self) -> None:
        self.html.write_text(
            '<html><script id="state">{"n":4}</script></html>', encoding="utf-8"
        )
        res = run("read", str(self.html), "-p", "$#script[id='state'].n")
        self.assertEqual(res.stdout.strip(), "4", res.stdout + res.stderr)

    def test_duplicate_script_id_uses_first_document_match(self) -> None:
        self.html.write_text(
            '<html><script id="state">{"n":1}</script>'
            '<script id="state">{"n":2}</script></html>',
            encoding="utf-8",
        )
        res = run("read", str(self.html), "-p", '$#script[id="state"].n')
        self.assertEqual(res.stdout.strip(), "1", res.stderr)

    def test_script_id_does_not_skip_malformed_first_match(self) -> None:
        self.html.write_text(
            '<html><script id="state">broken JSON</script>'
            '<script id="state">{"n":2}</script></html>',
            encoding="utf-8",
        )
        res = run("read", str(self.html), "-p", '$#script[id="state"].n')
        self.assertEqual(res.returncode, 0)
        self.assertIn("parse_error", res.stdout)
        self.assertIn("broken JSON", res.stdout)

    def test_single_quoted_keys_with_escapes_and_brackets(self) -> None:
        path = self.dir / "keys.json"
        path.write_text(
            json.dumps({"@type": "Thing", "a]b": {"it's": 17}}), encoding="utf-8"
        )
        for selector, want in (("$['@type']", "Thing"), (r"$['a]b']['it\'s']", "17")):
            with self.subTest(selector=selector):
                res = run("read", str(path), "-p", selector)
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
                self.assertEqual(res.stdout.strip(), want)

    def test_bad_id_selectors_are_guidance_not_tracebacks(self) -> None:
        for path in (
            "$#script[id=state]",
            '$#script[id="unterminated]',
            '$#script[id="x" junk]',
        ):
            with self.subTest(path=path):
                res = run("read", str(self.html), "-p", path)
                self.assertIn("[json_path]", res.stdout)
                self.assertNotIn("Traceback", res.stdout + res.stderr)

    def test_script_listing_emits_usable_stable_id_path(self) -> None:
        self.html.write_text(HTML_SAMPLE, encoding="utf-8")
        res = run("describe", str(self.html), "--scripts")
        listing = json.loads(res.stdout)
        row = listing["scripts"][0]
        self.assertEqual(row.get("id_path"), '$#script[id="__NEXT_DATA__"]')
        selected = run("read", str(self.html), "-p", row["id_path"] + ".props.x")
        self.assertEqual(selected.stdout.strip(), "1")

    @unittest.skipUnless(HAVE_PARSEL, "parsel not installed")
    def test_attribute_xpath_and_matching_attr_are_redundant_not_a_crash(self) -> None:
        for mode in ((str(self.html),), ("--files", str(self.html))):
            with self.subTest(mode=mode):
                res = run("read", *mode, "-x", "//a/@href", "--attr", "href")
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
                data = json.loads(res.stdout)
                row = data["results"][0] if "--files" in mode else data
                self.assertEqual(row["values"], ["/p/1", "/p/2"])

    @unittest.skipUnless(HAVE_PARSEL, "parsel not installed")
    def test_wrong_attribute_on_value_results_explains_retry(self) -> None:
        for xp in ("//a/@href", "//a/text()"):
            with self.subTest(xp=xp):
                res = run("read", str(self.html), "-x", xp, "--attr", "title")
                self.assertEqual(res.returncode, 1, res.stdout)
                self.assertIn("--attr", res.stdout)
                self.assertIn("--text", res.stdout)
                self.assertNotIn("Traceback", res.stdout + res.stderr)

    @unittest.skipUnless(HAVE_PARSEL, "parsel not installed")
    def test_grouped_xpath_uses_one_attr_mode_for_every_expression(self) -> None:
        res = run("read", str(self.html), "-x", "//a", "-x", "//a[1]", "--attr", "href")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        rows = json.loads(res.stdout)["results"]
        self.assertEqual(rows[0]["values"], ["/p/1", "/p/2"])
        self.assertEqual(rows[1]["values"], ["/p/1"])

    @unittest.skipUnless(HAVE_PARSEL, "parsel not installed")
    def test_grouped_xpath_html_mode_keeps_markup(self) -> None:
        res = run("read", str(self.html), "-x", "//a", "-x", "//h2", "--html")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        rows = json.loads(res.stdout)["results"]
        self.assertEqual(rows[0]["values"][0], '<a href="/p/1">one</a>')
        self.assertEqual(rows[1]["values"][1], "<h2>Card B</h2>")

    def test_conflicting_modes_offer_separate_calls(self) -> None:
        res = run("read", str(self.html), "-x", "//a", "--text", "--html")
        self.assertEqual(res.returncode, 1)
        self.assertIn("separate calls", res.stdout)
        self.assertNotIn("Traceback", res.stdout + res.stderr)

    @unittest.skipUnless(HAVE_PARSEL, "parsel not installed")
    def test_show_applies_to_single_grouped_and_file_xpath(self) -> None:
        for mode in (
            (str(self.html),),
            (str(self.html), "-x", "//h2"),
            ("--files", str(self.html)),
        ):
            with self.subTest(mode=mode):
                res = run("read", *mode, "-x", "//li", "--text", "--show", "2")
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
                data = json.loads(res.stdout)
                row = data["results"][-1] if "results" in data else data
                self.assertEqual(row["count"], 3)
                self.assertEqual(row["shown"], 2)
                self.assertEqual(row["omitted"], 1)
                self.assertEqual(row["values"], ["$1", "$2"])

    @unittest.skipUnless(HAVE_PARSEL, "parsel not installed")
    def test_show_clamps_and_discloses_omitted_matches(self) -> None:
        self.html.write_text(
            "<html>" + "".join(f"<p>{i}</p>" for i in range(230)) + "</html>",
            encoding="utf-8",
        )
        res = run("read", str(self.html), "-x", "//p", "--text", "--show", "999999")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["count"], 230)
        self.assertEqual(data["shown"], 200)
        self.assertEqual(data["omitted"], 30)
        self.assertEqual(data["values"][-1], "199")

    def test_show_rejects_invalid_values_and_non_xpath_mode(self) -> None:
        for n in ("0", "-1", "abc"):
            with self.subTest(n=n):
                res = run("read", str(self.html), "-x", "//li", "--show", n)
                self.assertEqual(res.returncode, 1)
                self.assertIn("show", res.stdout)
                self.assertNotIn("Traceback", res.stdout + res.stderr)
        res = run("read", str(self.html), "--show", "2")
        self.assertEqual(res.returncode, 1)
        self.assertIn("-x", res.stdout)

    @unittest.skipUnless(HAVE_PARSEL, "parsel not installed")
    def test_xpath_limit_clamps_both_ends(self) -> None:
        self.html.write_text(
            "<html><p>" + "x" * 30000 + "</p></html>", encoding="utf-8"
        )
        for mode in ((str(self.html),), ("--files", str(self.html))):
            for n, want in (("2", 16), ("999999", 20000)):
                with self.subTest(mode=mode, n=n):
                    res = run("read", *mode, "-x", "//p", "--text", "--limit", n)
                    self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
                    data = json.loads(res.stdout)
                    row = data["results"][0] if "--files" in mode else data
                    self.assertEqual(len(row["values"][0]), want)
                    self.assertTrue(row["values"][0].startswith("xx"))
                    self.assertTrue(row["values"][0].endswith("...[truncated]"))

    @unittest.skipUnless(HAVE_PARSEL, "parsel not installed")
    def test_grouped_xpath_total_output_budget_and_coverage(self) -> None:
        self.html.write_text(
            "<html>" + "<p>" + ("x" * 1000 + "</p><p>") * 40 + "</p></html>",
            encoding="utf-8",
        )
        expressions = [f"//p[position() > {i}]" for i in range(15)]
        res = run("read", str(self.html), "-x", *expressions, "--text")
        self.assertEqual(res.returncode, 0, res.stdout[:200] + res.stderr)
        self.assertLessEqual(len(res.stdout.rstrip("\r\n")), 80000)
        data = json.loads(res.stdout)
        self.assertEqual(data["count"], 15)
        self.assertEqual(data["shown"] + data["omitted"], 15)
        for row in data["results"]:
            self.assertEqual(row["shown"], len(row["values"]))
            self.assertEqual(row["shown"] + row["omitted"], row["count"])
        self.assertTrue(data["truncated"])

    @unittest.skipUnless(HAVE_PARSEL, "parsel not installed")
    def test_xpath_string_and_boolean_scalars_remain_typed(self) -> None:
        for expression, expected, typename in (
            ("string(//a[1])", "one", "string"),
            ("boolean(//missing)", False, "boolean"),
            ("count(//missing)", 0, "number"),
        ):
            with self.subTest(expression=expression):
                res = run("read", str(self.html), "-x", expression)
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
                data = json.loads(res.stdout)
                self.assertEqual(data.get("value"), expected)
                self.assertEqual(data.get("type"), typename)
        res = run("read", str(self.html), "-x", "//a/text()", "--text")
        self.assertEqual(json.loads(res.stdout)["values"], ["one", "two"])

    @unittest.skipUnless(HAVE_PARSEL, "parsel not installed")
    def test_xpath_union_is_a_nodeset_not_grouping(self) -> None:
        res = run("read", str(self.html), "-x", "//a | //h2", "--text")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["count"], 4)
        self.assertEqual(data["values"], ["one", "two", "Card A", "Card B"])

    def test_empty_xpath_requests_are_guidance(self) -> None:
        res = run("read", str(self.html), "-x")
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("expression", res.stdout)

    def test_keys_only_lists_late_keys_without_descending(self) -> None:
        source = self.dir / "wide.json"
        source.write_text(
            json.dumps(
                {f"section_{i}": {"hidden": "HIDDEN_BIG" * 1000} for i in range(23)}
            ),
            encoding="utf-8",
        )
        res = run("describe", str(source), "--keys")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["key_count"], 23)
        self.assertEqual(data["keys"], [f"section_{i}" for i in range(23)])
        self.assertEqual(data["shown"], 23)
        self.assertEqual(data["omitted_count"], 0)
        self.assertFalse(data["truncated"])
        self.assertNotIn("entries", data)
        self.assertNotIn("HIDDEN_BIG", res.stdout)

    def test_keys_only_resolves_script_and_multiple_subtrees(self) -> None:
        self.html.write_text(HTML_SAMPLE, encoding="utf-8")
        res = run(
            "describe",
            str(self.html),
            "--keys",
            "-p",
            '$#script[id="__NEXT_DATA__"]',
            "-p",
            "$#script[1]",
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["results"][0]["describe"]["keys"], ["props"])
        self.assertEqual(data["results"][1]["describe"]["keys"], ["@type", "name"])

    def test_keys_only_is_bounded_without_truncating_key_names(self) -> None:
        names = [str(i) + "k" * 800 for i in range(70)]
        source = self.dir / "many.json"
        source.write_text(json.dumps(dict.fromkeys(names)), encoding="utf-8")
        res = run("describe", str(source), "--keys")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertLessEqual(len(res.stdout.rstrip("\r\n")), 24000)
        data = json.loads(res.stdout)
        self.assertEqual(data["key_count"], 70)
        self.assertEqual(data["shown"], len(data["keys"]))
        self.assertEqual(data["shown"] + data["omitted_count"], 70)
        self.assertEqual(data["keys"], names[: data["shown"]])
        self.assertTrue(data["truncated"])

    def test_keys_only_rejects_nonobjects_and_conflicting_modes(self) -> None:
        source = self.dir / "array.json"
        source.write_text("[1,2]", encoding="utf-8")
        res = run("describe", str(source), "--keys")
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("object", res.stdout)
        self.assertNotIn("Traceback", res.stdout + res.stderr)
        for flag in (("--scripts",), ("-x", "//a")):
            with self.subTest(flag=flag):
                res = run("describe", str(self.html), "--keys", *flag)
                self.assertEqual(res.returncode, 1, res.stdout)
                self.assertIn("--keys", res.stdout)

    def test_html_description_includes_structural_counts_not_values(self) -> None:
        res = run("describe", str(self.html))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        structure = data.get("html_structure")
        self.assertIsNotNone(structure)
        self.assertEqual(structure["element_count"], 12)
        self.assertEqual(structure["tag_counts"]["li"], 3)
        self.assertEqual(structure["class_counts"]["div.card"], 2)
        self.assertFalse(structure["truncated"])

    def test_html_histogram_ignores_comments_and_script_contents(self) -> None:
        self.html.write_text(
            '<html><!-- <aside class="fake"></aside> -->'
            '<script>var x="<div class=hidden>";</script>'
            '<main class="real real"></main></html>',
            encoding="utf-8",
        )
        res = run("describe", str(self.html))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        structure = json.loads(res.stdout).get("html_structure")
        self.assertIsNotNone(structure)
        self.assertEqual(structure["element_count"], 3)
        self.assertNotIn("aside", structure["tag_counts"])
        self.assertNotIn("div", structure["tag_counts"])
        self.assertEqual(structure["class_counts"], {"main.real": 1})

    def test_html_histogram_is_bounded_with_explicit_coverage(self) -> None:
        self.html.write_text(
            "<html>"
            + "".join(f'<div class="c{i}"></div>' for i in range(70))
            + "</html>",
            encoding="utf-8",
        )
        res = run("describe", str(self.html))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertLessEqual(len(res.stdout.rstrip("\r\n")), 24000)
        structure = json.loads(res.stdout).get("html_structure")
        self.assertIsNotNone(structure)
        self.assertEqual(structure["element_count"], 71)
        self.assertEqual(structure["distinct_tag_classes"], 70)
        self.assertLessEqual(len(structure["class_counts"]), 30)
        self.assertEqual(
            len(structure["class_counts"]) + structure["class_groups_omitted"], 70
        )
        self.assertTrue(structure["truncated"])

    @unittest.skipUnless(HAVE_PARSEL, "parsel not installed")
    def test_nan_xpath_is_guidance_and_not_invalid_json(self) -> None:
        res = run("read", str(self.html), "-x", "number('bad')")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        self.assertIn("numeric conversion", data["error"])

    @unittest.skipUnless(HAVE_PARSEL, "parsel not installed")
    def test_describe_xpath_handles_attribute_nodes(self) -> None:
        res = run("describe", str(self.html), "-x", "//a/@href", "--limit", "11000")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        entry = next(e for e in data["entries"] if e["path"] == "$.matches[0].text")
        self.assertEqual(entry["sample"], "/p/1")

    def test_array_sampling_metadata_matches_actually_emitted_roots(self) -> None:
        source = self.dir / "sampled.json"
        source.write_text(
            json.dumps({"rows": [{"i": i} for i in range(200)]}), encoding="utf-8"
        )
        res = run("describe", str(source))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        paths = {entry["path"] for entry in data["entries"]}
        self.assertTrue(
            {"$.rows[0].i", "$.rows[100].i", "$.rows[199].i"}.issubset(paths)
        )
        entry = next(item for item in data["entries"] if item["path"] == "$.rows")
        self.assertEqual(entry["sample_count"], len(entry["sampled_indices"]))

    def test_array_type_frequencies_count_null_without_claiming_value_coverage(
        self,
    ) -> None:
        source = self.dir / "types.json"
        source.write_text(
            json.dumps({"rows": [{"v": 1}, {"v": None}, {"v": 2}, {}]}),
            encoding="utf-8",
        )
        res = run("describe", str(source))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        entry = next(
            item
            for item in json.loads(res.stdout)["entries"]
            if item["path"] == "$.rows"
        )
        field = entry["union_keys"]["v"]
        self.assertEqual(field.get("type_counts"), {"integer": 2, "null": 1})
        self.assertEqual(entry.get("scanned_count"), 4)

    def test_json_path_list_respects_actual_serialized_budget(self) -> None:
        source = self.dir / "fat-values.json"
        source.write_text(
            json.dumps({f"v{i}": "雪" * 25000 for i in range(8)}, ensure_ascii=False),
            encoding="utf-8",
        )
        res = run(
            "read",
            str(source),
            "-p",
            *[f"$.v{i}" for i in range(8)],
            "--limit",
            "20000",
        )
        self.assertEqual(res.returncode, 0, res.stdout[:100] + res.stderr)
        self.assertLessEqual(len(res.stdout.rstrip("\r\n")), 80000)
        data = json.loads(res.stdout)
        self.assertEqual(data["count"], 8)
        self.assertEqual(data["shown"] + data["omitted"], 8)
        self.assertTrue(data["truncated"])

    def test_script_listing_counts_utf8_bytes_and_has_a_budget(self) -> None:
        raw = json.dumps({"word": "雪"}, ensure_ascii=False)
        self.html.write_text(
            '<html><script id="s">' + raw + "</script></html>", encoding="utf-8"
        )
        res = run("describe", str(self.html), "--scripts")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        row = json.loads(res.stdout)["scripts"][0]
        self.assertEqual(row["json_bytes"], len(raw.encode("utf-8")))
        self.html.write_text(
            "<html>"
            + "".join(
                f'<script id="state_{i}">' + "bad data" * 30 + "</script>"
                for i in range(200)
            )
            + "</html>",
            encoding="utf-8",
        )
        res = run("describe", str(self.html), "--scripts")
        self.assertEqual(res.returncode, 0, res.stdout[:100] + res.stderr)
        self.assertLessEqual(len(res.stdout.rstrip("\r\n")), 24000)
        data = json.loads(res.stdout)
        self.assertEqual(data["count"], 200)
        self.assertEqual(data["shown"], len(data["scripts"]))
        self.assertEqual(data["shown"] + data["omitted"], 200)

    @unittest.skipUnless(HAVE_PARSEL, "parsel not installed")
    def test_budget_trimming_counts_only_emitted_clipped_values(self) -> None:
        self.html.write_text(
            "<html>" + ("<p>" + "x" * 6000 + "</p>") * 50 + "</html>", encoding="utf-8"
        )
        res = run(
            "read",
            str(self.html),
            "-x",
            "//p",
            "--text",
            "--show",
            "50",
            "--limit",
            "3000",
        )
        self.assertEqual(res.returncode, 0, res.stdout[:100] + res.stderr)
        data = json.loads(res.stdout)
        self.assertLess(data["shown"], 50)
        self.assertEqual(data["values_truncated"], data["shown"])
        self.assertEqual(data["shown"] + data["omitted"], 50)

    def test_batch_describe_budget_charges_paths_and_omission_rows(self) -> None:
        keys = [f"k{i}" + "z" * 995 for i in range(120)]
        source = self.dir / "long-keys.json"
        source.write_text(json.dumps(dict(zip(keys, range(120)))), encoding="utf-8")
        paths = self.dir / "long-paths.txt"
        paths.write_text(
            "\n".join("$[" + json.dumps(key) + "]" for key in keys), encoding="utf-8"
        )
        res = run("describe", str(source), "--paths-file", str(paths))
        self.assertEqual(res.returncode, 0, res.stdout[:100] + res.stderr)
        self.assertLessEqual(len(res.stdout.rstrip("\r\n")), 80000)
        data = json.loads(res.stdout)
        self.assertEqual(data["count"], 120)
        self.assertEqual(
            data["shown"], sum(not row.get("omitted", False) for row in data["results"])
        )
        self.assertEqual(data["shown"] + data["omitted"], 120)
        self.assertTrue(data["truncated"])

    def test_empty_xpath_never_falls_back_to_another_channel(self) -> None:
        for args in (
            ("describe", str(self.html), "-x", ""),
            ("read", "--files", str(self.html), "-x", "", "-p", "$"),
            ("read", str(self.html), "-x", "//a", "-x", ""),
        ):
            with self.subTest(args=args):
                res = run(*args)
                self.assertEqual(res.returncode, 1, res.stdout)
                self.assertIn("XPath expression", res.stdout)

    def test_file_io_failure_does_not_discard_other_rows(self) -> None:
        # Mock only the file-read boundary: Windows permissions are not portable
        # fixtures. All parsing, dispatch, globbing and other reads remain real.
        spec = importlib.util.spec_from_file_location("inspector_under_test", SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        getattr(spec.loader, "exec_module")(module)
        batch = self.dir / "io"
        batch.mkdir()
        for name, n in (("a.json", 1), ("b.json", 2), ("c.json", 3)):
            (batch / name).write_text(json.dumps({"n": n}), encoding="utf-8")
        original_read = module._read_file_text

        def fail_second(path):
            if Path(path).name == "b.json":
                raise PermissionError("denied")
            return original_read(path)

        output = io.StringIO()
        with (
            patch.object(module, "_read_file_text", side_effect=fail_second),
            contextlib.redirect_stdout(output),
        ):
            code = module.main(["read", "--files", str(batch / "*.json"), "-p", "$.n"])
        self.assertEqual(code, 0, output.getvalue())
        rows = json.loads(output.getvalue())["results"]
        self.assertEqual(rows[0]["value"], "1")
        self.assertFalse(rows[1]["ok"])
        self.assertIn("b.json", rows[1]["error"])
        self.assertEqual(rows[2]["value"], "3")

    @unittest.skipIf(HAVE_PARSEL, "dependency absence tested by no-parsel interpreter")
    def test_file_batch_missing_dependency_is_a_process_error(self) -> None:
        res = run("read", "--files", str(self.html), "-x", "//a")
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("pip install parsel", res.stdout)
        self.assertNotIn("Traceback", res.stdout + res.stderr)

    @unittest.skipUnless(HAVE_PARSEL, "parsel not installed")
    def test_html_mode_requires_elements_not_attribute_values(self) -> None:
        res = run("read", str(self.html), "-x", "//a/@href", "--html")
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("--html", res.stdout)
        self.assertIn("--text", res.stdout)
        self.assertNotIn("Traceback", res.stdout + res.stderr)

    # --- bounded keyword-context search (--grep) ---------------------------------

    def test_read_grep_reports_hits_with_verifiable_offsets(self) -> None:
        res = run("read", str(self.html), "--grep", "card", "--context", "10")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        # case-insensitive: div.card ×2 in markup + "Card A"/"Card B" text
        self.assertEqual(data["count"], 4)
        # Read exactly what the CLI sees (bytes → utf-8-sig), no newline translation.
        raw = self.html.read_bytes().decode("utf-8-sig", errors="replace")
        for i, hit in enumerate(data["hits"]):
            self.assertEqual(hit["index"], i)
            self.assertEqual(
                raw[hit["offset"] : hit["offset"] + len("card")].lower(), "card"
            )
            self.assertLessEqual(len(hit["context"]), 2 * 10 + len("card"))

    def test_read_grep_is_case_insensitive_and_counts_all(self) -> None:
        source = self.dir / "mixed-case.html"
        source.write_text("<p>VAT</p><p>vat</p><p>VaT</p>", encoding="utf-8")
        res = run("read", str(source), "--grep", "vat", "--context", "4")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["count"], 3)

    def test_read_grep_show_caps_matches_and_discloses_omission(self) -> None:
        self.html.write_text(
            "<html>" + "<p>needle</p>" * 50 + "</html>", encoding="utf-8"
        )
        res = run("read", str(self.html), "--grep", "needle", "--show", "3")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["count"], 50)
        self.assertEqual(data["shown"], 3)
        self.assertEqual(data["omitted"], 47)
        self.assertEqual(len(data["hits"]), 3)
        self.assertTrue(data["truncated"])

    def test_read_grep_works_on_minified_single_line_html(self) -> None:
        single = (
            "<html><body>"
            + "<th>单价<div>（含 13% 增值税）</div></th><td>9.9</td>" * 2
            + "</body></html>"
        )
        source = self.dir / "minified.html"
        source.write_text(single, encoding="utf-8")
        res = run("read", str(source), "--grep", "增值税", "--context", "8")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["count"], 2)
        self.assertTrue(all("增值税" in hit["context"] for hit in data["hits"]))
        self.assertNotEqual(data["hits"][0]["offset"], data["hits"][1]["offset"])

    def test_read_grep_files_batch_reports_per_file_counts(self) -> None:
        batch = self.dir / "grepbatch"
        batch.mkdir()
        (batch / "a.html").write_text(
            "<html><p>含 13% 增值税</p></html>", encoding="utf-8"
        )
        (batch / "b.html").write_text("<html><p>不含税</p></html>", encoding="utf-8")
        res = run("read", "--files", str(batch / "*.html"), "--grep", "增值税")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        rows = {r["file"]: r for r in data["results"]}
        self.assertEqual(rows["a.html"]["count"], 1)
        self.assertEqual(rows["b.html"]["count"], 0)
        self.assertTrue(rows["b.html"]["ok"])

    def test_read_grep_rejects_empty_keyword_and_conflicting_modes(self) -> None:
        for args in (
            ("read", str(self.html), "--grep", ""),
            ("read", str(self.html), "--grep", "   "),
            ("read", str(self.html), "--grep", "vat", "-p", "$.a"),
            ("read", str(self.html), "--grep", "vat", "-x", "//a"),
            ("read", str(self.html), "--grep", "vat", "--text"),
        ):
            with self.subTest(args=args):
                res = run(*args)
                self.assertEqual(res.returncode, 1, res.stdout)
                self.assertNotIn("Traceback", res.stdout + res.stderr)

    def test_read_grep_context_clamps_and_rejects_invalid(self) -> None:
        for value in ("-1", "abc"):
            with self.subTest(value=value):
                res = run("read", str(self.html), "--grep", "card", "--context", value)
                self.assertEqual(res.returncode, 1, res.stdout)
                self.assertIn("context", res.stdout)
        self.html.write_text(
            "<html><p>" + "y" * 900 + "needle" + "z" * 900 + "</p></html>",
            encoding="utf-8",
        )
        res = run("read", str(self.html), "--grep", "needle", "--context", "999999")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        for hit in json.loads(res.stdout)["hits"]:
            self.assertLessEqual(len(hit["context"]), 2 * 1000 + len("needle"))

    def test_read_grep_huge_output_respects_total_budget(self) -> None:
        self.html.write_text(
            "<html>" + ("<p>" + "x" * 3000 + "needle</p>") * 60 + "</html>",
            encoding="utf-8",
        )
        res = run("read", str(self.html), "--grep", "needle", "--context", "2000")
        self.assertEqual(res.returncode, 0, res.stdout[:100] + res.stderr)
        self.assertLessEqual(len(res.stdout.rstrip("\r\n")), 80000)
        data = json.loads(res.stdout)
        self.assertEqual(data["count"], 60)
        self.assertEqual(data["shown"] + data["omitted"], 60)
        self.assertTrue(data["truncated"])

    def test_read_grep_zero_hits_is_a_clean_empty_result(self) -> None:
        res = run("read", str(self.html), "--grep", "does-not-exist")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["hits"], [])

    # --- v1.15.0: --grep multi-keyword, batch multi-path matrix, --exclude ------

    def test_read_grep_multi_keyword_merges_hits_in_document_order(self) -> None:
        res = run(
            "read", str(self.html), "--grep", "card", "--grep", "one", "--context", "4"
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        # case-insensitive: card ×4 (2 attrs + Card A/B), one ×1
        self.assertEqual(data["count"], 5)
        self.assertEqual(data["keywords"], {"card": 4, "one": 1})
        offsets = [h["offset"] for h in data["hits"]]
        self.assertEqual(offsets, sorted(offsets))  # document order
        self.assertTrue(all(h["keyword"] in ("card", "one") for h in data["hits"]))

    def test_read_grep_keywords_overlap_keeps_both_matches(self) -> None:
        source = self.dir / "overlap.html"
        source.write_text("<p>含 13% 增值税</p>", encoding="utf-8")
        res = run("read", str(source), "--grep", "税", "--grep", "增值税")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        # 税 once (tail of 增值税); 增值税 once at its own start — merged, 2 hits
        self.assertEqual(data["keywords"], {"税": 1, "增值税": 1})
        self.assertEqual(data["count"], 2)

    def test_read_grep_single_keyword_keeps_flat_contract(self) -> None:
        res = run("read", str(self.html), "--grep", "card")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["count"], 4)
        self.assertTrue(all(h["keyword"] == "card" for h in data["hits"]))

    def test_read_files_multi_path_matrix_nested_by_file(self) -> None:
        batch = self.dir / "matrix"
        batch.mkdir()
        for name, code in (("a.json", 1), ("b.json", 2)):
            (batch / name).write_text(
                json.dumps({"code": code, "label": name}), encoding="utf-8"
            )
        res = run(
            "read", "--files", str(batch / "*.json"), "-p", "$.code", "-p", "$.label"
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["file_count"], 2)
        self.assertEqual(data["path_count"], 2)
        self.assertEqual(len(data["results"]), 2)
        row = data["results"][0]
        self.assertEqual(row["file"], "a.json")
        nested = {r["path"]: r for r in row["results"]}
        self.assertEqual(nested["$.code"]["value"], "1")
        self.assertEqual(nested["$.label"]["value"], "a.json")
        self.assertEqual(data["type_summaries"]["$.code"], {"integer": 2})
        self.assertEqual(data["type_summaries"]["$.label"], {"string": 2})

    def test_read_files_matrix_surfaces_cross_file_type_variance(self) -> None:
        batch = self.dir / "variance"
        batch.mkdir()
        (batch / "int.json").write_text(json.dumps({"v": 7}), encoding="utf-8")
        (batch / "str.json").write_text(json.dumps({"v": "7"}), encoding="utf-8")
        res = run("read", "--files", str(batch / "*.json"), "-p", "$.v")
        data = json.loads(res.stdout)
        self.assertEqual(
            data["type_summary"], {"integer": 1, "string": 1}
        )  # 单路径契约不变

    def test_read_files_batch_len_and_key_count(self) -> None:
        batch = self.dir / "lenbatch"
        batch.mkdir()
        (batch / "a.json").write_text(
            json.dumps({"arr": [1, 2, 3], "obj": {"x": 1}}), encoding="utf-8"
        )
        res_arr = run("read", "--files", str(batch / "a.json"), "-p", "$.arr")
        res_obj = run("read", "--files", str(batch / "a.json"), "-p", "$.obj")
        self.assertEqual(res_arr.returncode, 0, res_arr.stdout + res_arr.stderr)
        self.assertEqual(res_obj.returncode, 0, res_obj.stdout + res_obj.stderr)
        self.assertEqual(json.loads(res_arr.stdout)["results"][0]["len"], 3)
        self.assertEqual(json.loads(res_obj.stdout)["results"][0]["key_count"], 1)

    def test_read_files_matrix_len_key_count(self) -> None:
        batch = self.dir / "lenmatrix"
        batch.mkdir()
        (batch / "a.json").write_text(
            json.dumps({"arr": [1, 2], "obj": {"x": 1}}), encoding="utf-8"
        )
        res = run(
            "read", "--files", str(batch / "a.json"), "-p", "$.arr", "-p", "$.obj"
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        nested = {r["path"]: r for r in data["results"][0]["results"]}
        self.assertEqual(nested["$.arr"]["len"], 2)
        self.assertEqual(nested["$.obj"]["key_count"], 1)

    def test_read_files_exclude_glob_filters_collected_files(self) -> None:
        batch = self.dir / "excl"
        batch.mkdir()
        for name in ("keep1.json", "keep2.json", "skip.json"):
            (batch / name).write_text(json.dumps({"code": 1}), encoding="utf-8")
        res = run(
            "read",
            "--files",
            str(batch / "*.json"),
            "--exclude",
            "skip.json",
            "-p",
            "$.code",
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["file_count"], 2)
        self.assertFalse(any("skip" in r["file"] for r in data["results"]))

    def test_read_files_accepts_literal_file_paths_without_glob(self) -> None:
        batch = self.dir / "literal"
        batch.mkdir()
        (batch / "x.json").write_text(json.dumps({"code": 5}), encoding="utf-8")
        res = run("read", "--files", str(batch / "x.json"), "-p", "$.code")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(json.loads(res.stdout)["results"][0]["value"], "5")

    def test_read_files_matrix_budget_trims_nested_results_honestly(self) -> None:
        batch = self.dir / "fatm"
        batch.mkdir()
        (batch / "f.json").write_text(
            json.dumps({f"v{i}": "x" * 20000 for i in range(6)}), encoding="utf-8"
        )
        res = run(
            "read",
            "--files",
            str(batch / "f.json"),
            "-p",
            "$.v0",
            "-p",
            "$.v1",
            "-p",
            "$.v2",
            "-p",
            "$.v3",
            "--limit",
            "20000",
        )
        self.assertEqual(res.returncode, 0, res.stdout[:100] + res.stderr)
        self.assertLessEqual(len(res.stdout.rstrip("\r\n")), 80000)
        data = json.loads(res.stdout)
        self.assertEqual(data["path_count"], 4)
        row = data["results"][0]
        self.assertEqual(row["shown"] + row["omitted"], 4)
        self.assertTrue(data["truncated"])

    def test_read_files_zero_paths_still_rejected(self) -> None:
        batch = self.dir / "zero"
        batch.mkdir()
        (batch / "c.json").write_text(json.dumps({"code": 1}), encoding="utf-8")
        res = run("read", "--files", str(batch / "c.json"))
        self.assertEqual(res.returncode, 1)
        self.assertIn("path", res.stdout)

    def test_read_grep_still_rejects_multi_path_batch(self) -> None:
        batch = self.dir / "grepm"
        batch.mkdir()
        (batch / "c.json").write_text(json.dumps({"code": 1}), encoding="utf-8")
        res = run(
            "read", "--files", str(batch / "c.json"), "--grep", "x", "-p", "$.code"
        )
        self.assertEqual(res.returncode, 1)
        self.assertIn("--grep", res.stdout)

    def test_read_grep_empty_alternative_keyword_rejected(self) -> None:
        res = run("read", str(self.html), "--grep", "card", "--grep", "  ")
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("non-empty", res.stdout)

    # --- --value-only compact output ---------------------------------------------

    def test_read_value_only_multi_path_compact(self) -> None:
        source = self.dir / "vo.json"
        source.write_text(
            json.dumps({"code": 0, "message": "success"}), encoding="utf-8"
        )
        res = run(
            "read", str(source), "-p", "$.code", "-p", "$.message", "--value-only"
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data, {"$.code": 0, "$.message": "success"})

    def test_read_value_only_missing_path_is_null(self) -> None:
        source = self.dir / "vo.json"
        source.write_text(json.dumps({"code": 0}), encoding="utf-8")
        res = run("read", str(source), "-p", "$.code", "-p", "$.nope", "--value-only")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["$.code"], 0)
        self.assertIsNone(data["$.nope"])

    def test_read_value_only_files_batch(self) -> None:
        batch = self.dir / "vobatch"
        batch.mkdir()
        for name, code in (("a.json", 1), ("b.json", 2)):
            (batch / name).write_text(
                json.dumps({"code": code, "label": name}), encoding="utf-8"
            )
        res = run(
            "read",
            "--files",
            str(batch / "*.json"),
            "-p",
            "$.code",
            "-p",
            "$.label",
            "--value-only",
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["a.json"], {"$.code": 1, "$.label": "a.json"})
        self.assertEqual(data["b.json"], {"$.code": 2, "$.label": "b.json"})

    def test_read_value_more_data_in_same_budget(self) -> None:
        source = self.dir / "many.json"
        source.write_text(
            json.dumps({f"k{i}": i for i in range(200)}), encoding="utf-8"
        )
        paths = [f"$.k{i}" for i in range(200)]
        with_vo = run(
            "read", str(source), "-p", *paths, "--value-only", "--limit", "100"
        )
        without = run("read", str(source), "-p", *paths, "--limit", "100")
        self.assertEqual(with_vo.returncode, 0)
        self.assertEqual(without.returncode, 0)
        self.assertLess(len(with_vo.stdout), len(without.stdout))

    # --- v1.16: [*] wildcard projection ------------------------------------------

    def test_read_wildcard_array_projection(self) -> None:
        source = self.dir / "wc.json"
        source.write_text(
            json.dumps(
                {
                    "items": [
                        {"label": "alpha", "value": 1},
                        {"label": "beta", "value": 2},
                        {"label": "gamma", "value": 3},
                    ]
                }
            ),
            encoding="utf-8",
        )
        res = run("read", str(source), "-p", "$.items[*].label")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data, ["alpha", "beta", "gamma"])

    def test_read_wildcard_returns_full_elements(self) -> None:
        source = self.dir / "wc2.json"
        source.write_text(json.dumps({"tags": ["x", "y"]}), encoding="utf-8")
        res = run("read", str(source), "-p", "$.tags[*]")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(json.loads(res.stdout), ["x", "y"])

    def test_read_wildcard_empty_array(self) -> None:
        source = self.dir / "empty.json"
        source.write_text(json.dumps({"items": []}), encoding="utf-8")
        res = run("read", str(source), "-p", "$.items[*].label")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(json.loads(res.stdout), [])

    def test_read_wildcard_non_array_is_error(self) -> None:
        source = self.dir / "scalar.json"
        source.write_text(json.dumps({"code": 0}), encoding="utf-8")
        res = run("read", str(source), "-p", "$.code[*]")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("array", res.stdout)
        self.assertNotIn("Traceback", res.stdout + res.stderr)

    def test_read_wildcard_scalar_field_returns_values(self) -> None:
        source = self.dir / "vals.json"
        source.write_text(
            json.dumps({"prices": ["¥100", "¥200", "¥300"]}), encoding="utf-8"
        )
        res = run("read", str(source), "-p", "$.prices[*]")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(json.loads(res.stdout), ["¥100", "¥200", "¥300"])

    def test_read_wildcard_with_value_only(self) -> None:
        source = self.dir / "vowc.json"
        source.write_text(
            json.dumps(
                {
                    "items": [{"label": "a"}, {"label": "b"}, {"label": "c"}],
                }
            ),
            encoding="utf-8",
        )
        res = run("read", str(source), "-p", "$.items[*].label", "--value-only")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["$.items[*].label"], ["a", "b", "c"])

    def test_read_wildcard_suffix_miss_disclosed_as_null(self) -> None:
        source = self.dir / "partial.json"
        source.write_text(
            json.dumps(
                {
                    "items": [{"label": "a"}, {"other": 1}, {"label": "c"}],
                }
            ),
            encoding="utf-8",
        )
        res = run("read", str(source), "-p", "$.items[*].label")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data, ["a", None, "c"])

    def test_read_recursion_error_is_guidance(self) -> None:
        source = self.dir / "deep.json"
        source.write_text("[" * 6000 + "]" * 6000, encoding="utf-8")
        res = run("read", str(source), "-p", "$")
        self.assertEqual(res.returncode, 1, res.stdout[:100] + res.stderr[:100])
        self.assertNotIn("Traceback", res.stdout + res.stderr)
        self.assertIn("deeply nested", res.stdout + res.stderr)

    # --- v1.16.1: describe per-path budget (--limit) ------------------------------

    def test_describe_limit_bounds_subtree(self) -> None:
        source = self.dir / "wide.json"
        wide = {
            f"key{i:02d}": {"nested": {"deep": f"value-{i}-{'x' * 60}"}}
            for i in range(80)
        }
        source.write_text(json.dumps(wide), encoding="utf-8")
        full = run("describe", str(source), "-p", "$")
        self.assertEqual(full.returncode, 0, full.stdout + full.stderr)
        self.assertTrue(json.loads(full.stdout).get("truncated"))
        slim = run("describe", str(source), "-p", "$", "--limit", "400")
        self.assertEqual(slim.returncode, 0, slim.stdout + slim.stderr)
        slim_data = json.loads(slim.stdout)
        self.assertTrue(slim_data.get("truncated"))
        self.assertLess(len(slim.stdout), len(full.stdout))

    def test_describe_limit_multi_path_all_fit(self) -> None:
        source = self.dir / "regions.json"
        regions = {
            f"region{i}": {f"leaf{j}": f"{'y' * 80}-{i}-{j}" for j in range(12)}
            for i in range(6)
        }
        source.write_text(json.dumps(regions), encoding="utf-8")
        paths = [f"$.region{i}" for i in range(6)]
        no_budget = run("describe", str(source), "-p", *paths)
        self.assertEqual(no_budget.returncode, 0)
        with_budget = run("describe", str(source), "-p", *paths, "--limit", "500")
        self.assertEqual(
            with_budget.returncode, 0, with_budget.stdout + with_budget.stderr
        )
        self.assertLess(len(with_budget.stdout), len(no_budget.stdout))

    def test_describe_limit_validation(self) -> None:
        source = self.dir / "any.json"
        source.write_text("{}", encoding="utf-8")
        res = run("describe", str(source), "-p", "$", "--limit", "abc")
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("--limit", res.stdout)
        res_zero = run("describe", str(source), "-p", "$", "--limit", "0")
        self.assertEqual(res_zero.returncode, 0, res_zero.stdout + res_zero.stderr)
        self.assertNotIn("Traceback", res_zero.stdout + res_zero.stderr)

    # --- v1.16.2: --list-file literal batch + describe default 900 ---------------

    def test_read_list_file_literal_subset(self) -> None:
        batch = self.dir / "ff"
        batch.mkdir()
        for name in ("a.json", "b.json", "c.json"):
            (batch / name).write_text(json.dumps({"code": name[0]}), encoding="utf-8")
        listfile = self.dir / "subset.txt"
        listfile.write_text(
            f"# subset\n{batch / 'a.json'}\n\n{batch / 'c.json'}\n", encoding="utf-8"
        )
        res = run("read", "--list-file", str(listfile), "-p", "$.code")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        names = [r["file"] for r in data["results"]]
        self.assertEqual(len(names), 2)
        self.assertTrue(all(n in ("a.json", "c.json") for n in names))

    def test_read_list_file_missing_path_rejected(self) -> None:
        listfile = self.dir / "bad.txt"
        listfile.write_text(str(self.dir / "nope.json"), encoding="utf-8")
        res = run("read", "--list-file", str(listfile), "-p", "$.code")
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("not found", res.stdout)

    def test_describe_default_budget_is_900(self) -> None:
        source = self.dir / "mid.json"
        # ~30 entries of ~400 chars each: old 11000 default would emit most entries;
        # new 900 default truncates early with an honest flag.
        wide = {f"k{i:02d}": {"blob": "z" * 300} for i in range(30)}
        source.write_text(json.dumps(wide), encoding="utf-8")
        res = run("describe", str(source), "-p", "$", "--limit", "900")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertTrue(json.loads(res.stdout).get("truncated"))

    # --- v1.17: batch multi -x grouped per-file rows --------------------------------

    @unittest.skipUnless(HAVE_PARSEL, "parsel not installed")
    def test_read_files_multi_x_returns_grouped_rows_per_file(self) -> None:
        batch = self.dir / "groupx"
        batch.mkdir()
        for name in ("a.html", "b.html"):
            (batch / name).write_text(HTML_PAGE, encoding="utf-8")
        for extra in ((), ("--text",)):
            with self.subTest(extra=extra):
                res = run(
                    "read",
                    "--files",
                    str(batch / "*.html"),
                    "-x",
                    "//a",
                    "-x",
                    "//h2",
                    *extra,
                )
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
                data = json.loads(res.stdout)
                self.assertEqual(data["file_count"], 2)
                self.assertEqual(data["shown"], 2)
                self.assertEqual(data["omitted"], 0)
                self.assertEqual(
                    [r["file"] for r in data["results"]], ["a.html", "b.html"]
                )
                for row in data["results"]:
                    self.assertTrue(row["ok"])
                    self.assertEqual(set(row), {"file", "path", "ok", "groups"})
                    groups = {g["xpath"]: g for g in row["groups"]}
                    self.assertEqual(groups["//a"]["count"], 2)
                    self.assertEqual(groups["//a"]["values"], ["one", "two"])
                    self.assertEqual(groups["//h2"]["count"], 2)
                    self.assertEqual(groups["//h2"]["values"], ["Card A", "Card B"])

    @unittest.skipUnless(HAVE_PARSEL, "parsel not installed")
    def test_read_files_multi_x_show_limit_are_per_group(self) -> None:
        res = run(
            "read", "--files", str(self.html), "-x", "//li", "-x", "//h2", "--show", "2"
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        row = json.loads(res.stdout)["results"][0]
        li, h2 = row["groups"]
        self.assertEqual((li["count"], li["shown"], li["omitted"]), (3, 2, 1))
        self.assertEqual(li["values"], ["$1", "$2"])
        self.assertTrue(li["truncated"])  # match_limit disclosure per group
        self.assertEqual((h2["count"], h2["shown"], h2["omitted"]), (2, 2, 0))
        self.assertEqual(h2["values"], ["Card A", "Card B"])

    @unittest.skipUnless(HAVE_PARSEL, "parsel not installed")
    def test_read_files_multi_x_group_errors_stay_per_group(self) -> None:
        res = run("read", "--files", str(self.html), "-x", "//a", "-x", "//a[@href=")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        row = json.loads(res.stdout)["results"][0]
        self.assertTrue(row["ok"])
        self.assertTrue(row["groups"][0]["ok"])
        self.assertFalse(row["groups"][1]["ok"])
        self.assertIn("XPath error", row["groups"][1]["error"])

    @unittest.skipUnless(HAVE_PARSEL, "parsel not installed")
    def test_read_files_multi_x_non_html_file_is_row_level_error(self) -> None:
        batch = self.dir / "mixx"
        batch.mkdir()
        (batch / "good.html").write_text(HTML_PAGE, encoding="utf-8")
        (batch / "data.json").write_text(json.dumps({"not": "html"}), encoding="utf-8")
        res = run("read", "--files", str(batch / "*"), "-x", "//a", "-x", "//h2")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        rows = {r["file"]: r for r in json.loads(res.stdout)["results"]}
        self.assertTrue(rows["good.html"]["ok"])
        self.assertFalse(rows["data.json"]["ok"])
        self.assertIn("error", rows["data.json"])
        self.assertEqual(rows["data.json"]["groups"], [])

    @unittest.skipUnless(HAVE_PARSEL, "parsel not installed")
    def test_read_files_single_x_batch_row_stays_flat(self) -> None:
        res = run("read", "--files", str(self.html), "-x", "//a", "--text")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        row = json.loads(res.stdout)["results"][0]
        self.assertNotIn("groups", row)
        self.assertEqual(row["count"], 2)
        self.assertEqual(row["values"], ["one", "two"])

    def test_read_files_multi_x_still_rejects_p_container(self) -> None:
        res = run(
            "read", "--files", str(self.html), "-x", "//a", "-x", "//h2", "-p", "$"
        )
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("no -p container", res.stdout)
        self.assertNotIn("Traceback", res.stdout + res.stderr)

    @unittest.skipUnless(HAVE_PARSEL, "parsel not installed")
    def test_read_files_multi_x_metadata_overflow_is_guidance_exit1(self) -> None:
        # In-process argv (no OS command-line length limit): patterns metadata
        # alone must exceed the 80K batch budget after every row is trimmed.
        spec = importlib.util.spec_from_file_location("inspector_groupx", SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        getattr(spec.loader, "exec_module")(module)
        argv = ["read"]
        junk_root = self.dir.anchor  # same drive as the real file: relpath works
        for i in range(220):
            argv += [
                "--files",
                str(Path(junk_root) / ("x" * 400 + str(i)) / "none.html"),
            ]
        argv += ["--files", str(self.html), "-x", "//a", "-x", "//h2"]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = module.main(argv)
        self.assertEqual(code, 1, output.getvalue())
        data = json.loads(output.getvalue())
        self.assertIn("output budget", data["error"])
        self.assertEqual(data["truncation_reason"], "output_limit")

    # --- v1.17: [?key=value] predicate + $len in the path grammar -------------------

    def _write_attributes(self) -> Path:
        source = self.dir / "attributes.json"
        source.write_text(
            json.dumps(
                {
                    "attributes": [
                        {"label": "系列", "value": "A-series"},
                        {"label": "颜色", "value": "black"},
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return source

    def test_read_pred_unique_match_unwraps_element(self) -> None:
        source = self._write_attributes()
        for selector in (
            "$.attributes[?label=系列].value",
            '$.attributes[?label="系列"].value',
        ):
            with self.subTest(selector=selector):
                res = run("read", str(source), "-p", selector)
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
                self.assertEqual(res.stdout.strip(), "A-series")
        described = run("describe", str(source), "-p", "$.attributes[?label=系列]")
        self.assertEqual(described.returncode, 0, described.stdout + described.stderr)
        self.assertIn(
            "$.attributes[?label=系列].value",
            {e["path"] for e in json.loads(described.stdout)["entries"]},
        )

    def test_read_pred_zero_match_lists_available_values(self) -> None:
        source = self._write_attributes()
        res = run("read", str(source), "-p", "$.attributes[?label=尺码].value")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("available label values", res.stdout)
        self.assertIn("系列", res.stdout)
        self.assertIn("颜色", res.stdout)
        self.assertNotIn("Traceback", res.stdout + res.stderr)

    def test_read_pred_multiple_matches_suggest_narrowing(self) -> None:
        dup = self.dir / "dup.json"
        dup.write_text(
            json.dumps(
                {
                    "attributes": [
                        {"label": "系列", "value": "one"},
                        {"label": "系列", "value": "two"},
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        res = run("read", str(dup), "-p", "$.attributes[?label=系列].value")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("2 matches for [?label=系列]", res.stdout)
        self.assertIn("append [0]", res.stdout)
        self.assertNotIn("Traceback", res.stdout + res.stderr)

    def test_read_pred_on_non_list_is_guidance(self) -> None:
        source = self.dir / "obj.json"
        source.write_text(json.dumps({"attributes": {"label": "x"}}), encoding="utf-8")
        res = run("read", str(source), "-p", "$.attributes[?label=x]")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("not a list", res.stdout)
        self.assertNotIn("Traceback", res.stdout + res.stderr)

    def test_read_pred_malformed_is_guidance(self) -> None:
        source = self._write_attributes()
        for selector in (
            "$.attributes[?label]",
            "$.attributes[?=v]",
            "$.attributes[?label='unterminated]",
        ):
            with self.subTest(selector=selector):
                res = run("read", str(source), "-p", selector)
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
                self.assertIn("[json_path]", res.stdout)
                self.assertNotIn("Traceback", res.stdout + res.stderr)

    def test_read_len_operator_on_list_dict_and_root(self) -> None:
        source = self.dir / "lens.json"
        source.write_text(
            json.dumps({"items": [1, 2, 3], "meta": {"a": 1, "b": 2}}), encoding="utf-8"
        )
        for selector, want in (
            ("$.items.$len", "3"),
            ("$.meta.$len", "2"),
            ("$.$len", "2"),
        ):
            with self.subTest(selector=selector):
                res = run("read", str(source), "-p", selector)
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
                self.assertEqual(res.stdout.strip(), want)

    def test_read_len_on_scalar_is_guidance(self) -> None:
        source = self.dir / "scalar.json"
        source.write_text(json.dumps({"code": 0}), encoding="utf-8")
        res = run("read", str(source), "-p", "$.code.$len")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("$len", res.stdout)
        self.assertIn("integer", res.stdout)
        self.assertNotIn("Traceback", res.stdout + res.stderr)

    # --- v1.17: batch describe --scripts summary -------------------------------------

    def _write_scripts_batch(self) -> Path:
        batch = self.dir / "scriptsbatch"
        batch.mkdir()
        (batch / "a.html").write_text(
            '<html><script id="app" type="application/json">{"n":1}</script>'
            "<script>var x=1;</script></html>",
            encoding="utf-8",
        )
        (batch / "b.html").write_text(
            '<html><script type="application/ld+json">{"@type":"Product"}</script>'
            "</html>",
            encoding="utf-8",
        )
        return batch

    def test_describe_scripts_files_batch_summary(self) -> None:
        batch = self._write_scripts_batch()
        res = run("describe", "--scripts", "--files", str(batch / "*.html"))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(set(data), {"file_count", "shown", "omitted", "results"})
        self.assertEqual(data["file_count"], 2)
        self.assertEqual(data["shown"], 2)
        self.assertEqual(data["omitted"], 0)
        rows = {r["file"]: r for r in data["results"]}
        self.assertEqual(set(rows["a.html"]), {"file", "path", "scripts"})
        a = rows["a.html"]["scripts"]
        self.assertEqual([s["index"] for s in a], [0, 1])
        self.assertEqual(a[0]["id"], "app")
        self.assertEqual(a[0]["type"], "application/json")
        self.assertTrue(a[0]["parse_ok"])
        self.assertIsInstance(a[0]["length"], int)
        self.assertFalse(a[1]["parse_ok"])
        self.assertNotIn("head", a[1])  # compact summary only — no raw heads
        b = rows["b.html"]["scripts"]
        self.assertEqual(b[0]["id"], None)
        self.assertEqual(b[0]["type"], "application/ld+json")
        self.assertTrue(b[0]["parse_ok"])

    def test_describe_scripts_list_file_batch(self) -> None:
        batch = self._write_scripts_batch()
        listfile = self.dir / "subset.txt"
        listfile.write_text(f"# pick one\n{batch / 'a.html'}\n", encoding="utf-8")
        res = run("describe", "--scripts", "--list-file", str(listfile))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["file_count"], 1)
        self.assertEqual(data["results"][0]["file"], "a.html")
        self.assertEqual(data["results"][0]["scripts"][0]["id"], "app")

    def test_describe_scripts_files_exclude(self) -> None:
        batch = self._write_scripts_batch()
        (batch / "skip.html").write_text(
            '<html><script id="skip">{"n":9}</script></html>', encoding="utf-8"
        )
        res = run(
            "describe",
            "--scripts",
            "--files",
            str(batch / "*.html"),
            "--exclude",
            "skip.html",
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["file_count"], 2)
        self.assertFalse(any("skip" in r["file"] for r in data["results"]))

    def test_describe_scripts_batch_marks_non_html_files(self) -> None:
        batch = self._write_scripts_batch()
        (batch / "data.json").write_text(json.dumps({"not": "html"}), encoding="utf-8")
        res = run("describe", "--scripts", "--files", str(batch / "*"))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        rows = {r["file"]: r for r in json.loads(res.stdout)["results"]}
        self.assertEqual(rows["data.json"]["scripts"], [])
        self.assertIn("not HTML", rows["data.json"].get("error", ""))
        self.assertEqual(rows["a.html"]["scripts"][0]["id"], "app")

    def test_describe_files_batch_requires_scripts_flag(self) -> None:
        batch = self._write_scripts_batch()
        res = run("describe", "--files", str(batch / "*.html"))
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("--scripts", res.stdout)
        self.assertNotIn("Traceback", res.stdout + res.stderr)
        res = run("batch_describe", "--scripts", "--files", str(batch / "*.html"))
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertNotIn("Traceback", res.stdout + res.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
