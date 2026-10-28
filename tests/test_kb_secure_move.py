"""Unit tests for kb_secure_move. Every file lives in a temporary folder; the system trash is never used (--trash-dir)."""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))
import kb_common as kb  # noqa: E402
import kb_secure_move  # noqa: E402

SENTINEL = "SENTINEL-SECRET-CONTENT-12345"


def run(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = kb_secure_move.main(list(args))
    return code, out.getvalue(), err.getvalue()


class Env:
    """A vault, a destination and a trash folder, all outside each other."""

    def __init__(self):
        base = Path(tempfile.mkdtemp(prefix="kbsm-")).resolve()
        self.vault, self.dest, self.trash = base / "vault", base / "safe", base / "trash"
        (self.vault / "settings").mkdir(parents=True)
        self.secret = self.vault / "settings" / "identity.json"
        self.secret.write_text('{"k": "' + SENTINEL + '"}', encoding="utf-8")
        self.other = self.vault / "keys.txt"
        self.other.write_text("second " + SENTINEL, encoding="utf-8")
        self.log = self.dest / kb_secure_move.LOG_NAME

    def args(self, *files, apply=True, extra=()):
        a = ["--root", str(self.vault), "--dest", str(self.dest), "--trash-dir", str(self.trash)]
        for f in files:
            a += ["--file", f]
        if apply:
            a += ["--apply", "--approved-by", "owner"]
        return a + list(extra)


class DryRunTests(unittest.TestCase):
    def test_nothing_happens_without_apply(self):
        env = Env()
        before = env.secret.read_bytes()
        code, out, err = run(*env.args("settings/identity.json", apply=False))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("DRY RUN", out)
        self.assertIn("would move: settings/identity.json", out)
        self.assertEqual(env.secret.read_bytes(), before)
        self.assertFalse(env.dest.exists() or env.trash.exists())

    def test_the_content_is_never_printed(self):
        env = Env()
        for flags in ((), ("--json",)):
            self.assertNotIn(SENTINEL, run(*env.args("settings/identity.json", apply=False, extra=flags))[1])
            self.assertNotIn(SENTINEL, run(*env.args("settings/identity.json", extra=flags))[1] if not env.dest.exists() else "")

    def test_a_dry_run_reports_the_same_problems_as_the_real_run(self):
        env = Env()
        code, out, _ = run(*env.args("nowhere.txt", apply=False))
        self.assertEqual(code, 1)
        self.assertIn("NOT moved: nowhere.txt", out)


class MoveTests(unittest.TestCase):
    def test_copy_verify_trash_and_log(self):
        env = Env()
        original = env.secret.read_bytes()
        with mock.patch.object(kb_secure_move, "now_text", return_value="2026-10-29 01:02"):
            code, out, err = run(*env.args("settings/identity.json", extra=("--reason", "client token | public")))
        self.assertEqual((code, err), (0, ""))
        copy = env.dest / "settings" / "identity.json"
        self.assertEqual(copy.read_bytes(), original)
        self.assertFalse(env.secret.exists())
        self.assertEqual((env.trash / "identity.json").read_bytes(), original)
        log = env.log.read_text(encoding="utf-8")
        self.assertIn("| 2026-10-29 01:02 | settings/identity.json |", log)
        self.assertIn(str(copy), log)
        self.assertIn(kb.sha256(original), log)
        self.assertIn("| owner | client token / public | moved; original in the trash |", log)
        self.assertNotIn(SENTINEL, log + out)

    def test_the_log_is_appended_to_and_has_one_header(self):
        env = Env()
        run(*env.args("settings/identity.json"))
        run(*env.args("keys.txt"))
        log = env.log.read_text(encoding="utf-8")
        self.assertEqual(log.count("| When |"), 1)
        self.assertEqual(sum(1 for l in log.splitlines() if l.startswith("| 20")), 2)

    def test_several_files_and_one_bad_one(self):
        env = Env()
        code, out, _ = run(*env.args("settings/identity.json", "missing.txt", "keys.txt"))
        self.assertEqual(code, 1)
        self.assertFalse(env.secret.exists() or env.other.exists())
        self.assertIn("NOT moved: missing.txt", out)
        self.assertEqual(sum(1 for l in env.log.read_text(encoding="utf-8").splitlines() if l.startswith("| 20")), 2)

    def test_a_custom_log_path_and_json_output(self):
        env = Env()
        custom = env.dest.parent / "records" / "moves.md"
        code, out, _ = run(*env.args("keys.txt", extra=("--log", str(custom), "--json")))
        data = json.loads(out)
        self.assertEqual((code, data["applied"], data["logged_rows"], data["files"][0]["status"]), (0, True, 1, "moved"))
        self.assertTrue(custom.is_file() and not env.log.exists())

    def test_a_copy_that_does_not_match_is_removed_and_the_original_stays(self):
        env = Env()
        original = env.secret.read_bytes()

        def bad_copy(src, dst):
            Path(dst).write_bytes(b"corrupted")

        with mock.patch.object(shutil, "copy2", bad_copy):
            code, out, _ = run(*env.args("settings/identity.json"))
        self.assertEqual(code, 1)
        self.assertIn("SHA-256 differs", out)
        self.assertEqual(env.secret.read_bytes(), original)
        self.assertFalse((env.dest / "settings" / "identity.json").exists())
        self.assertFalse(env.log.exists())

    def test_when_the_trash_refuses_the_copy_stays_and_the_log_says_so(self):
        env = Env()
        with mock.patch.object(kb_secure_move, "move_to_trash", side_effect=OSError("denied")):
            code, out, _ = run(*env.args("settings/identity.json"))
        self.assertEqual(code, 1)
        self.assertIn("COPIED ONLY", out)
        self.assertTrue(env.secret.exists() and (env.dest / "settings" / "identity.json").exists())
        self.assertIn("COPIED ONLY: original still in the vault", env.log.read_text(encoding="utf-8"))

    def test_a_log_that_cannot_be_written_is_reported_with_what_to_record_by_hand(self):
        env = Env()
        blocker = env.dest.parent / "blocker"
        blocker.write_text("x", encoding="utf-8")
        code, out, _ = run(*env.args("keys.txt", extra=("--log", str(blocker / "log.md"))))
        self.assertEqual(code, 1)
        self.assertIn("the log could not be written", out)
        self.assertIn("keys.txt", out)
        self.assertFalse(env.other.exists())
        self.assertNotIn(SENTINEL, out)


class RefusalTests(unittest.TestCase):
    def refused(self, env, rel, fragment):
        code, out, _ = run(*env.args(rel))
        self.assertEqual(code, 1, rel)
        self.assertIn(fragment, out, rel)

    def test_paths_outside_the_vault_directories_and_missing_files(self):
        env = Env()
        outside = env.vault.parent / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        self.refused(env, "../outside.txt", "inside the vault")
        self.refused(env, "settings", "not a file")
        self.refused(env, "", "inside the vault")
        self.refused(env, "nope.txt", "not a file")
        self.assertTrue(outside.exists() and env.secret.exists())

    def test_an_absolute_path_works_only_inside_the_vault(self):
        env = Env()
        outside = env.vault.parent / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        self.refused(env, str(outside), "inside the vault")
        self.assertTrue(outside.exists())
        code, out, _ = run(*env.args(str(env.secret)))
        self.assertEqual(code, 0)
        self.assertTrue((env.dest / "settings" / "identity.json").is_file())
        self.assertFalse(env.secret.exists())

    def test_symbolic_links_are_not_followed(self):
        env = Env()
        link = env.vault / "link.txt"
        try:
            os.symlink(env.secret, link)
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links not available")
        self.refused(env, "link.txt", "symbolic link")

    def test_an_existing_destination_file_is_never_overwritten(self):
        env = Env()
        (env.dest / "settings").mkdir(parents=True)
        (env.dest / "settings" / "identity.json").write_text("old", encoding="utf-8")
        self.refused(env, "settings/identity.json", "nothing is overwritten")
        self.assertEqual((env.dest / "settings" / "identity.json").read_text(encoding="utf-8"), "old")
        self.assertTrue(env.secret.exists())

    def test_an_online_only_file_is_not_opened(self):
        env = Env()
        with mock.patch.object(kb, "is_cloud_only", return_value=True), mock.patch.object(Path, "read_bytes", side_effect=AssertionError("opened")):
            code, out, _ = run(*env.args("settings/identity.json"))
        self.assertEqual(code, 1)
        self.assertIn("online-only", out)

    def test_bad_destinations_and_missing_approval_are_usage_errors(self):
        env = Env()
        for dest in (env.vault / "inside", env.vault, env.vault.parent):
            code, _, err = run("--root", str(env.vault), "--dest", str(dest), "--file", "keys.txt")
            self.assertEqual(code, 2, dest)
            self.assertIn("outside the vault", err)
        synced = env.dest.parent / "OneDrive - Company" / "safe"
        code, _, err = run("--root", str(env.vault), "--dest", str(synced), "--file", "keys.txt")
        self.assertEqual((code, "synced folder" in err), (2, True))
        code, out, _ = run("--root", str(env.vault), "--dest", str(synced), "--file", "keys.txt", "--allow-synced-dest")
        self.assertEqual(code, 0)
        code, _, err = run("--root", str(env.vault), "--dest", str(env.dest), "--file", "keys.txt", "--apply")
        self.assertEqual((code, "--approved-by" in err), (2, True))
        code, _, err = run("--root", str(env.vault), "--dest", str(env.dest), "--file", "keys.txt", "--apply", "--approved-by", "  ")
        self.assertEqual(code, 2)
        code, _, err = run("--root", str(env.vault), "--dest", str(env.dest), "--trash-dir", str(env.vault / "t"), "--file", "keys.txt")
        self.assertEqual((code, "--trash-dir" in err), (2, True))
        self.assertTrue(env.other.exists())

    def test_a_missing_vault_or_no_file_is_a_usage_error(self):
        env = Env()
        self.assertEqual(run("--root", str(env.vault / "nowhere"), "--dest", str(env.dest), "--file", "a")[0], 2)
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                kb_secure_move.main(["--root", str(env.vault), "--dest", str(env.dest)])


class TrashTests(unittest.TestCase):
    def test_trash_dir_keeps_both_when_names_collide(self):
        env = Env()
        env.trash.mkdir()
        (env.trash / "keys.txt").write_text("older", encoding="utf-8")
        run(*env.args("keys.txt"))
        self.assertEqual((env.trash / "keys.txt").read_text(encoding="utf-8"), "older")
        self.assertIn(SENTINEL, (env.trash / "keys.1.txt").read_text(encoding="utf-8"))

    def test_the_linux_trash_writes_the_info_file(self):
        base = Path(tempfile.mkdtemp(prefix="kbsm-xdg-"))
        victim = base / "v" / "a.txt"
        victim.parent.mkdir()
        victim.write_text("x", encoding="utf-8")
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(base / "data")}):
            kb_secure_move._freedesktop_trash(victim)
        self.assertFalse(victim.exists())
        self.assertTrue((base / "data" / "Trash" / "files" / "a.txt").is_file())
        self.assertIn("[Trash Info]", (base / "data" / "Trash" / "info" / "a.txt.trashinfo").read_text(encoding="utf-8"))


class DocsTests(unittest.TestCase):
    def test_version_and_module_help(self):
        out = io.StringIO()
        with self.assertRaises(SystemExit) as ctx, redirect_stdout(out):
            kb_secure_move.main(["--version"])
        self.assertEqual((ctx.exception.code, out.getvalue().strip()), (0, f"kit {kb.KIT_VERSION}"))
        for phrase in ("does nothing unless you pass --apply", "SHA-256", "never prints or logs the content"):
            self.assertIn(phrase, kb_secure_move.__doc__)

    def test_the_file_is_pure_standard_library(self):
        text = (HERE.parent / "tools" / "kb_secure_move.py").read_text(encoding="utf-8")
        for module in ("requests", "send2trash", "urllib", "socket", "subprocess"):
            self.assertNotIn("import " + module, text)


if __name__ == "__main__":
    unittest.main()
