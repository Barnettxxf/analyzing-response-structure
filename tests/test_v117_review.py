"""Review-round regressions (v1.17.0): pred[0] selection, script type selector,
--json-root re-rooting, pred missing-key semantics, multi-x trim disclosure,
cross-drive batches and argument discipline."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from test_inspect_sample import HAVE_PARSEL, HTML_PAGE, run


class V117ReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    # --- F1: [?k=v] multi-match + explicit [N] selection ---------------------------

    def _write_multi_match(self) -> Path:
        source = self.dir / "multi.json"
        source.write_text(
            json.dumps(
                {
                    "attributes": [
                        {"label": "s", "value": "first"},
                        {"label": "t", "value": "other"},
                        {"label": "s", "value": "second"},
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return source

    def test_read_pred_index_selects_first_match(self) -> None:
        source = self._write_multi_match()
        res = run("read", str(source), "-p", "$.attributes[?label=s][0].value")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(res.stdout.strip(), "first")

    def test_read_pred_negative_index_selects_last_match(self) -> None:
        source = self._write_multi_match()
        res = run("read", str(source), "-p", "$.attributes[?label=s][-1].value")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(res.stdout.strip(), "second")

    def test_read_pred_index_out_of_range_is_guidance(self) -> None:
        source = self._write_multi_match()
        res = run("read", str(source), "-p", "$.attributes[?label=s][9].value")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("out of range", res.stdout)
        self.assertIn("length=2", res.stdout)
        self.assertNotIn("Traceback", res.stdout + res.stderr)

    def test_describe_pred_index_selection_maps_element(self) -> None:
        source = self._write_multi_match()
        res = run("describe", str(source), "-p", "$.attributes[?label=s][0]")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        payload = json.loads(res.stdout)
        self.assertIn(
            "$.attributes[?label=s][0].value",
            {e["path"] for e in payload["entries"]},
        )

    # --- F2: $#script[type="..."] selector -----------------------------------------

    TYPE_PAGE = (
        "<html><body>"
        '<script type="application/json">{"items": [{"label": "a"}, {"label": "b"}]}'
        "</script>"
        '<script id="ld" type="application/ld+json">{"@type": "Product"}</script>'
        '<script type="application/json">{"other": true}</script>'
        "<script>window.__X__ = 1;</script>"
        "</body></html>"
    )

    def _write_type_page(self, text: str = TYPE_PAGE) -> Path:
        page = self.dir / "typed.html"
        page.write_text(text, encoding="utf-8")
        return page

    def test_read_script_by_type_quoted_resolves(self) -> None:
        page = self._write_type_page(
            '<html><script type="application/ld+json">{"name": "kbd"}</script></html>'
        )
        res = run("read", str(page), "-p", '$#script[type="application/ld+json"].name')
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(res.stdout.strip(), "kbd")

    def test_read_script_by_type_bareword_rejected_with_exact_form(self) -> None:
        page = self._write_type_page()
        res = run("read", str(page), "-p", "$#script[type=application/json]")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("[json_path]", res.stdout)
        self.assertIn('#script[type="application/json"]', res.stdout)
        self.assertNotIn("Traceback", res.stdout + res.stderr)

    def test_read_script_by_type_zero_match_lists_available_types(self) -> None:
        page = self._write_type_page()
        res = run("read", str(page), "-p", '$#script[type="module"].name')
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn('no script[type="module"]', res.stdout)
        self.assertIn("application/json", res.stdout)
        self.assertIn("application/ld+json", res.stdout)
        self.assertNotIn("Traceback", res.stdout + res.stderr)

    def test_read_script_by_type_multi_match_lists_indexes(self) -> None:
        page = self._write_type_page()
        res = run("read", str(page), "-p", '$#script[type="application/json"].items')
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("2", res.stdout)
        self.assertIn("#script[0]", res.stdout)
        self.assertIn("#script[2]", res.stdout)
        self.assertNotIn("Traceback", res.stdout + res.stderr)

    def test_read_script_by_type_unique_unwraps_and_continues(self) -> None:
        page = self._write_type_page()
        res = run("read", str(page), "-p", '$#script[type="application/ld+json"].name')
        # no "name" key — proves the ld+json object itself was unwrapped
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("available keys", res.stdout)
        self.assertIn("@type", res.stdout)

    def test_read_script_by_type_parse_error_reports_head(self) -> None:
        page = self._write_type_page(
            '<html><script type="application/json">'
            "window.__CFG__ = {assigned: true};</script></html>"
        )
        res = run("read", str(page), "-p", '$#script[type="application/json"].n')
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("parse_error", res.stdout)
        self.assertIn("window.__CFG__", res.stdout)

    def test_read_script_by_type_wildcard_multi_match_still_error(self) -> None:
        page = self._write_type_page()
        res = run(
            "read", str(page), "-p", '$#script[type="application/json"].items[*].label'
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        # type selector runs first: two candidates → index list, no silent first-win
        self.assertIn("#script[0]", res.stdout)
        self.assertIn("#script[2]", res.stdout)

    def test_read_script_by_type_wildcard_prefix_composition(self) -> None:
        page = self._write_type_page(
            "<html><body>"
            '<script type="application/json">'
            '[{"label": "a"}, {"label": "b"}]</script>'
            "</body></html>"
        )
        res = run("read", str(page), "-p", '$#script[type="application/json"][*].label')
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(json.loads(res.stdout), ["a", "b"])

    def test_read_script_by_type_batch_with_drifting_indexes(self) -> None:
        batch = self.dir / "typebatch"
        batch.mkdir()
        (batch / "a.html").write_text(
            '<html><script type="application/json">{"n": 1}</script></html>',
            encoding="utf-8",
        )
        (batch / "b.html").write_text(
            '<html><script id="first">var x=1;</script>'
            '<script type="application/json">{"n": 2}</script></html>',
            encoding="utf-8",
        )
        res = run(
            "read",
            "--files",
            str(batch / "*.html"),
            "-p",
            '$#script[type="application/json"].n',
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        payload = json.loads(res.stdout)
        self.assertEqual([r["value"] for r in payload["results"]], ["1", "2"])

    def test_describe_scripts_rows_expose_type_path(self) -> None:
        page = self._write_type_page()
        res = run("describe", str(page), "--scripts")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        rows = json.loads(res.stdout)["scripts"]
        by_index = {r["index"]: r for r in rows}
        self.assertEqual(
            by_index[0].get("type_path"), '$#script[type="application/json"]'
        )
        self.assertEqual(
            by_index[1].get("type_path"), '$#script[type="application/ld+json"]'
        )
        self.assertNotIn("type_path", by_index[3])  # untyped script has none
        selected = run("read", str(page), "-p", by_index[1]["type_path"] + '["@type"]')
        self.assertEqual(selected.stdout.strip(), "Product")

    # --- F3: --json-root re-rooting -------------------------------------------------

    def _write_drifting_batch(self) -> Path:
        """Two HTML files whose target script sits at a different index."""
        batch = self.dir / "driftbatch"
        batch.mkdir()
        (batch / "a.html").write_text(
            '<html><script type="application/json">'
            '{"code": 1, "label": "A"}</script></html>',
            encoding="utf-8",
        )
        (batch / "b.html").write_text(
            "<html><script>var noise = 1;</script>"
            '<script type="application/json">'
            '{"code": 2, "label": "B"}</script></html>',
            encoding="utf-8",
        )
        return batch

    def test_read_json_root_flat_batch_with_drifting_indexes(self) -> None:
        batch = self._write_drifting_batch()
        res = run(
            "read",
            "--files",
            str(batch / "*.html"),
            "--json-root",
            '$#script[type="application/json"]',
            "-p",
            "$.code",
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        payload = json.loads(res.stdout)
        self.assertEqual(payload["file_count"], 2)
        self.assertEqual([r["value"] for r in payload["results"]], ["1", "2"])

    def test_read_json_root_matrix_reports_short_paths(self) -> None:
        batch = self._write_drifting_batch()
        res = run(
            "read",
            "--files",
            str(batch / "*.html"),
            "--json-root",
            '$#script[type="application/json"]',
            "-p",
            "$.code",
            "-p",
            "$.label",
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        payload = json.loads(res.stdout)
        self.assertEqual(payload["path_count"], 2)
        for row in payload["results"]:
            nested = {r["path"]: r["value"] for r in row["results"]}
            self.assertEqual(set(nested), {"$.code", "$.label"})
        # cross-file type summaries stay keyed by the ORIGINAL short paths
        self.assertEqual(set(payload["type_summaries"]), {"$.code", "$.label"})
        self.assertEqual(payload["type_summaries"]["$.code"], {"integer": 2})

    def test_read_json_root_value_only_batch_short_keys(self) -> None:
        batch = self._write_drifting_batch()
        res = run(
            "read",
            "--files",
            str(batch / "*.html"),
            "--json-root",
            '$#script[type="application/json"]',
            "-p",
            "$.code",
            "-p",
            "$.label",
            "--value-only",
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["a.html"], {"$.code": 1, "$.label": "A"})
        self.assertEqual(data["b.html"], {"$.code": 2, "$.label": "B"})

    def test_read_json_root_value_only_all_missed_html_hint(self) -> None:
        batch = self._write_drifting_batch()
        res = run(
            "read",
            "--files",
            str(batch / "*.html"),
            "-p",
            "$.code",
            "--value-only",
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        self.assertIsNone(data["a.html"])
        hint = data.get("__hint__", "")
        self.assertIn("--json-root", hint)
        self.assertIn("--scripts", hint)
        self.assertIn("#script", hint)

    def test_read_json_root_bad_root_exit1(self) -> None:
        batch = self._write_drifting_batch()
        res = run(
            "read",
            "--files",
            str(batch / "a.html"),
            "--json-root",
            "$.a[",
            "-p",
            "$.code",
        )
        self.assertEqual(res.returncode, 1)
        self.assertIn("--json-root", res.stdout)
        self.assertIn("not a valid path", res.stdout)
        self.assertNotIn("Traceback", res.stdout + res.stderr)

    def test_read_json_root_non_dollar_path_exit1(self) -> None:
        batch = self._write_drifting_batch()
        res = run(
            "read",
            "--files",
            str(batch / "a.html"),
            "--json-root",
            "$.data",
            "-p",
            "code",
        )
        self.assertEqual(res.returncode, 1)
        self.assertIn("$", res.stdout)
        self.assertIn("code", res.stdout)
        self.assertNotIn("Traceback", res.stdout + res.stderr)

    def test_read_json_root_rejects_xpath_and_grep(self) -> None:
        batch = self._write_drifting_batch()
        for extra in (("-x", "//a"), ("--grep", "kw")):
            with self.subTest(extra=extra):
                res = run(
                    "read",
                    "--files",
                    str(batch / "a.html"),
                    "--json-root",
                    "$",
                    *extra,
                )
                self.assertEqual(res.returncode, 1, res.stdout)
                self.assertIn("--json-root", res.stdout)
                self.assertIn("cannot be combined", res.stdout)
                self.assertNotIn("Traceback", res.stdout + res.stderr)

    def test_read_json_root_single_file_modes(self) -> None:
        source = self.dir / "rooted.json"
        source.write_text(
            json.dumps({"data": {"items": [{"id": 7}, {"id": 8}]}}), encoding="utf-8"
        )
        single = run(
            "read", str(source), "--json-root", "$.data", "-p", "$.items[0].id"
        )
        self.assertEqual(single.returncode, 0, single.stdout + single.stderr)
        self.assertEqual(single.stdout.strip(), "7")
        multi = run(
            "read",
            str(source),
            "--json-root",
            "$.data",
            "-p",
            "$.items[0].id",
            "-p",
            "$.items[-1].id",
        )
        self.assertEqual(multi.returncode, 0, multi.stdout + multi.stderr)
        payload = json.loads(multi.stdout)
        self.assertEqual(
            {r["path"]: r["value"] for r in payload["results"]},
            {"$.items[0].id": "7", "$.items[-1].id": "8"},
        )
        vo = run(
            "read",
            str(source),
            "--json-root",
            "$.data",
            "-p",
            "$.items[0].id",
            "--value-only",
        )
        self.assertEqual(json.loads(vo.stdout), {"$.items[0].id": 7})

    def test_read_json_root_without_paths_is_guidance(self) -> None:
        source = self.dir / "rooted.json"
        source.write_text("{}", encoding="utf-8")
        res = run("read", str(source), "--json-root", "$.data")
        self.assertEqual(res.returncode, 1)
        self.assertIn("--json-root", res.stdout)
        self.assertIn("-p", res.stdout)
        self.assertNotIn("Traceback", res.stdout + res.stderr)

    # --- F4: [?k=v] must not match elements that lack the key ----------------------

    def test_read_pred_missing_key_never_matches_none_string(self) -> None:
        source = self.dir / "none.json"
        source.write_text(
            json.dumps(
                {
                    "xs": [
                        {"v": None, "tag": "nullval"},
                        {"w": 1, "tag": "missing"},
                        {"v": "None", "tag": "literal"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        # str(None) == "None" matches the null-valued element and the literal
        # string, but the element WITHOUT the key must not match.
        res = run("read", str(source), "-p", "$.xs[?v=None][0].tag")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(res.stdout.strip(), "nullval")
        res_all = run("read", str(source), "-p", "$.xs[?v=None].$len")
        # pred followed by $len is not an index continuation — error wording,
        # and the count (2, not 3) proves the key-missing element was excluded
        self.assertIn("2 matches for [?v=None]", res_all.stdout)

    def test_read_pred_missing_key_zero_match_when_only_absent(self) -> None:
        source = self.dir / "absent.json"
        source.write_text(
            json.dumps({"xs": [{"a": 1}, {}, {"b": None}]}), encoding="utf-8"
        )
        res = run("read", str(source), "-p", "$.xs[?v=None]")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("matched 0 elements", res.stdout)
        self.assertIn("(none)", res.stdout)

    def test_read_pred_numeric_cross_type_still_coerces(self) -> None:
        source = self.dir / "num.json"
        source.write_text(
            json.dumps({"xs": [{"id": 1, "tag": "int"}, {"id": "1", "tag": "str"}]}),
            encoding="utf-8",
        )
        res = run("read", str(source), "-p", "$.xs[?id=1][0].tag")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(res.stdout.strip(), "int")

    # --- F5: multi-x batch trim disclosure -----------------------------------------

    @unittest.skipUnless(HAVE_PARSEL, "parsel not installed")
    def test_read_files_multi_x_trim_drops_empty_groups_rows(self) -> None:
        batch = self.dir / "trimx"
        batch.mkdir()
        (batch / "f1.html").write_text(
            "<html><body>"
            + "".join(f"<p>{'x' * 20000}</p>" for _ in range(5))
            + "<h1>title</h1></body></html>",
            encoding="utf-8",
        )
        (batch / "f2.html").write_text(
            "<html><body><p>tiny</p><h1>small</h1></body></html>", encoding="utf-8"
        )
        res = run(
            "read",
            "--files",
            str(batch / "*.html"),
            "-x",
            "//p",
            "-x",
            "//h1",
            "--text",
            "--limit",
            "20000",
        )
        self.assertEqual(res.returncode, 0, res.stdout[:200] + res.stderr)
        self.assertLessEqual(len(res.stdout.rstrip("\r\n")), 80_100)
        data = json.loads(res.stdout)
        self.assertEqual(data["file_count"], 2)
        self.assertTrue(data["truncated"])
        # rows whose every group was popped are dropped, not left empty
        for row in data["results"]:
            self.assertNotEqual(row.get("groups"), [])
        self.assertEqual(data["shown"], len(data["results"]))
        self.assertEqual(data["shown"] + data["omitted"], data["file_count"])

    @unittest.skipUnless(HAVE_PARSEL, "parsel not installed")
    def test_read_files_multi_x_trim_preserves_match_limit_reason(self) -> None:
        page = self.dir / "reasonx.html"
        page.write_text(
            "<html><body><q>t</q>"
            + "".join(f"<p>{'x' * 19000}</p>" for _ in range(300))
            + "</body></html>",
            encoding="utf-8",
        )
        res = run(
            "read",
            "--files",
            str(page),
            "-x",
            "//p",
            "-x",
            "//q",
            "--text",
            "--show",
            "5",
            "--limit",
            "20000",
        )
        self.assertEqual(res.returncode, 0, res.stdout[:200] + res.stderr)
        data = json.loads(res.stdout)
        self.assertTrue(data["truncated"])
        row = data["results"][0]
        groups = {g["xpath"]: g for g in row["groups"]}
        p = groups["//p"]
        # //p was already value-limited by --show; output trimming popped more
        # values but must not rewrite the original match_limit reason
        self.assertLess(p["shown"], 5)
        self.assertEqual(p["count"], 300)
        self.assertEqual(p["shown"] + p["omitted"], 300)
        self.assertTrue(p["truncated"])
        self.assertEqual(p["truncation_reason"], "match_limit")

    # --- F6: cross-drive --list-file batches ---------------------------------------

    @staticmethod
    def _second_drive_anchor(anchor: str) -> Path | None:
        for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
            root = Path(f"{letter}:/")
            if root.is_dir() and root.drive.lower() != Path(anchor).drive.lower():
                return root
        return None

    def test_read_list_file_cross_drive_mix_works(self) -> None:
        other = self._second_drive_anchor(self.dir.anchor)
        if other is None:
            self.skipTest("host exposes a single drive — cross-drive path untestable")
        with tempfile.TemporaryDirectory(dir=str(other)) as foreign:
            f1 = self.dir / "home.json"
            f1.write_text(json.dumps({"code": 1}), encoding="utf-8")
            f2 = Path(foreign) / "foreign.json"
            f2.write_text(json.dumps({"code": 2}), encoding="utf-8")
            listfile = self.dir / "cross.txt"
            listfile.write_text(f"{f1}\n{f2}\n", encoding="utf-8")
            res = run("read", "--list-file", str(listfile), "-p", "$.code")
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            data = json.loads(res.stdout)
            self.assertEqual(data["file_count"], 2)
            self.assertEqual(sorted(r["value"] for r in data["results"]), ["1", "2"])
            self.assertNotIn("Traceback", res.stdout + res.stderr)

    def test_describe_scripts_list_file_cross_drive_mix_works(self) -> None:
        other = self._second_drive_anchor(self.dir.anchor)
        if other is None:
            self.skipTest("host exposes a single drive — cross-drive path untestable")
        with tempfile.TemporaryDirectory(dir=str(other)) as foreign:
            f1 = self.dir / "home.html"
            f1.write_text(
                '<html><script id="a">{"n":1}</script></html>', encoding="utf-8"
            )
            f2 = Path(foreign) / "foreign.html"
            f2.write_text(
                '<html><script id="b">{"n":2}</script></html>', encoding="utf-8"
            )
            listfile = self.dir / "cross.txt"
            listfile.write_text(f"{f1}\n{f2}\n", encoding="utf-8")
            res = run("describe", "--scripts", "--list-file", str(listfile))
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            data = json.loads(res.stdout)
            self.assertEqual(data["file_count"], 2)
            ids = {r["scripts"][0]["id"] for r in data["results"]}
            self.assertEqual(ids, {"a", "b"})
            self.assertNotIn("Traceback", res.stdout + res.stderr)

    # --- F7: argument discipline ---------------------------------------------------

    def test_read_files_value_only_with_multi_x_rejected(self) -> None:
        page = self.dir / "disc.html"
        page.write_text(HTML_PAGE, encoding="utf-8")
        res = run(
            "read",
            "--files",
            str(page),
            "-x",
            "//a",
            "-x",
            "//h1",
            "--value-only",
        )
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("--value-only", res.stdout)
        self.assertIn("already values-only", res.stdout)
        self.assertNotIn("Traceback", res.stdout + res.stderr)

    def test_describe_scripts_files_rejects_paths_and_limit(self) -> None:
        batch = self.dir / "disc"
        batch.mkdir()
        (batch / "a.html").write_text(
            '<html><script id="app">{"n":1}</script></html>', encoding="utf-8"
        )
        pathsfile = self.dir / "paths.txt"
        pathsfile.write_text("$.x\n", encoding="utf-8")
        for extra in (
            ("-p", "$.x"),
            ("--paths-file", str(pathsfile)),
            ("--limit", "900"),
        ):
            with self.subTest(extra=extra):
                res = run(
                    "describe", "--scripts", "--files", str(batch / "*.html"), *extra
                )
                self.assertEqual(res.returncode, 1, res.stdout)
                self.assertIn("--scripts", res.stdout)
                self.assertNotIn("Traceback", res.stdout + res.stderr)

    def test_read_list_file_exclude_applies_after_collection(self) -> None:
        batch = self.dir / "exlist"
        batch.mkdir()
        for name in ("a.json", "b.json", "c.json"):
            (batch / name).write_text(json.dumps({"code": name[0]}), encoding="utf-8")
        listfile = self.dir / "all.txt"
        listfile.write_text(
            "\n".join(str(batch / n) for n in ("a.json", "b.json", "c.json")),
            encoding="utf-8",
        )
        res = run(
            "read", "--list-file", str(listfile), "--exclude", "b.json", "-p", "$.code"
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["file_count"], 2)
        self.assertFalse(any("b.json" in r["file"] for r in data["results"]))
        all_excluded = run(
            "read",
            "--list-file",
            str(listfile),
            "--exclude",
            "*.json",
            "-p",
            "$.code",
        )
        self.assertEqual(all_excluded.returncode, 1, all_excluded.stdout)
        self.assertIn("no files remain after excludes", all_excluded.stdout)

    def test_list_file_empty_error_names_list_file_flag(self) -> None:
        listfile = self.dir / "empty.txt"
        listfile.write_text("# only a comment\n\n", encoding="utf-8")
        res = run("read", "--list-file", str(listfile), "-p", "$.code")
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("--list-file", res.stdout)
        self.assertNotIn("--files-file", res.stdout)
        self.assertNotIn("Traceback", res.stdout + res.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
