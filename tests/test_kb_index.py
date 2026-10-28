"""Unit tests for kb_index, run against the invented sample vault in tests/fixtures/vault."""
import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))
sys.path.insert(0, str(HERE))
import kb_common as kb  # noqa: E402
import kb_index  # noqa: E402
import kb_lint  # noqa: E402
from test_kb_common import FIXTURE, fixture_copy  # noqa: E402
from test_kb_lint import tree_hash  # noqa: E402


def run(root, *flags):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = kb_index.main(["--root", str(root), *flags])
    return code, out.getvalue(), err.getvalue()


def set_config(root, **over):
    path = Path(root) / "kb.config.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg.update(over)
    path.write_text(json.dumps(cfg), encoding="utf-8")


def note(title, **fields):
    fm = {"title": title, "type": "note", "status": "draft", "updated": "2024-01-01", "summary": "s"}
    fm.update(fields)
    lines = []
    for k, v in fm.items():
        lines.append(f"{k}:" + ("" if isinstance(v, list) else f" {v}"))
        if isinstance(v, list):
            lines.extend(f"  - {i}" for i in v)
    return "---\n" + "\n".join(lines) + "\n---\nbody\n"


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = run(FIXTURE)[1]

    def test_lists_only_notes_with_frontmatter_outside_exempt_places(self):
        self.assertIn("Notes with frontmatter: 6", self.out)
        for absent in ("Projects/Beta/Overview.md", "Daily/2024-05-01.md", "README.md", "Note Template", "Index.generated.md"):
            self.assertNotIn(absent, self.out)

    def test_groups_are_sorted_and_rows_sorted_by_path(self):
        heads = [l for l in self.out.splitlines() if l.startswith("## type: ")]
        self.assertEqual(heads, ["## type: concept", "## type: idea", "## type: index", "## type: note", "## type: project"])
        self.assertLess(self.out.index("Concepts/Café Ideas.md"), self.out.index("Concepts/Orphan Idea.md"))

    def test_wikilinks_use_vault_paths_and_escape_the_table_pipe(self):
        self.assertIn("[[Projects/Alpha/Meeting Notes 2024\\|Meeting Notes: May]]", self.out)
        self.assertIn("[[Concepts/Café Ideas\\|Café Ideas]]", self.out)

    def test_every_wikilink_in_the_generated_index_resolves_in_the_vault(self):
        root = fixture_copy()
        run(root, "--write")
        _, found = kb_lint.run(root, kb.load_config(root)[0], ("links",))
        self.assertEqual([f for f in found if f.path == "Index.generated.md"], [])

    def test_frontmatter_comes_from_config_and_updated_is_the_newest_valid_date(self):
        head = self.out.split("---\n")[1]
        self.assertIn('title: "Generated Index"', head)
        self.assertIn('type: "index"', head)
        self.assertIn("updated: 2024-05-03", head)  # newest valid date; "05/01/2024" is not a date

    def test_generated_index_passes_the_vault_schema(self):
        root = fixture_copy()
        run(root, "--write")
        cfg = kb.load_config(root)[0]
        _, found = kb_lint.run(root, cfg, (), True)
        self.assertEqual([f for f in found if f.path == "Index.generated.md"], [])

    def test_marker_line_is_present(self):
        self.assertIn(kb.GENERATED_MARKER, self.out)

    def test_missing_values_are_grouped_under_a_placeholder(self):
        root = fixture_copy()
        (root / "NoType.md").write_text("---\ntitle: Typeless\n---\n[[Home]]\n", encoding="utf-8")
        self.assertIn("## type: (missing)", run(root)[1])


