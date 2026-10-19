"""Issue #2 output, identity, coverage and Unicode regressions."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_inspect_sample import HAVE_PARSEL, SCRIPT, run


class Issue2OutputTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def source(self, name: str, data: object) -> Path:
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def in_dir(self, directory: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=directory,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )

    def test_bare_globs_compact_preserve_same_basename_sources(self) -> None:
        self.source("left/same.json", {"id": 1})
        self.source("right/same.json", {"id": 2})
        result = self.in_dir(
            self.dir / "left",
            "read",
            "--files",
            "*.json",
            "--files",
            "../right/*.json",
            "-p",
            "$.id",
            "--value-only",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(len(payload), 2)
        self.assertCountEqual(list(payload.values()), [1, 2])

    def test_relative_glob_root_and_sources_are_canonicalized_together(self) -> None:
        self.source("real/sample.json", {"id": 7})
        result = self.in_dir(
            self.dir, "read", "--files", "real/../real/*.json", "-p", "$.id"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = json.loads(result.stdout)["results"][0]
        self.assertEqual(row["file"], "sample.json")
        self.assertEqual(Path(row["path"]), (self.dir / "real/sample.json").resolve())

    def test_symlink_root_alias_keeps_glob_relative_display_identity(self) -> None:
        real = self.dir / "physical"
        self.source("physical/sample.json", {"id": 7})
        alias = self.dir / "alias"
        try:
            alias.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")
        result = run("read", "--files", str(alias / "*.json"), "-p", "$.id")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = json.loads(result.stdout)["results"][0]
        self.assertEqual(row["file"], "sample.json")
        self.assertEqual(Path(row["path"]), (real / "sample.json").resolve())

    def test_two_glob_spellings_of_same_source_deduplicate(self) -> None:
        self.source("same/file.json", {"id": 7})
        result = self.in_dir(
            self.dir,
            "read",
            "--files",
            "same/*.json",
            "--files",
            "same/../same/*.json",
            "-p",
            "$.id",
            "--value-only",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout), {"file.json": 7})

    def test_compact_small_containers_preserve_native_json_types(self) -> None:
        src = self.source("small.json", {"obj": {"x": 1}, "arr": [1, 2], "ok": True})
        result = run(
            "read",
            str(src),
            "-p",
            "$.obj",
            "-p",
            "$.arr",
            "-p",
            "$.ok",
            "--value-only",
            "--limit",
            "80",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {"$.obj": {"x": 1}, "$.arr": [1, 2], "$.ok": True},
        )

    def test_compact_large_containers_obey_limit_and_keep_small_siblings(self) -> None:
        src = self.source(
            "large.json",
            {"obj": {"blob": "x" * 100000}, "arr": list(range(500)), "ok": True},
        )
        result = run(
            "read",
            str(src),
            "-p",
            "$.obj",
            "-p",
            "$.arr",
            "-p",
            "$.ok",
            "--value-only",
            "--limit",
            "50",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("$.ok", payload)
        self.assertIs(payload["$.ok"], True)
        for key in ("$.obj", "$.arr"):
            with self.subTest(key=key):
                self.assertIsInstance(payload[key], str)
                self.assertLessEqual(len(payload[key]), 50)
                self.assertIn("[truncated]", payload[key])

    def test_compact_high_escape_value_cannot_erase_small_sibling(self) -> None:
        src = self.source("escaped.json", {"big": "\x00" * 20000, "ok": True})
        result = run(
            "read",
            str(src),
            "-p",
            "$.big",
            "-p",
            "$.ok",
            "--value-only",
            "--limit",
            "20000",
        )
        self.assertEqual(result.returncode, 0, result.stdout[:300] + result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("$.ok", payload)
        self.assertIs(payload["$.ok"], True)
        self.assertLessEqual(len(result.stdout.rstrip("\r\n")), 80000)

    def test_compact_escape_previews_preserve_batch_small_siblings(self) -> None:
        for value in ("\x00" * 20000, {"blob": "\x00" * 20000}):
            for order in (("$.big", "$.ok"), ("$.ok", "$.big")):
                with self.subTest(kind=type(value).__name__, order=order):
                    src = self.source("escaped-batch.json", {"big": value, "ok": True})
                    result = run(
                        "read",
                        "--files",
                        str(src),
                        "-p",
                        *order,
                        "--value-only",
                        "--limit",
                        "20000",
                    )
                    self.assertEqual(
                        result.returncode, 0, result.stdout[:300] + result.stderr
                    )
                    payload = json.loads(result.stdout)
                    self.assertIn("escaped-batch.json", payload)
                    row = payload["escaped-batch.json"]
                    self.assertIn("$.ok", row)
                    self.assertIs(row["$.ok"], True)
                    self.assertLessEqual(len(result.stdout.rstrip("\r\n")), 80000)

    def test_compact_packing_keeps_small_trailing_fields(self) -> None:
        src = self.source(
            "pack.json", {**{f"big{i}": "x" * 20000 for i in range(4)}, "ok": True}
        )
        result = run(
            "read",
            str(src),
            "-p",
            "$.big0",
            "$.big1",
            "$.big2",
            "$.big3",
            "$.ok",
            "--value-only",
            "--limit",
            "20000",
        )
        self.assertEqual(result.returncode, 0, result.stdout[:300] + result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("$.ok", payload)
        self.assertIs(payload["$.ok"], True)
        self.assertEqual(payload["__compact__"]["shown"], 4)
        self.assertEqual(payload["__compact__"]["omitted"], 1)

    def test_compact_batch_large_container_is_bounded(self) -> None:
        src = self.source("large.json", {"obj": {"blob": "x" * 100000}})
        result = run(
            "read", "--files", str(src), "-p", "$.obj", "--value-only", "--limit", "50"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertIsInstance(payload["large.json"], str)
        self.assertLessEqual(len(payload["large.json"]), 50)
        self.assertIn("[truncated]", payload["large.json"])

    def test_compact_batch_reserved_filenames_never_become_envelope_fields(
        self,
    ) -> None:
        for name in (
            "results",
            "scripts",
            "hits",
            "values",
            "error",
            "__hint__",
            "__compact__",
        ):
            with self.subTest(name=name):
                src = self.source(f"reserved/{name}", {"blob": ["x" * 90000]})
                result = run(
                    "read",
                    "--files",
                    str(src),
                    "-p",
                    "$.blob",
                    "--value-only",
                    "--limit",
                    "16",
                )
                self.assertEqual(
                    result.returncode, 0, result.stdout[:300] + result.stderr
                )
                payload = json.loads(result.stdout)
                self.assertIn(name, payload)
                self.assertIsInstance(payload[name], str)
                self.assertLessEqual(len(payload[name]), 16)
                self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_compact_budget_trim_retains_data_and_discloses_omissions(self) -> None:
        for n in range(12):
            self.source(f"fat/{n:02d}.json", {"value": "x" * 20000})
        result = run(
            "read",
            "--files",
            str(self.dir / "fat/*.json"),
            "-p",
            "$.value",
            "--value-only",
            "--limit",
            "20000",
        )
        self.assertEqual(result.returncode, 0, result.stdout[:300] + result.stderr)
        self.assertLessEqual(len(result.stdout.rstrip("\r\n")), 80000)
        payload = json.loads(result.stdout)
        data = {k: v for k, v in payload.items() if k.endswith(".json")}
        self.assertGreater(len(data), 0)
        meta = next(
            v
            for k, v in payload.items()
            if k not in data and isinstance(v, dict) and v.get("truncated")
        )
        self.assertEqual(meta["shown"], len(data))
        self.assertEqual(meta["shown"] + meta["omitted"], 12)

    def test_compact_omission_metadata_cannot_overwrite_real_file(self) -> None:
        for name in (
            "__compact__",
            "__compact__#1",
            "__hint__",
            "error",
            "results",
            "scripts",
            "hits",
            "f7",
            "f8",
        ):
            self.source(f"metadata/{name}", {"value": "x" * 15000})
        result = run(
            "read",
            "--files",
            str(self.dir / "metadata/*"),
            "-p",
            "$.value",
            "--value-only",
            "--limit",
            "15000",
        )
        self.assertEqual(result.returncode, 0, result.stdout[:300] + result.stderr)
        payload = json.loads(result.stdout)
        self.assertIsInstance(payload["__compact__"], str)
        self.assertIsInstance(payload["__compact__#1"], str)
        meta = [
            v for v in payload.values() if isinstance(v, dict) and v.get("truncated")
        ]
        self.assertEqual(len(meta), 1)

    def test_matrix_coverage_counts_only_files_with_emitted_paths(self) -> None:
        for n in range(3):
            self.source(f"matrix/{n}.json", {"a": "x" * 20000, "b": "y" * 20000})
        result = run(
            "read",
            "--files",
            str(self.dir / "matrix/*.json"),
            "-p",
            "$.a",
            "-p",
            "$.b",
            "--limit",
            "20000",
        )
        self.assertEqual(result.returncode, 0, result.stdout[:300] + result.stderr)
        payload = json.loads(result.stdout)
        displayed = sum(bool(r["results"]) for r in payload["results"])
        self.assertEqual(payload["shown"], displayed)
        self.assertEqual(payload["shown"] + payload["omitted"], 3)
        for row in payload["results"]:
            self.assertEqual(row["shown"], len(row["results"]))
            self.assertEqual(row["shown"] + row["omitted"], 2)

    @unittest.skipUnless(HAVE_PARSEL, "parsel not installed")
    def test_batch_xpath_trim_refreshes_clipped_value_indices(self) -> None:
        page = self.dir / "groups.html"
        page.write_text(
            "<html>" + ("<p>" + "x" * 6000 + "</p>") * 20 + "<q>small</q></html>",
            encoding="utf-8",
        )
        result = run(
            "read", "--files", str(page), "-x", "//p", "-x", "//q", "--limit", "5000"
        )
        self.assertEqual(result.returncode, 0, result.stdout[:300] + result.stderr)
        payload = json.loads(result.stdout)
        self.assertLessEqual(len(result.stdout.rstrip("\r\n")), 80000)
        group = payload["results"][0]["groups"][0]
        self.assertEqual(group["shown"], len(group["values"]))
        self.assertEqual(group["truncated_value_indices"], list(range(group["shown"])))
        self.assertEqual(group["values_truncated"], group["shown"])
        self.assertEqual(group["shown"] + group["omitted"], 20)

    def test_malformed_marked_declarations_never_bypass_fragment_guard(self) -> None:
        for html in (
            "<html-fragment><![not-html]></html-fragment>",
            "<!-- <html --> <![not-html]>",
            "<!DOCTYPE html><div><![not-html]></div>",
        ):
            src = self.dir / "malformed.html"
            src.write_text(html, encoding="utf-8")
            for mode in (
                ("describe", "--scripts"),
                ("describe",),
                ("read", "-p", "$#script[0]"),
            ):
                with self.subTest(html=html, mode=mode):
                    result = run(mode[0], str(src), *mode[1:])
                    self.assertNotIn("Traceback", result.stdout + result.stderr)
                    self.assertTrue(result.stdout.strip())
                    self.assertLess(len(result.stdout), 24000)

    def test_grep_offsets_stay_in_original_unicode_text(self) -> None:
        page = self.dir / "unicode.txt"
        page.write_bytes("İprice\nİPRICE price".encode("utf-8"))
        result = run("read", str(page), "--grep", "price", "--context", "0")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual([h["offset"] for h in payload["hits"]], [1, 8, 14])
        self.assertEqual([h["line"] for h in payload["hits"]], [1, 2, 2])
        self.assertEqual(
            [h["context"] for h in payload["hits"]], ["price", "PRICE", "price"]
        )

    def test_grep_literal_metacharacters_and_original_unicode_span(self) -> None:
        page = self.dir / "literal.txt"
        page.write_text("İ[A+B] [a+b] aab", encoding="utf-8")
        result = run("read", str(page), "--grep", "[a+b]", "--context", "0")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["count"], 2)
        self.assertEqual([h["offset"] for h in payload["hits"]], [1, 7])
        self.assertEqual([h["context"] for h in payload["hits"]], ["[A+B]", "[a+b]"])

    def test_grep_overlap_and_duplicate_keywords_keep_original_spans(self) -> None:
        page = self.dir / "overlap.txt"
        page.write_text("İfoobar", encoding="utf-8")
        result = run(
            "read",
            str(page),
            "--grep",
            "foo",
            "--grep",
            "FOO",
            "--grep",
            "foobar",
            "--context",
            "0",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["count"], 2)
        self.assertEqual([h["offset"] for h in payload["hits"]], [1, 1])
        self.assertCountEqual(
            [h["context"] for h in payload["hits"]], ["foo", "foobar"]
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
