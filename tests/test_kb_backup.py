"""Unit tests for kb_backup, run against copies of the invented sample vault."""
import io
import json
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))
sys.path.insert(0, str(HERE))
import kb_backup  # noqa: E402
import kb_common as kb  # noqa: E402
import kb_frontmatter  # noqa: E402
from test_kb_common import fixture_copy  # noqa: E402
from test_kb_lint import tree_hash  # noqa: E402


def run(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = kb_backup.main(list(args))
    return code, out.getvalue(), err.getvalue()


def vault_with_system():
    root = fixture_copy()
    (root / "_system" / "tools" / "__pycache__").mkdir(parents=True)
    (root / "_system" / "SCHEMA.md").write_text("# Schema\n", encoding="utf-8")
    (root / "_system" / "tools" / "helper.py").write_text("print(1)\n", encoding="utf-8")
    (root / "_system" / "tools" / "__pycache__" / "helper.pyc").write_bytes(b"\x00\x01")
    return root


def backup(root, dest=None):
    dest = dest or Path(tempfile.mkdtemp(prefix="kbdest-"))
    code, out, err = run("--root", str(root), "--dest", str(dest))
    assert code == 0, err
    return next(Path(dest).glob("*.zip")) if Path(dest).is_dir() else Path(dest)


def names(archive):
    with zipfile.ZipFile(archive) as zf:
        return sorted(zf.namelist())


class CreateTests(unittest.TestCase):
    def test_archive_holds_notes_system_config_and_a_manifest_and_nothing_else(self):
        root = vault_with_system()
        (root / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
        got = names(backup(root))
        self.assertEqual([n for n in got if n.startswith("_system/")], ["_system/SCHEMA.md", "_system/tools/helper.py"])
        self.assertIn("kb.config.json", got)
        self.assertIn(kb_backup.MANIFEST, got)
        self.assertIn("Projects/Alpha/Meeting Notes 2024.md", got)
        self.assertIn("Concepts/Café Ideas.md", got)
        for absent in ("templates/Note Template.md", "assets/diagram.png", "docs/Spec Sheet.pdf", ".obsidian/app.json"):
            self.assertNotIn(absent, got)
        self.assertFalse([n for n in got if "__pycache__" in n])

    def test_manifest_lists_every_file_with_its_sha256(self):
        root = vault_with_system()
        archive = backup(root)
        with zipfile.ZipFile(archive) as zf:
            manifest = json.loads(zf.read(kb_backup.MANIFEST).decode("utf-8"))
            for item in manifest["files"]:
                self.assertEqual(kb.sha256(zf.read(item["path"])), item["sha256"])
            self.assertEqual({i["path"] for i in manifest["files"]}, set(zf.namelist()) - {kb_backup.MANIFEST})

    def test_notes_in_exempt_dirs_follow_the_config(self):
        root = vault_with_system()
        cfg = json.loads((root / "kb.config.json").read_text(encoding="utf-8"))
        cfg["exempt_dirs"] = ["Daily/"]
        (root / "kb.config.json").write_text(json.dumps(cfg), encoding="utf-8")
        got = names(backup(root))
        self.assertNotIn("Daily/2024-05-01.md", got)
        self.assertIn("templates/Note Template.md", got)

    def test_explicit_zip_path_creates_parent_folders_and_the_vault_is_not_touched(self):
        root = vault_with_system()
        before = tree_hash(root)
        target = Path(tempfile.mkdtemp(prefix="kbdest-")) / "deep" / "copy.zip"
        self.assertEqual(run("--root", str(root), "--dest", str(target))[0], 0)
        self.assertTrue(target.is_file())
        self.assertEqual(before, tree_hash(root))

    def test_destination_inside_the_vault_is_refused(self):
        root = vault_with_system()
        for dest in (root, root / "Projects", root / "inside.zip"):
            code, _, err = run("--root", str(root), "--dest", str(dest))
            self.assertEqual(code, 2, dest)
            self.assertIn("inside the vault", err)
        self.assertEqual(list(root.rglob("*.zip")), [])

    def test_an_existing_archive_is_never_overwritten(self):
        root = vault_with_system()
        target = Path(tempfile.mkdtemp(prefix="kbdest-")) / "keep.zip"
        target.write_bytes(b"precious")
        code, _, err = run("--root", str(root), "--dest", str(target))
        self.assertEqual(code, 2)
        self.assertIn("refusing to overwrite", err)
        self.assertEqual(target.read_bytes(), b"precious")

    def test_cloud_only_files_are_listed_in_the_manifest_not_downloaded(self):
        root = vault_with_system()
        opened = []
        real = Path.read_bytes

        def spy(self):
            opened.append(self.name)
            return real(self)

        dest = Path(tempfile.mkdtemp(prefix="kbdest-"))
        with mock.patch.object(kb, "file_attributes", lambda p: 0x400000 if Path(p).name == "Overview.md" else 0), \
                mock.patch.object(Path, "read_bytes", spy):
            code, out, _ = run("--root", str(root), "--dest", str(dest))
        archive = next(dest.glob("*.zip"))
        self.assertEqual(code, 0)
        self.assertIn("skipped 2 cloud-only files", out)
        self.assertNotIn("Overview.md", opened)
        self.assertFalse([n for n in names(archive) if n.endswith("Overview.md")])
        with zipfile.ZipFile(archive) as zf:
            skipped = {i["path"] for i in json.loads(zf.read(kb_backup.MANIFEST).decode("utf-8"))["skipped"]}
        self.assertEqual(skipped, {"Projects/Alpha/Overview.md", "Projects/Beta/Overview.md"})

    def test_no_partial_file_is_left_when_the_archive_check_fails(self):
        root = vault_with_system()
        dest = Path(tempfile.mkdtemp(prefix="kbdest-"))
        with mock.patch.object(kb_backup, "check_archive", return_value=["boom"]):
            code, _, err = run("--root", str(root), "--dest", str(dest))
        self.assertEqual(code, 2)
        self.assertEqual(list(dest.iterdir()), [])


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.root = vault_with_system()
        self.archive = backup(self.root)

    def verify(self, *flags):
        code, out, err = run("--root", str(self.root), "--verify", str(self.archive), *flags)
        return code, out

    def test_untouched_vault_verifies(self):
        code, out = self.verify()
        self.assertEqual(code, 0)
        self.assertIn("-> OK", out)
        self.assertIn("0 body differs", out)

    def test_frontmatter_only_changes_pass(self):
        mapping = self.root.parent / "map.json"
        mapping.write_text(json.dumps({"Daily/2024-05-01.md": {"title": "T", "type": "daily", "status": "draft", "summary": "s"}}),
                           encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(kb_frontmatter.main(["--root", str(self.root), "--map", str(mapping), "--write"]), 0)
        code, out = self.verify()
        self.assertEqual(code, 0)
        self.assertIn("1 frontmatter only", out)

    def test_a_changed_body_fails_and_names_the_note(self):
        note = self.root / "Concepts" / "Graph Theory.md"
        note.write_text(note.read_text(encoding="utf-8") + "One more line.\n", encoding="utf-8")
        code, out = self.verify()
        self.assertEqual(code, 1)
        self.assertIn("BODY DIFFERS: Concepts/Graph Theory.md", out)
        self.assertIn("-> FAILED", out)

    def test_a_changed_frontmatter_plus_body_still_fails(self):
        note = self.root / "Concepts" / "Graph Theory.md"
        text = note.read_text(encoding="utf-8").replace("Nodes and edges.", "Nodes, edges and loops.").replace("title: Graph Theory", "title: GT")
        note.write_text(text, encoding="utf-8")
        self.assertEqual(self.verify()[0], 1)

    def test_a_changed_bom_counts_as_a_difference(self):
        note = self.root / "Daily" / "2024-05-01.md"
        note.write_bytes(kb.UTF8_BOM + note.read_bytes())
        code, out = self.verify()
        self.assertEqual(code, 1)
        self.assertIn("BODY DIFFERS: Daily/2024-05-01.md", out)

    def test_changed_line_endings_in_the_body_fail(self):
        note = self.root / "Daily" / "2024-05-01.md"
        note.write_bytes(note.read_bytes().replace(b"\n", b"\r\n"))
        self.assertEqual(self.verify()[0], 1)

    def test_missing_notes_fail_unless_allowed(self):
        (self.root / "Daily" / "2024-05-01.md").unlink()
        code, out = self.verify()
        self.assertEqual(code, 1)
        self.assertIn("missing now: Daily/2024-05-01.md", out)
        self.assertEqual(self.verify("--allow-missing")[0], 0)

    def test_new_notes_since_the_backup_are_only_counted(self):
        (self.root / "Brand New.md").write_text("# New\n", encoding="utf-8")
        code, out = self.verify()
        self.assertEqual(code, 0)
        self.assertIn("1 new since backup", out)

    def test_cloud_only_notes_are_not_opened_and_not_judged(self):
        (self.root / "Daily" / "2024-05-01.md").write_text("changed while offline copy\n", encoding="utf-8")
        with mock.patch.object(kb, "file_attributes", lambda p: 0x40000 if Path(p).name == "2024-05-01.md" else 0):
            code, out = self.verify()
        self.assertEqual(code, 0)
        self.assertIn("not checked (cloud-only): Daily/2024-05-01.md", out)

    def test_changes_to_system_files_are_informational(self):
        (self.root / "_system" / "SCHEMA.md").write_text("# Schema v2\n", encoding="utf-8")
        code, out = self.verify("--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["other_files_changed"], ["_system/SCHEMA.md"])

    def test_a_tampered_archive_is_detected(self):
        tampered = self.archive.with_name("tampered.zip")
        with zipfile.ZipFile(self.archive) as src, zipfile.ZipFile(tampered, "w") as dst:
            for item in src.infolist():
                data = src.read(item.filename)
                dst.writestr(item.filename, data + b"X" if item.filename == "Home.md" else data)
        self.archive = tampered
        code, out = self.verify()
        self.assertEqual(code, 1)
        self.assertIn("hash mismatch inside archive: Home.md", out)

    def test_a_corrupt_or_foreign_file_fails_cleanly(self):
        junk = self.archive.with_name("junk.zip")
        junk.write_bytes(b"not a zip at all")
        self.archive = junk
        code, out = self.verify()
        self.assertEqual(code, 1)
        self.assertIn("ARCHIVE PROBLEM", out)

    def test_an_archive_without_a_manifest_fails_the_archive_check(self):
        bare = self.archive.with_name("bare.zip")
        with zipfile.ZipFile(self.archive) as src, zipfile.ZipFile(bare, "w") as dst:
            for item in src.infolist():
                if item.filename != kb_backup.MANIFEST:
                    dst.writestr(item.filename, src.read(item.filename))
        self.archive = bare
        self.assertEqual(self.verify()[0], 1)

    def test_json_output_and_the_limit_option(self):
        for n in ("Home.md", "Daily/2024-05-01.md", "Concepts/Graph Theory.md"):
            path = self.root / n
            path.write_text(path.read_text(encoding="utf-8") + "extra\n", encoding="utf-8")
        data = json.loads(self.verify("--json")[1])
        self.assertEqual(len(data["body_differs"]), 3)
        self.assertFalse(data["ok"])
        code, out = self.verify("--limit", "1")
        self.assertIn("BODY DIFFERS: ... 2 more", out)

    def test_verify_never_writes(self):
        before = tree_hash(self.root)
        self.verify()
        self.assertEqual(before, tree_hash(self.root))


class CliTests(unittest.TestCase):
    def test_usage_errors(self):
        root = vault_with_system()
        self.assertEqual(run("--root", str(root), "--verify", "no-such.zip")[0], 2)
        self.assertEqual(run("--root", "no-such-folder", "--dest", "x")[0], 2)
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            run("--root", str(root))                                   # one of --dest / --verify is required
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            run("--root", str(root), "--dest", "x", "--verify", "y")

    def test_bad_config_exits_2(self):
        root = vault_with_system()
        (root / "kb.config.json").write_text('{"enums": 3}', encoding="utf-8")
        self.assertEqual(run("--root", str(root), "--dest", str(tempfile.mkdtemp(prefix="kbdest-")))[0], 2)

    def test_works_without_a_config_file_and_a_custom_config_is_stored_under_the_standard_name(self):
        root = vault_with_system()
        other = root.parent / "elsewhere.json"
        other.write_text(json.dumps({"waiting_marker": "X"}), encoding="utf-8")
        dest = Path(tempfile.mkdtemp(prefix="kbdest-"))
        self.assertEqual(run("--root", str(root), "--config", str(other), "--dest", str(dest))[0], 0)
        with zipfile.ZipFile(next(dest.glob("*.zip"))) as zf:
            self.assertEqual(json.loads(zf.read("kb.config.json").decode("utf-8")), {"waiting_marker": "X"})
        (root / "kb.config.json").unlink()
        dest2 = Path(tempfile.mkdtemp(prefix="kbdest-"))
        self.assertEqual(run("--root", str(root), "--dest", str(dest2))[0], 0)
        self.assertNotIn("kb.config.json", names(next(dest2.glob("*.zip"))))


if __name__ == "__main__":
    unittest.main()