class ConfigurableOutputTests(unittest.TestCase):
    def test_group_by_two_levels_and_list_values(self):
        root = fixture_copy()
        (root / "Tagged.md").write_text(note("Tagged", domain="work", tags=["a", "b"]), encoding="utf-8")
        set_config(root, index_group_by=["domain", "type"], index_columns=["title", "tags"])
        out = run(root)[1]
        self.assertIn("## domain: work", out)
        self.assertIn("### type: note", out)
        self.assertIn("| [[Tagged\\|Tagged]] | a, b |", out)
        self.assertIn("## domain: (missing)", out)

    def test_no_grouping_gives_a_single_table(self):
        root = fixture_copy()
        set_config(root, index_group_by=[])
        out = run(root)[1]
        self.assertEqual(out.count("| title |"), 1)
        self.assertNotIn("## type:", out)

    def test_redaction_hides_summary_for_configured_values(self):
        root = fixture_copy()
        (root / "Secret.md").write_text(note("Private note", domain="personal", summary="do not show this"), encoding="utf-8")
        (root / "Open.md").write_text(note("Open note", domain="work", summary="show this"), encoding="utf-8")
        set_config(root, index_redact={"domain": ["personal"]})
        out = run(root)[1]
        self.assertNotIn("do not show this", out)
        self.assertIn("(redacted)", out)
        self.assertIn("show this", out)
        self.assertIn("Private note", out)

    def test_path_link_style_has_no_wikilinks_in_rows(self):
        root = fixture_copy()
        set_config(root, index_link_style="path")
        out = run(root)[1]
        self.assertNotIn("[[", out)
        self.assertIn("| Graph Theory | `Concepts/Graph Theory.md` |", out)

    def test_custom_title_and_columns_and_pipes_in_cells(self):
        root = fixture_copy()
        (root / "Pipe.md").write_text(note("Pipe", summary="a | b"), encoding="utf-8")
        set_config(root, index_title="All notes", index_columns=["title", "summary", "status"])
        out = run(root)[1]
        self.assertIn("# All notes", out)
        self.assertIn("| title | summary | status |", out)
        self.assertIn("a \\| b", out)

    def test_title_falls_back_to_the_file_name(self):
        root = fixture_copy()
        (root / "Untitled Thing.md").write_text("---\ntype: note\n---\n[[Home]]\n", encoding="utf-8")
        self.assertIn("[[Untitled Thing\\|Untitled Thing]]", run(root)[1])

    def test_no_frontmatter_block_when_nothing_is_configured_and_no_dates(self):
        root = fixture_copy()
        for p in list(root.rglob("*.md")):
            p.unlink()
        (root / "A.md").write_text("---\ntype: note\n---\n", encoding="utf-8")
        set_config(root, index_frontmatter={})
        out = run(root)[1]
        self.assertTrue(out.startswith(kb.GENERATED_MARKER))

    def test_exempt_names_and_dirs_come_from_the_config(self):
        root = fixture_copy()
        set_config(root, exempt_dirs=[".obsidian/"], exempt_names=["README.md", "Home.md"])
        out = run(root)[1]
        self.assertNotIn("`Home.md`", out)
        self.assertIn("Note Template", out)  # templates/ is no longer exempt, so it is listed


class ModeTests(unittest.TestCase):
    def test_default_is_read_only(self):
        root = fixture_copy()
        before = tree_hash(root)
        code, out, _ = run(root)
        self.assertEqual(code, 0)
        self.assertTrue(out.strip())
        self.assertEqual(before, tree_hash(root))
        self.assertFalse((root / "Index.generated.md").exists())

    def test_write_creates_only_the_configured_file_and_is_idempotent(self):
        root = fixture_copy()
        before = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
        self.assertEqual(run(root, "--write")[1].strip(), "wrote Index.generated.md")
        after = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
        self.assertEqual(after - before, {"Index.generated.md"})
        first = (root / "Index.generated.md").read_bytes()
        run(root, "--write")
        self.assertEqual(first, (root / "Index.generated.md").read_bytes())
        self.assertNotIn(b"\r\n", first)

    def test_index_output_can_live_in_a_subfolder(self):
        root = fixture_copy()
        set_config(root, index_output="Meta/Notes Index.md")
        run(root, "--write")
        self.assertTrue((root / "Meta" / "Notes Index.md").is_file())
        self.assertEqual(run(root, "--check")[0], 0)

    def test_refuses_to_overwrite_a_note_it_did_not_generate(self):
        root = fixture_copy()
        (root / "Index.generated.md").write_text("# My own page\n", encoding="utf-8")
        code, _, err = run(root, "--write")
        self.assertEqual(code, 2)
        self.assertIn("refusing to overwrite", err)
        self.assertEqual((root / "Index.generated.md").read_text(encoding="utf-8"), "# My own page\n")

    def test_check_detects_missing_stale_and_fresh(self):
        root = fixture_copy()
        self.assertEqual(run(root, "--check")[0], 1)
        run(root, "--write")
        self.assertEqual(run(root, "--check")[0], 0)
        (root / "Concepts" / "Café Ideas.md").write_text(note("Changed"), encoding="utf-8")
        self.assertEqual(run(root, "--check")[0], 1)

    def test_write_and_check_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            kb_index.main(["--root", str(FIXTURE), "--write", "--check"])

    def test_bad_folder_and_bad_config_exit_2(self):
        self.assertEqual(run(FIXTURE / "missing-folder")[0], 2)
        root = fixture_copy()
        set_config(root, index_output="../escape.md")
        code, _, err = run(root, "--write")
        self.assertEqual(code, 2)
        self.assertIn("config error", err)
        self.assertFalse((root.parent / "escape.md").exists())

    def test_dot_obsidian_notes_are_never_indexed(self):
        root = fixture_copy()
        (root / ".obsidian" / "x.md").write_text(note("Hidden config note"), encoding="utf-8")
        self.assertNotIn("Hidden config note", run(root)[1])


if __name__ == "__main__":
    unittest.main()
