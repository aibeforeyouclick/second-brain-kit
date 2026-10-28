"""Unit tests for kb_frontmatter, run against copies of the invented sample vault."""
import io
import json
import os
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))
sys.path.insert(0, str(HERE))
import kb_common as kb  # noqa: E402
import kb_frontmatter  # noqa: E402
from test_kb_common import fixture_copy  # noqa: E402
from test_kb_lint import tree_hash  # noqa: E402

NEW_FIELDS = {"title": "Daily note", "type": "daily", "status": "draft", "summary": "A day."}


def run(root, mapping, *flags, raw_map=None):
    map_path = Path(root).parent / "map.json"
    map_path.write_text(raw_map if raw_map is not None else json.dumps(mapping), encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = kb_frontmatter.main(["--root", str(root), "--map", str(map_path), *flags])
    return code, out.getvalue(), err.getvalue()


def body_of(path: Path) -> bytes:
    return kb.split_bytes(Path(path).read_bytes())[2]


class DryRunAndWriteTests(unittest.TestCase):
    def test_dry_run_is_the_default_and_writes_nothing(self):
        root = fixture_copy()
        before = tree_hash(root)
        code, out, _ = run(root, {"Daily/2024-05-01.md": NEW_FIELDS})
        self.assertEqual(code, 0)
        self.assertIn("dry-run  Daily/2024-05-01.md", out)
        self.assertEqual(before, tree_hash(root))
        _, js, _ = run(root, {"Daily/2024-05-01.md": NEW_FIELDS}, "--json")
        entry = json.loads(js)["notes"][0]
        self.assertEqual(entry["fields_added"], ["title", "type", "status", "summary", "updated"])
        self.assertIn('title: "Daily note"', entry["lines"])

    def test_write_adds_frontmatter_and_the_body_is_byte_identical(self):
        root = fixture_copy()
        note = root / "Daily" / "2024-05-01.md"
        original = note.read_bytes()
        code, out, _ = run(root, {"Daily/2024-05-01.md": NEW_FIELDS}, "--write")
        self.assertEqual(code, 0)
        self.assertIn("1 written", out)
        self.assertEqual(body_of(note), original)
        self.assertTrue(note.read_bytes().endswith(original))
        fm = kb.parse_frontmatter(note.read_text(encoding="utf-8"))
        self.assertEqual((fm["title"], fm["type"], fm["status"]), ("Daily note", "daily", "draft"))

    def test_second_run_changes_nothing(self):
        root = fixture_copy()
        run(root, {"Daily/2024-05-01.md": NEW_FIELDS}, "--write")
        after_first = tree_hash(root)
        code, out, _ = run(root, {"Daily/2024-05-01.md": NEW_FIELDS}, "--write")
        self.assertEqual((code, tree_hash(root)), (0, after_first))
        self.assertIn("1 skipped", out)
        self.assertIn("already has frontmatter", out)

    def test_no_temporary_files_are_left_behind(self):
        root = fixture_copy()
        run(root, {"Daily/2024-05-01.md": NEW_FIELDS}, "--write")
        self.assertEqual([p for p in root.rglob(".kbtmp-*")], [])

    def test_written_hashes_are_in_the_report_file(self):
        root = fixture_copy()
        report = root.parent / "report.json"
        run(root, {"Daily/2024-05-01.md": NEW_FIELDS}, "--write", "--report", str(report))
        entry = json.loads(report.read_text(encoding="utf-8"))["notes"][0]
        self.assertEqual(entry["action"], "written")
        self.assertEqual(entry["body_sha256_before"], entry["body_sha256_after"])
        self.assertEqual(len(entry["body_sha256_before"]), 64)

    def test_report_file_must_be_new_and_json(self):
        root = fixture_copy()
        existing = root.parent / "exists.json"
        existing.write_text("{}", encoding="utf-8")
        self.assertEqual(run(root, {"Daily/2024-05-01.md": NEW_FIELDS}, "--report", str(existing))[0], 2)
        self.assertEqual(run(root, {"Daily/2024-05-01.md": NEW_FIELDS}, "--report", str(root.parent / "r.txt"))[0], 2)
        self.assertEqual(existing.read_text(encoding="utf-8"), "{}")


class QuotingAndEncodingTests(unittest.TestCase):
    TRICKY = ['He said "hi": a: b', "back\\slash", "# not a comment", "- not a list", "yes", "null", "123", "Caf\u00e9 \u05d0",
              "line one\nline two", "[brackets]", "{braces}", " leading space"]

    def test_every_text_value_is_written_as_a_json_string_that_reads_back_exactly(self):
        root = fixture_copy()
        note = root / "Daily" / "2024-05-01.md"
        fields = {"title": "T", "type": "daily", "status": "draft", "summary": "s"}
        for i, value in enumerate(self.TRICKY):
            fields[f"f{i}"] = value
        run(root, {"Daily/2024-05-01.md": fields}, "--write")
        lines = {ln.split(": ", 1)[0]: ln.split(": ", 1)[1] for ln in note.read_text(encoding="utf-8").split("\n---")[0].splitlines()[1:]}
        for i, value in enumerate(self.TRICKY):
            self.assertEqual(json.loads(lines[f"f{i}"]), value, value)

    def test_lists_numbers_booleans_dates_and_odd_field_names(self):
        root = fixture_copy()
        note = root / "Daily" / "2024-05-01.md"
        run(root, {"Daily/2024-05-01.md": {"title": "T", "type": "daily", "status": "draft", "summary": "s",
                                           "tags": ["a", "b c"], "count": 3, "flag": True, "updated": "2024-02-29",
                                           "source_date": "not a date", "due date": "2024-01-01"}}, "--write")
        text = note.read_text(encoding="utf-8")
        for expected in ('tags: ["a", "b c"]', "count: 3", "flag: true", "updated: 2024-02-29",
                         'source_date: "not a date"', '"due date": "2024-01-01"'):
            self.assertIn(expected, text)

    def test_bom_and_crlf_are_kept(self):
        root = fixture_copy()
        crlf = root / "Crlf.md"
        crlf.write_bytes(kb.UTF8_BOM + b"# Title\r\nline two\r\n")
        lf = root / "Lf.md"
        lf.write_bytes(b"# Title\nline two\n")
        run(root, {"Crlf.md": NEW_FIELDS, "Lf.md": NEW_FIELDS}, "--write")
        raw = crlf.read_bytes()
        self.assertTrue(raw.startswith(kb.UTF8_BOM + b"---\r\n"))
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        self.assertTrue(raw.endswith(b"# Title\r\nline two\r\n"))
        raw = lf.read_bytes()
        self.assertNotIn(b"\r", raw)
        self.assertTrue(raw.endswith(b"# Title\nline two\n"))

    def test_note_with_non_ascii_text_in_the_body_stays_intact(self):
        root = fixture_copy()
        note = root / "Unicode.md"
        body = "# \u00dcber caf\u00e9\nline with \u05d0\u05d1\u05d2 and emoji \U0001F600\n".encode("utf-8")
        note.write_bytes(body)
        run(root, {"Unicode.md": NEW_FIELDS}, "--write")
        self.assertEqual(body_of(note), body)


class UpdatedFieldTests(unittest.TestCase):
    def test_updated_comes_from_the_modification_date_unless_given(self):
        root = fixture_copy()
        a, b = root / "A.md", root / "B.md"
        a.write_text("# A\n", encoding="utf-8")
        b.write_text("# B\n", encoding="utf-8")
        stamp = time.mktime((2024, 3, 5, 12, 0, 0, 0, 0, -1))
        os.utime(a, (stamp, stamp))
        os.utime(b, (stamp, stamp))
        run(root, {"A.md": NEW_FIELDS, "B.md": dict(NEW_FIELDS, updated="2020-01-02")}, "--write")
        self.assertEqual(kb.parse_frontmatter(a.read_text(encoding="utf-8"))["updated"], "2024-03-05")
        self.assertEqual(kb.parse_frontmatter(b.read_text(encoding="utf-8"))["updated"], "2020-01-02")

    def test_no_auto_updated_leaves_it_out(self):
        root = fixture_copy()
        run(root, {"Daily/2024-05-01.md": NEW_FIELDS}, "--write", "--no-auto-updated")
        self.assertNotIn("updated", kb.parse_frontmatter((root / "Daily" / "2024-05-01.md").read_text(encoding="utf-8")))


class MergeTests(unittest.TestCase):
    def test_existing_frontmatter_is_skipped_unless_merge(self):
        root = fixture_copy()
        before = (root / "Concepts" / "Orphan Idea.md").read_bytes()
        code, out, _ = run(root, {"Concepts/Orphan Idea.md": {"updated": "2024-01-01", "summary": "New summary"}}, "--write")
        self.assertEqual((code, (root / "Concepts" / "Orphan Idea.md").read_bytes()), (0, before))
        self.assertIn("use --merge", out)

    def test_merge_adds_only_missing_fields_and_keeps_every_existing_line(self):
        root = fixture_copy()
        note = root / "Concepts" / "Orphan Idea.md"  # has an empty `summary:` line and no `updated`
        before = note.read_bytes()
        _, old_fm, old_body = kb.split_bytes(before)
        run(root, {"Concepts/Orphan Idea.md": {"updated": "2024-01-01", "summary": "New summary", "title": "Other title",
                                               "domain": "work"}}, "--write", "--merge")
        after = note.read_bytes()
        _, new_fm, new_body = kb.split_bytes(after)
        self.assertEqual(new_body, old_body)
        for line in old_fm.splitlines(keepends=True)[:-1]:
            self.assertIn(line, new_fm)  # existing lines untouched (including the empty summary)
        parsed = kb.parse_frontmatter(after.decode("utf-8"))
        self.assertEqual((parsed["updated"], parsed["domain"], parsed["title"], parsed["summary"]), ("2024-01-01", "work", "Orphan Idea", ""))
        self.assertEqual(new_fm.count(b"summary:"), 1)  # no duplicate key
        self.assertTrue(new_fm.rstrip().endswith(b"---"))

    def test_merge_with_nothing_missing_is_a_skip(self):
        root = fixture_copy()
        code, out, _ = run(root, {"Concepts/Caf\u00e9 Ideas.md": {"title": "x"}}, "--write", "--merge", "--no-auto-updated")
        self.assertIn("nothing to add", out)

    def test_merge_keeps_the_files_line_endings(self):
        root = fixture_copy()
        note = root / "Crlf.md"
        note.write_bytes(b"---\r\ntitle: Old\r\n---\r\nbody\r\n")
        run(root, {"Crlf.md": {"status": "draft"}}, "--write", "--merge", "--no-auto-updated")
        self.assertEqual(note.read_bytes(), b"---\r\ntitle: Old\r\nstatus: \"draft\"\r\n---\r\nbody\r\n")


class VerificationTests(unittest.TestCase):
    def test_a_changed_body_after_writing_restores_the_original_and_stops(self):
        root = fixture_copy()
        first, second = root / "A.md", root / "B.md"
        first.write_bytes(b"# A\nbody a\n")
        second.write_bytes(b"# B\nbody b\n")
        real = kb_frontmatter.replace_file
        calls = {"n": 0}

        def mangling(path, data):
            calls["n"] += 1
            real(path, data[:-1] + b"X" if calls["n"] == 1 else data)  # only the first write is damaged

        with mock.patch.object(kb_frontmatter, "replace_file", mangling):
            code, out, err = run(root, {"A.md": NEW_FIELDS, "B.md": NEW_FIELDS}, "--write", "--json")
        self.assertEqual(code, 3)
        self.assertIn("STOPPED", err)
        self.assertEqual(first.read_bytes(), b"# A\nbody a\n")      # restored exactly
        self.assertEqual(second.read_bytes(), b"# B\nbody b\n")      # never touched
        actions = [e["action"] for e in json.loads(out)["notes"]]
        self.assertEqual(actions, ["restored"])

    def test_the_read_back_check_is_real_not_assumed(self):
        root = fixture_copy()
        note = root / "A.md"
        note.write_bytes(b"# A\nbody a\n")
        with mock.patch.object(kb_frontmatter, "verify_written", return_value=False):
            code, _, _ = run(root, {"A.md": NEW_FIELDS}, "--write")
        self.assertEqual((code, note.read_bytes()), (3, b"# A\nbody a\n"))

    def test_nothing_is_verified_or_written_in_a_dry_run(self):
        root = fixture_copy()
        with mock.patch.object(kb_frontmatter, "replace_file", side_effect=AssertionError("must not write")):
            self.assertEqual(run(root, {"Daily/2024-05-01.md": NEW_FIELDS})[0], 0)


class MappingValidationTests(unittest.TestCase):
    def assert_rejected(self, mapping, fragment, raw_map=None):
        root = fixture_copy()
        before = tree_hash(root)
        good = {"Daily/2024-05-01.md": NEW_FIELDS}
        code, _, err = run(root, dict(good, **mapping) if mapping else None, "--write", raw_map=raw_map)
        self.assertEqual(code, 2, mapping)
        self.assertIn(fragment, err)
        self.assertEqual(before, tree_hash(root))  # nothing written, not even the valid first entry

    def test_bad_paths_values_and_types_are_rejected_before_anything_is_written(self):
        self.assert_rejected({"../outside.md": NEW_FIELDS}, "stay inside")
        self.assert_rejected({"/abs.md": NEW_FIELDS}, "stay inside")
        self.assert_rejected({"C:/x.md": NEW_FIELDS}, "stay inside")
        self.assert_rejected({"A.md": "text"}, "non-empty object")
        self.assert_rejected({"A.md": {}}, "non-empty object")
        self.assert_rejected({"A.md": {"title": {"nested": 1}}}, "must be text")
        self.assert_rejected({"A.md": {"tags": [["x"]]}}, "must be text")
        self.assert_rejected({"./Daily/2024-05-01.md": NEW_FIELDS}, "listed twice")

    def test_unreadable_or_wrong_shaped_mapping_files(self):
        self.assert_rejected(None, "cannot read mapping", raw_map="{broken")
        self.assert_rejected(None, "must be a JSON object", raw_map="[1, 2]")

    def test_missing_file_stops_the_run_with_an_error_entry(self):
        root = fixture_copy()
        code, out, _ = run(root, {"Daily/2024-05-01.md": NEW_FIELDS, "Nope.md": NEW_FIELDS}, "--write")
        self.assertEqual(code, 3)
        self.assertIn("file not found", out)

    def test_missing_map_file_and_bad_root_exit_2(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(kb_frontmatter.main(["--root", str(fixture_copy()), "--map", "no-such-file.json"]), 2)
            self.assertEqual(kb_frontmatter.main(["--root", "no-such-folder", "--map", "x.json"]), 2)
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            kb_frontmatter.main(["--map", "x.json", "--write", "--dry-run"])


class Utf16NoteTests(unittest.TestCase):
    def test_a_utf16_note_is_skipped_and_never_touched(self):
        root = fixture_copy()
        note = root / "Daily" / "2024-05-01.md"
        data = b"\xff\xfe" + note.read_bytes().decode("utf-8").lstrip(kb.BOM).encode("utf-16-le")
        note.write_bytes(data)
        code, out, _ = run(root, {"Daily/2024-05-01.md": NEW_FIELDS}, "--write")
        self.assertEqual(note.read_bytes(), data)
        entry = json.loads(run(root, {"Daily/2024-05-01.md": NEW_FIELDS}, "--json")[1])["notes"][0]
        self.assertEqual(entry["action"], "skipped")
        self.assertIn("UTF-16", entry["reason"])

    def test_a_utf8_bom_note_keeps_its_bom_after_a_write(self):
        root = fixture_copy()
        note = root / "Daily" / "2024-05-01.md"
        note.write_bytes(kb.UTF8_BOM + note.read_bytes().lstrip(kb.UTF8_BOM))
        original_body = body_of(note)
        run(root, {"Daily/2024-05-01.md": NEW_FIELDS}, "--write")
        raw = note.read_bytes()
        self.assertTrue(raw.startswith(kb.UTF8_BOM + b"---"))
        self.assertEqual(body_of(note), original_body)


class SkipTests(unittest.TestCase):
    def test_exempt_hidden_non_markdown_unclosed_and_cloud_only_notes_are_skipped(self):
        root = fixture_copy()
        (root / "Unclosed.md").write_bytes(b"---\ntitle: never closed\nbody\n")
        (root / ".obsidian" / "x.md").write_text("# hidden\n", encoding="utf-8")
        (root / "Cloudy.md").write_text("# Cloudy\n", encoding="utf-8")
        before = {p: p.read_bytes() for p in (root / "README.md", root / "Unclosed.md", root / ".obsidian" / "x.md", root / "Cloudy.md")}
        mapping = {n: NEW_FIELDS for n in ("README.md", "Unclosed.md", ".obsidian/x.md", "Cloudy.md", "assets/diagram.png")}

        def attrs(path):
            return 0x400000 if Path(path).name == "Cloudy.md" else 0

        with mock.patch.object(kb, "file_attributes", attrs):
            code, out, _ = run(root, mapping, "--write", "--json")
        self.assertEqual(code, 0)
        reasons = {e["path"]: e["reason"] for e in json.loads(out)["notes"]}
        self.assertIn("exempt", reasons["README.md"])
        self.assertIn("unclosed", reasons["Unclosed.md"])
        self.assertIn("not a note", reasons[".obsidian/x.md"])
        self.assertIn("cloud-only", reasons["Cloudy.md"])
        self.assertIn("not a note", reasons["assets/diagram.png"])
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)

    def test_a_cloud_only_note_is_never_opened(self):
        root = fixture_copy()
        opened = []
        real = Path.read_bytes

        def spy(self):
            opened.append(self.name)
            return real(self)

        with mock.patch.object(kb, "file_attributes", lambda p: 0x1000 if Path(p).name == "2024-05-01.md" else 0), \
                mock.patch.object(Path, "read_bytes", spy):
            run(root, {"Daily/2024-05-01.md": NEW_FIELDS}, "--write")
        self.assertNotIn("2024-05-01.md", opened)


if __name__ == "__main__":
    unittest.main()
