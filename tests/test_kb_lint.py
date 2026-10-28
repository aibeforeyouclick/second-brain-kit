"""Unit tests for kb_lint, run against the invented sample vault in tests/fixtures/vault."""
import hashlib
import io
import json
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))
sys.path.insert(0, str(HERE))
import kb_common as kb  # noqa: E402
import kb_lint  # noqa: E402
from test_kb_common import FIXTURE, fixture_copy  # noqa: E402

# Secret-looking strings are assembled at runtime so no file in the kit contains one.
FAKE_KEY = "sk-" + "ant-" + "B" * 30
FAKE_PEM = "-----BEGIN " + "PRIVATE KEY-----"
FAKE_ASSIGN = "pass" + "word: " + "Qx7" * 8


def lint(root, checks=kb_lint.CHECKS, schema=False, **cfg_over):
    cfg, _ = kb.load_config(root)
    cfg.update(cfg_over)
    vault, findings = kb_lint.run(root, cfg, checks, schema)
    return vault, findings


def messages(findings, check):
    return [f"{f.path}|{f.severity}|{f.message}" for f in findings if f.check == check]


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()


class LinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vault, cls.findings = lint(FIXTURE, ("links",))
        cls.msgs = messages(cls.findings, "links")

    def joined(self):
        return "\n".join(self.msgs)

    def test_unresolved_wikilink_and_markdown_link_are_errors(self):
        self.assertIn("Home.md|error|unresolved wiki link -> Ghost Note", self.msgs)
        self.assertIn("Home.md|error|unresolved md link -> Concepts/Nope.md", self.msgs)

    def test_resolution_by_file_name_embeds_paths_spaces_and_unicode_pass(self):
        for fine in ("Overview|", "Alpha", "Meeting Notes 2024", "2024-05-01", "diagram.png", "Spec Sheet.pdf",
                     "Café Ideas", "Graph%20Theory", "Inline Code", "In Fence", "Hidden Comment", "Template"):
            self.assertNotIn("unresolved wiki link -> " + fine.rstrip("|"), self.joined(), fine)
        self.assertEqual(sum("unresolved" in m for m in self.msgs), 2)

    def test_table_escaped_pipe_link_resolves(self):
        self.assertNotIn("Graph Theory\\", self.joined())

    def test_ignore_links_hides_matching_unresolved_targets_and_counts_them(self):
        self.assertNotIn("Not Yet Written", self.joined())
        self.assertEqual(self.vault.links_ignored, 1)

    def test_ignore_links_supports_globs_and_case(self):
        _, found = lint(FIXTURE, ("links",), ignore_links=["not yet *", "ghost*", "*nope.md"])
        self.assertEqual([f for f in found if f.severity == "error" and "[[GT]]" not in f.message], [])

    def test_alias_link_is_reported_with_a_fix_hint_unless_resolve_aliases_is_on(self):
        alias = [m for m in self.msgs if "[[GT]]" in m]
        self.assertEqual(len(alias), 1)
        self.assertIn("write [[Concepts/Graph Theory|GT]]", alias[0])
        _, found = lint(FIXTURE, ("links",), resolve_aliases=True)
        self.assertFalse([f for f in found if "[[GT]]" in f.message])

    def test_ambiguous_name_is_a_warning_and_nearest_folder_wins(self):
        amb = [m for m in self.msgs if "ambiguous" in m]
        self.assertEqual(len(amb), 1)
        self.assertTrue(amb[0].startswith("Home.md|warning|"))
        # Alpha/Overview.md and Alpha/Meeting Notes 2024.md share a folder: no warning for [[Meeting Notes 2024]]
        self.assertNotIn("Meeting Notes 2024]] matches", self.joined())

    def test_missing_heading_is_a_warning_present_heading_is_fine(self):
        self.assertIn("Home.md|warning|heading not found: [[Graph Theory#Missing Heading]]", self.msgs)
        self.assertNotIn("Definitions", self.joined())
        _, found = lint(FIXTURE, ("links",), check_headings=False)
        self.assertFalse([f for f in found if "heading" in f.message])

    def test_notes_in_exempt_dirs_do_not_report_placeholder_links(self):
        self.assertNotIn("Template", self.joined())
        self.assertNotIn("templates/", self.joined())


class OrphanTests(unittest.TestCase):
    def test_only_the_unlinked_note_is_an_orphan(self):
        _, found = lint(FIXTURE, ("orphans",), orphans_scope=["*"])
        self.assertEqual([f.path for f in found], ["Concepts/Orphan Idea.md"])

    def test_embeds_count_as_inbound_links(self):
        root = fixture_copy()
        (root / "Solo.md").write_text("# Solo\n", encoding="utf-8")
        (root / "Home.md").write_text((root / "Home.md").read_text(encoding="utf-8") + "\n![[Solo]]\n", encoding="utf-8")
        _, found = lint(root, ("orphans",), orphans_scope=["*"])
        self.assertNotIn("Solo.md", [f.path for f in found])

    def test_self_links_do_not_rescue_a_note(self):
        root = fixture_copy()
        (root / "Loop.md").write_text("[[Loop]]\n", encoding="utf-8")
        _, found = lint(root, ("orphans",), orphans_scope=["*"])
        self.assertIn("Loop.md", [f.path for f in found])

    def test_exempt_notes_are_never_orphans(self):
        _, found = lint(FIXTURE, ("orphans",), orphans_scope=["*"])
        self.assertFalse({"README.md", "templates/Note Template.md"} & {f.path for f in found})


class SchemaAndRequiredTests(unittest.TestCase):
    def test_required_mode_without_schema_flag(self):
        _, found = lint(FIXTURE, ("required",))
        self.assertEqual(messages(found, "required"),
                         ["Projects/Beta/Overview.md|error|missing frontmatter (note is under required_paths)"])
        # the notes in scope are also field-validated even though --schema is off
        self.assertEqual(len(messages(found, "schema")), 2)
        self.assertTrue(all(m.startswith("Projects/Alpha/Meeting Notes 2024.md") for m in messages(found, "schema")))

    def test_schema_flag_validates_everything_with_frontmatter_without_double_reports(self):
        _, found = lint(FIXTURE, ("required",), schema=True)
        schema = messages(found, "schema")
        self.assertEqual(len(schema), 3)
        self.assertIn("Concepts/Orphan Idea.md|error|missing required field: updated", schema)
        self.assertIn("Projects/Alpha/Meeting Notes 2024.md|error|bad value for status: 'wip' (allowed: active, done, draft)", schema)
        self.assertIn("Projects/Alpha/Meeting Notes 2024.md|error|updated must be a real date, YYYY-MM-DD", schema)

    def test_note_without_frontmatter_outside_scope_is_only_a_warning(self):
        _, found = lint(FIXTURE, ("required",), schema=True)
        self.assertEqual(messages(found, "frontmatter"),
                         ["Daily/2024-05-01.md|warning|no frontmatter (not under required_paths)"])

    def test_empty_value_counts_as_missing_and_lists_are_checked_per_item(self):
        root = fixture_copy()
        (root / "Multi.md").write_text("---\ntitle: M\ntype: [note, bogus]\nstatus: draft\nupdated: 2024-01-01\n---\n[[Home]]\n",
                                       encoding="utf-8")
        _, found = lint(root, (), schema=True)
        self.assertIn("Multi.md|error|bad value for type: 'bogus' (allowed: concept, daily, idea, index, note, project)",
                      messages(found, "schema"))
        self.assertIn("Concepts/Orphan Idea.md|error|missing required field: updated", messages(found, "schema"))

    def test_custom_enums_required_fields_and_date_fields_come_from_the_config(self):
        _, found = lint(FIXTURE, (), schema=True, enums={}, required_fields=["title"], date_fields=[])
        self.assertEqual(messages(found, "schema"), [])

    def test_date_fields_accept_datetime_and_reject_impossible_dates(self):
        root = fixture_copy()
        (root / "Dates.md").write_text("---\ntitle: D\ntype: note\nstatus: draft\nupdated: 2024-02-30\n---\n[[Home]]\n",
                                       encoding="utf-8")
        (root / "Dates2.md").write_text("---\ntitle: D\ntype: note\nstatus: draft\nupdated: 2024-02-29T08:00\n---\n[[Home]]\n",
                                        encoding="utf-8")
        _, found = lint(root, (), schema=True)
        flagged = {f.path for f in found if "real date" in f.message}
        self.assertIn("Dates.md", flagged)
        self.assertNotIn("Dates2.md", flagged)

    def test_unused_required_path_entry_is_a_warning(self):
        _, found = lint(FIXTURE, ("required",), required_paths=["Projects/", "Nowhere/"])
        self.assertIn(f"{kb.CONFIG_NAME}|warning|required_paths entry matches no note: Nowhere/", messages(found, "required"))

    def test_obsidian_properties_are_parsed_for_validation(self):
        root = fixture_copy()
        (root / "Props.md").write_text(
            "---\ntitle: P\ntype: note\nstatus: draft\nupdated: 2024-01-01\ntags:\n  - a\n  - b\naliases: [x, y]\n"
            "cssclasses: []\nlinks:\n  - \"[[Home]]\"\n---\nbody\n", encoding="utf-8")
        _, found = lint(root, ("links", "orphans"), schema=True, orphans_scope=["*"])
        self.assertFalse([f for f in found if f.path == "Props.md" and f.check in ("schema", "links")])
        self.assertIn("Props.md", [f.path for f in found if f.check == "orphans"])


class SecretTests(unittest.TestCase):
    def test_detects_patterns_never_prints_values_and_skips_dot_folders(self):
        root = fixture_copy()
        (root / "Keys.md").write_text(f"key {FAKE_KEY}\n{FAKE_PEM}\n{FAKE_ASSIGN}\n", encoding="utf-8")
        (root / ".obsidian" / "plugins.md").write_text(f"{FAKE_KEY}\n[[Nowhere]]\n", encoding="utf-8")
        vault, found = lint(root, ("secrets",))
        hits = [f for f in found if f.check == "secrets"]
        self.assertEqual({(f.path, f.line) for f in hits}, {("Keys.md", 1), ("Keys.md", 2), ("Keys.md", 3)})
        blob = json.dumps([f.__dict__ for f in hits])
        for secret in (FAKE_KEY, "B" * 20, "Qx7Qx7"):
            self.assertNotIn(secret, blob)

    def test_placeholders_are_ignored_and_scan_can_be_switched_off(self):
        root = fixture_copy()
        (root / "Env.md").write_text("api_key = your_api_key_goes_here_12345\ntoken: <your token>\n", encoding="utf-8")
        self.assertEqual([f for f in lint(root, ("secrets",))[1]], [])
        (root / "Keys.md").write_text(FAKE_KEY + "\n", encoding="utf-8")
        self.assertEqual(lint(root, ("secrets",), secret_scan=False)[1], [])


class ObsidianFolderTests(unittest.TestCase):
    def test_dot_obsidian_notes_are_not_linted_or_listed(self):
        root = fixture_copy()
        (root / ".obsidian" / "stray.md").write_text("[[Broken Link]]\n", encoding="utf-8")
        vault, found = lint(root, kb_lint.CHECKS, schema=True)
        self.assertFalse([f for f in found if ".obsidian" in f.path])
        self.assertNotIn(".obsidian/stray.md", vault.notes)

    def test_nothing_is_ever_written(self):
        root = fixture_copy()
        before = tree_hash(root)
        with redirect_stdout(io.StringIO()):
            kb_lint.main(["--root", str(root), "--schema"])
            kb_lint.main(["--root", str(root), "--stats", "--json"])
        self.assertEqual(before, tree_hash(root))


class StatsAndCliTests(unittest.TestCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = kb_lint.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_stats_numbers_on_the_sample_vault(self):
        code, out, _ = self.run_cli("--root", str(FIXTURE), "--schema", "--stats", "--json")
        stats = json.loads(out)["stats"]
        self.assertEqual((stats["notes"], stats["notes_with_frontmatter"], stats["schema_applicable"],
                          stats["applicable_with_frontmatter"], stats["applicable_frontmatter_pct"]), (10, 7, 8, 6, 75.0))
        self.assertEqual((stats["required_pages"], stats["required_pages_with_frontmatter"], stats["links_ignored"]), (4, 3, 1))
        self.assertEqual(stats["waiting_marker_lines"], 2)
        self.assertEqual(stats["waiting_marker_by_note"],
                         {"Projects/Alpha/Meeting Notes 2024.md": 1, "Projects/Alpha/Overview.md": 1})

    def test_waiting_marker_is_configurable_and_empty_disables_it(self):
        root = fixture_copy()
        (root / "kb.config.json").write_text(json.dumps({"waiting_marker": "called the vendor"}), encoding="utf-8")
        stats = json.loads(self.run_cli("--root", str(root), "--stats", "--json")[1])["stats"]
        self.assertEqual(stats["waiting_marker_by_note"], {"Daily/2024-05-01.md": 1})
        (root / "kb.config.json").write_text(json.dumps({"waiting_marker": ""}), encoding="utf-8")
        stats = json.loads(self.run_cli("--root", str(root), "--stats", "--json")[1])["stats"]
        self.assertEqual(stats["waiting_marker_lines"], 0)

    def test_exit_codes(self):
        self.assertEqual(self.run_cli("--root", str(FIXTURE))[0], 1)  # errors present
        clean = fixture_copy()
        for p in list(clean.rglob("*.md")):
            if p.name not in ("README.md",):
                p.unlink()
        (clean / "kb.config.json").write_text("{}", encoding="utf-8")
        (clean / "A.md").write_text("[[B]]\n", encoding="utf-8")
        (clean / "B.md").write_text("[[A]]\n", encoding="utf-8")
        self.assertEqual(self.run_cli("--root", str(clean))[0], 0)
        (clean / "C.md").write_text("lonely\n", encoding="utf-8")
        self.assertEqual(self.run_cli("--root", str(clean))[0], 0)  # orphan = warning
        self.assertEqual(self.run_cli("--root", str(clean), "--strict")[0], 1)

    def test_usage_and_config_errors_exit_2_with_a_message(self):
        code, _, err = self.run_cli("--root", str(FIXTURE), "--checks", "nope")
        self.assertEqual(code, 2)
        self.assertIn("unknown check", err)
        self.assertEqual(self.run_cli("--root", str(FIXTURE / "missing-folder"))[0], 2)
        bad = fixture_copy()
        (bad / "kb.config.json").write_text('{"enums": 3}', encoding="utf-8")
        code, _, err = self.run_cli("--root", str(bad))
        self.assertEqual(code, 2)
        self.assertIn("config error", err)

    def test_runs_with_built_in_defaults_when_there_is_no_config_file(self):
        root = fixture_copy()
        (root / "kb.config.json").unlink()
        code, out, err = self.run_cli("--root", str(root), "--schema")
        self.assertIn("using built-in defaults", err)
        self.assertNotIn("missing required field", out)  # nothing configured to require
        self.assertNotIn("bad value for", out)           # no enums configured
        self.assertIn("updated must be a real date", out)  # date_fields default to ["updated"]

    def test_explicit_config_path_and_text_output_limit(self):
        elsewhere = fixture_copy().parent / "other.json"
        elsewhere.write_text(json.dumps({"required_fields": ["owner"]}), encoding="utf-8")
        code, out, _ = self.run_cli("--root", str(FIXTURE), "--config", str(elsewhere), "--schema", "--checks", "schema",
                                    "--limit", "2")
        self.assertIn("... ", out)
        self.assertIn("kb_lint: 10 notes", out)


class ThirdPartyExemptionTests(unittest.TestCase):
    def make(self):
        root = fixture_copy()
        for rel in ("vendor/lib/docs/Guide.md", "vendor/lib/README.md", "app/venv/pkg/notes.md", "docs/vendor/x/Api.md"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("# Third party\n[[Missing Thing]] and some text\n", encoding="utf-8")
        return root

    def test_third_party_folders_are_noise_by_default_and_exempt_when_configured(self):
        root = self.make()
        _, noisy = lint(root, ("links", "orphans"), schema=True, orphans_scope=["*"], exempt_dirs=[".obsidian/", "templates/"])
        self.assertTrue([f for f in noisy if f.path.startswith("vendor/")])
        _, quiet = lint(root, ("links", "orphans"), schema=True, orphans_scope=["*"],
                        exempt_dirs=["templates/", "vendor/", "venv/"])
        self.assertEqual([f for f in quiet if "vendor/" in f.path or "venv/" in f.path and f.check != "links"], [])

    def test_a_single_folder_name_matches_at_any_depth_a_longer_path_only_from_the_root(self):
        root = self.make()
        vault, _ = lint(root, ())
        self.assertTrue(vault.is_exempt("app/venv/pkg/notes.md"))      # default exempt_dirs include venv/
        self.assertFalse(vault.is_exempt("docs/vendor/x/Api.md"))
        vault.cfg["exempt_dirs"] = ["vendor/"]
        self.assertTrue(vault.is_exempt("docs/vendor/x/Api.md"))       # at any depth
        vault.cfg["exempt_dirs"] = ["docs/vendor/"]
        self.assertTrue(vault.is_exempt("docs/vendor/x/Api.md"))
        self.assertFalse(vault.is_exempt("vendor/lib/docs/Guide.md"))  # from the root only

    def test_exempt_notes_are_counted_separately_in_stats(self):
        root = self.make()
        vault, found = lint(root, (), exempt_dirs=["vendor/", "templates/"])
        stats = kb_lint.compute_stats(vault, found)
        self.assertEqual(stats["notes"], 14)
        self.assertEqual(stats["schema_applicable"], 9)  # the 8 original notes plus the venv note


class FolderBreakdownTests(unittest.TestCase):
    def rows(self, root, depth, schema=True):
        vault, found = lint(root, kb_lint.CHECKS, schema=schema)
        return {r["folder"]: r for r in kb_lint.folder_breakdown(vault, found, depth)}

    def test_level_one_counts_on_the_sample_vault(self):
        rows = self.rows(FIXTURE, 1)
        self.assertEqual(sorted(rows), ["(root)", "Concepts", "Daily", "Projects", "templates"])
        self.assertEqual((rows["(root)"]["notes"], rows["(root)"]["exempt"], rows["(root)"]["broken_links"]), (2, 1, 3))
        self.assertEqual((rows["Projects"]["applicable"], rows["Projects"]["frontmatter_pct"]), (3, 66.7))
        self.assertEqual((rows["Daily"]["frontmatter_pct"], rows["templates"]["exempt"]), (0.0, 1))

    def test_level_two_adds_nested_folders_and_parents_include_their_children(self):
        rows = self.rows(FIXTURE, 2)
        self.assertEqual((rows["Projects/Alpha"]["notes"], rows["Projects/Beta"]["notes"]), (2, 1))
        self.assertEqual(rows["Projects"]["notes"], rows["Projects/Alpha"]["notes"] + rows["Projects/Beta"]["notes"])
        self.assertNotIn("Projects/Alpha/Deeper", rows)

    def test_findings_are_attributed_to_every_folder_level_above_them(self):
        root = fixture_copy()
        (root / "Concepts" / "Deep").mkdir()
        (root / "Concepts" / "Deep" / "Lonely.md").write_text("# Lonely\n", encoding="utf-8")
        (root / "Concepts" / "Deep" / "Keys.md").write_text(f"{FAKE_KEY}\n```\n{FAKE_KEY}\n```\n[[Nowhere]]\n", encoding="utf-8")
        vault, found = lint(root, kb_lint.CHECKS, orphans_scope=["*"])
        rows = {r["folder"]: r for r in kb_lint.folder_breakdown(vault, found, 2)}
        for folder in ("Concepts", "Concepts/Deep"):
            self.assertEqual((rows[folder]["secrets"], rows[folder]["secret_examples"]), (1, 1), folder)
        self.assertEqual(rows["Concepts/Deep"]["orphans"], 2)
        self.assertEqual(rows["Concepts/Deep"]["broken_links"], 1)

    def test_cli_prints_a_table_and_json_carries_the_rows(self):
        code, out, _ = StatsAndCliTests.run_cli(None, "--root", str(FIXTURE), "--by-folder", "--depth", "1")
        self.assertIn("folder", out)
        self.assertIn("Projects", out)
        self.assertIn("notes: 10", out)
        code, out, _ = StatsAndCliTests.run_cli(None, "--root", str(FIXTURE), "--by-folder", "--json")
        data = json.loads(out)["stats"]["by_folder"]
        self.assertIn("Projects/Alpha", [r["folder"] for r in data])

    def test_depth_is_validated(self):
        for bad in ("0", "5"):
            self.assertEqual(StatsAndCliTests.run_cli(None, "--root", str(FIXTURE), "--by-folder", "--depth", bad)[0], 2)

    def test_notes_at_the_root_only(self):
        root = fixture_copy()
        for p in list(root.iterdir()):
            if p.is_dir() and not p.name.startswith("."):
                import shutil
                shutil.rmtree(p)
        vault, found = lint(root, ())
        self.assertEqual([r["folder"] for r in kb_lint.folder_breakdown(vault, found, 2)], ["(root)"])


class SecretScopeTests(unittest.TestCase):
    def test_secret_inside_a_code_block_is_a_separate_warning_outside_it_stays_an_error(self):
        root = fixture_copy()
        (root / "Docs.md").write_text(f"Real: {FAKE_KEY}\n\n```bash\nexport KEY={FAKE_KEY}\n```\n\n~~~\n{FAKE_ASSIGN}\n~~~\n", encoding="utf-8")
        _, found = lint(root, ("secrets",))
        got = sorted((f.check, f.severity, f.line) for f in found)
        self.assertEqual(got, [("secret-in-example", "warning", 4), ("secret-in-example", "warning", 8), ("secrets", "error", 1)])
        self.assertTrue(all("value not shown" in f.message for f in found))
        self.assertNotIn(FAKE_KEY, json.dumps([f.__dict__ for f in found]))

    def test_one_line_gives_one_finding_even_when_a_specific_and_a_generic_pattern_both_match(self):
        root = fixture_copy()
        (root / "Both.md").write_text("TOKEN=" + FAKE_KEY + chr(10), encoding="utf-8")
        _, found = lint(root, ("secrets",))
        self.assertEqual(len(found), 1)

    def test_examples_do_not_fail_the_exit_code_unless_strict(self):
        root = fixture_copy()
        for p in list(root.rglob("*.md")):
            p.unlink()
        (root / "Only.md").write_text(f"```\n{FAKE_KEY}\n```\n", encoding="utf-8")
        (root / "kb.config.json").write_text("{}", encoding="utf-8")
        self.assertEqual(StatsAndCliTests.run_cli(None, "--root", str(root), "--checks", "secrets")[0], 0)
        self.assertEqual(StatsAndCliTests.run_cli(None, "--root", str(root), "--checks", "secrets", "--strict")[0], 1)

    def test_fences_only_matter_in_markdown_notes(self):
        root = fixture_copy()
        (root / "script.py").write_text(f"```\n{FAKE_KEY}\n```\n", encoding="utf-8")
        _, found = lint(root, ("secrets",), secret_scan_extensions=[".md", ".py"])
        self.assertEqual([(f.check, f.severity) for f in found], [("secrets", "error")])

    def test_secret_scan_exclude_takes_folders_files_and_globs_with_a_reason(self):
        root = fixture_copy()
        for rel in ("lib/docs/a.md", "export/data.json", "keep/b.md"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(FAKE_KEY + "\n", encoding="utf-8")
        _, found = lint(root, ("secrets",), secret_scan_exclude={"docs/": "vendor docs", "export/data.json": "sample export", "*.cfg": "x"})
        self.assertEqual([f.path for f in found], ["keep/b.md"])

    def test_secret_scan_exclude_requires_a_reason(self):
        for bad in ({"secret_scan_exclude": ["docs/"]}, {"secret_scan_exclude": {"docs/": ""}}, {"secret_scan_exclude": {"docs/": 3}}):
            root = fixture_copy()
            (root / "kb.config.json").write_text(json.dumps(bad), encoding="utf-8")
            code, _, err = StatsAndCliTests.run_cli(None, "--root", str(root))
            self.assertEqual(code, 2, bad)
            self.assertIn("reason", err)

    def test_default_scan_reads_notes_scripts_and_json_and_can_be_narrowed(self):
        root = fixture_copy()
        for rel in ("tool.ps1", "deploy.sh", "app.js", "settings.yml", ".env", ".env.local", "notes.txt", "image.png", "Keys.md"):
            (root / rel).write_text(FAKE_KEY + "\n", encoding="utf-8")
        _, found = lint(root, ("secrets",))
        self.assertEqual(sorted(f.path for f in found), [".env", ".env.local", "Keys.md", "app.js", "deploy.sh", "settings.yml", "tool.ps1"])
        _, found = lint(root, ("secrets",), secret_scan_extensions=[".md"])
        self.assertEqual([f.path for f in found], ["Keys.md"])
        everything = [".md", ".txt", ".csv", ".json", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".env", ".py", ".ps1", ".sh", ".js", ".ts"]
        _, found = lint(root, ("secrets",), secret_scan_extensions=everything)
        self.assertEqual(sorted(f.path for f in found),
                         [".env", ".env.local", "Keys.md", "app.js", "deploy.sh", "notes.txt", "settings.yml", "tool.ps1"])
        _, ps_only = lint(root, ("secrets",), secret_scan_extensions=[".ps1"])
        self.assertEqual([f.path for f in ps_only], ["tool.ps1"])
        _, no_env = lint(root, ("secrets",), secret_scan_extensions=[".md", ".js"])
        self.assertEqual(sorted(f.path for f in no_env), ["Keys.md", "app.js"])

    def test_env_files_inside_dot_folders_are_not_scanned(self):
        root = fixture_copy()
        (root / ".obsidian" / ".env").write_text(FAKE_KEY + "\n", encoding="utf-8")
        self.assertEqual(lint(root, ("secrets",), secret_scan_extensions=[".md", ".env"])[1], [])

    def test_extension_list_must_use_dots(self):
        root = fixture_copy()
        (root / "kb.config.json").write_text('{"secret_scan_extensions": ["py"]}', encoding="utf-8")
        code, _, err = StatsAndCliTests.run_cli(None, "--root", str(root))
        self.assertEqual(code, 2)
        self.assertIn("start with a dot", err)


class OrphansScopeTests(unittest.TestCase):
    def test_default_scope_is_required_paths_then_everything(self):
        _, found = lint(FIXTURE, ("orphans",))                       # required_paths = Projects/, Home.md: nothing orphaned there
        self.assertEqual(found, [])
        _, found = lint(FIXTURE, ("orphans",), required_paths=[])    # no required_paths: the whole vault
        self.assertEqual([f.path for f in found], ["Concepts/Orphan Idea.md"])

    def test_explicit_scope_wins_over_required_paths_and_star_means_everything(self):
        _, found = lint(FIXTURE, ("orphans",), orphans_scope=["Concepts/"])
        self.assertEqual([f.path for f in found], ["Concepts/Orphan Idea.md"])
        _, found = lint(FIXTURE, ("orphans",), orphans_scope=["Daily/"])
        self.assertEqual(found, [])
        _, found = lint(FIXTURE, ("orphans",), orphans_scope=["*"])
        self.assertEqual([f.path for f in found], ["Concepts/Orphan Idea.md"])

    def test_links_from_notes_outside_the_scope_still_count_as_inbound(self):
        root = fixture_copy()
        (root / "Scoped").mkdir()
        (root / "Scoped" / "Target.md").write_text("# Target\n", encoding="utf-8")
        (root / "Elsewhere.md").write_text("[[Target]]\n[[Home]]\n", encoding="utf-8")
        _, found = lint(root, ("orphans",), orphans_scope=["Scoped/"])
        self.assertEqual(found, [])

    def test_scope_accepts_exact_paths_and_globs(self):
        _, found = lint(FIXTURE, ("orphans",), orphans_scope=["Concepts/Orphan Idea.md"])
        self.assertEqual(len(found), 1)
        _, found = lint(FIXTURE, ("orphans",), orphans_scope=["concepts/*.md"])
        self.assertEqual(len(found), 1)

    def test_orphans_scope_must_be_a_list_of_strings(self):
        root = fixture_copy()
        (root / "kb.config.json").write_text('{"orphans_scope": "Concepts/"}', encoding="utf-8")
        self.assertEqual(StatsAndCliTests.run_cli(None, "--root", str(root))[0], 2)


class PropertyCountTests(unittest.TestCase):
    def test_fields_counts_how_many_notes_use_each_property(self):
        code, out, _ = StatsAndCliTests.run_cli(None, "--root", str(FIXTURE), "--fields", "--json")
        stats = json.loads(out)["stats"]
        self.assertEqual(stats["fields_notes"], 6)
        self.assertEqual(stats["fields"], {"status": 6, "summary": 6, "title": 6, "type": 6, "updated": 5, "aliases": 2, "tags": 2})
        self.assertEqual(list(stats["fields"]), ["status", "summary", "title", "type", "updated", "aliases", "tags"])

    def test_fields_text_output_and_it_implies_stats(self):
        code, out, _ = StatsAndCliTests.run_cli(None, "--root", str(FIXTURE), "--fields")
        self.assertIn("properties used by the 6 notes that have frontmatter", out)
        self.assertIn("  tags: 2", out)
        self.assertIn("notes: 10", out)

    def test_exempt_notes_and_notes_without_frontmatter_are_not_counted(self):
        root = fixture_copy()
        (root / "templates" / "Other.md.tmp").write_text("x", encoding="utf-8")
        (root / "README.md").write_text("---\nreadme_only: yes\n---\n", encoding="utf-8")
        vault, found = lint(root, ())
        counts, with_fm = kb_lint.property_counts(vault)
        self.assertNotIn("readme_only", counts)
        self.assertEqual(with_fm, 6)


class GlobExemptionTests(unittest.TestCase):
    def test_glob_folder_patterns_exempt_notes(self):
        root = fixture_copy()
        for rel in ("old-2020/a.md", "older/b.md", "docs/x/vendor/c.md", "z/docs/x/vendor/d.md"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("# n\n", encoding="utf-8")
        vault, _ = lint(root, (), exempt_dirs=["old-*/", "docs/*/vendor/"])
        self.assertTrue(vault.is_exempt("old-2020/a.md"))
        self.assertFalse(vault.is_exempt("older/b.md"))
        self.assertTrue(vault.is_exempt("docs/x/vendor/c.md"))
        self.assertFalse(vault.is_exempt("z/docs/x/vendor/d.md"))

    def test_glob_in_secret_scan_exclude(self):
        root = fixture_copy()
        for rel in ("old-1/a.md", "keep/b.md"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(FAKE_KEY + "\n", encoding="utf-8")
        _, found = lint(root, ("secrets",), secret_scan_exclude={"old-*/": "archive"})
        self.assertEqual([f.path for f in found], ["keep/b.md"])


class UnusedConfigEntryTests(unittest.TestCase):
    def lint_with_file(self, cfg):
        root = fixture_copy()
        (root / "kb.config.json").write_text(json.dumps(cfg), encoding="utf-8")
        return lint(root, kb_lint.CHECKS)[1]

    def messages(self, found):
        return sorted(f.message for f in found if f.check == "config")

    def test_every_entry_that_matches_nothing_is_reported_once(self):
        found = self.lint_with_file({
            "exempt_dirs": ["nothing-here/", "Concepts/", "typo-*/"],
            "exempt_names": ["missing.md", "README.md"],
            "secret_scan_exclude": {"ghost/": "r", "docs/": "real folder"},
            "orphans_scope": ["Nowhere/", "Daily/"],
            "ignore_links": ["zzz*", "Not Yet Written"],
        })
        self.assertEqual(self.messages(found), [
            "exempt_dirs entry matches nothing: nothing-here/",
            "exempt_dirs entry matches nothing: typo-*/",
            "exempt_names entry matches nothing: missing.md",
            "ignore_links entry matches nothing: zzz*",
            "orphans_scope entry matches nothing: Nowhere/",
            "secret_scan_exclude entry matches nothing: ghost/",
        ])
        self.assertTrue(all(f.severity == "warning" and f.path == kb.CONFIG_NAME for f in found if f.check == "config"))

    def test_entries_for_hidden_folders_and_node_modules_are_not_reported(self):
        found = self.lint_with_file({"exempt_dirs": [".obsidian/", ".git/", "node_modules/", ".venv/"]})
        self.assertEqual(self.messages(found), [])

    def test_built_in_defaults_never_produce_config_warnings(self):
        root = fixture_copy()
        (root / "kb.config.json").unlink()
        _, found = lint(root, kb_lint.CHECKS)
        self.assertEqual([f for f in found if f.check == "config"], [])

    def test_the_sample_vault_config_is_fully_used(self):
        self.assertEqual([f for f in lint(FIXTURE)[1] if f.check == "config"], [])

    def test_config_check_can_be_left_out_and_does_not_fail_the_exit_code(self):
        root = fixture_copy()
        (root / "kb.config.json").write_text(json.dumps({"exempt_dirs": ["nothing-here/"]}), encoding="utf-8")
        _, found = lint(root, ("links",))
        self.assertEqual([f for f in found if f.check == "config"], [])
        clean = fixture_copy()
        for p in list(clean.rglob("*.md")):
            p.unlink()
        (clean / "A.md").write_text("[[B]]", encoding="utf-8")
        (clean / "B.md").write_text("[[A]]", encoding="utf-8")
        (clean / "kb.config.json").write_text(json.dumps({"exempt_dirs": ["nothing-here/"]}), encoding="utf-8")
        self.assertEqual(StatsAndCliTests.run_cli(None, "--root", str(clean))[0], 0)
        self.assertEqual(StatsAndCliTests.run_cli(None, "--root", str(clean), "--strict")[0], 1)

    def test_ignore_links_glob_and_source_entries(self):
        root = fixture_copy()
        (root / "Concepts" / "Linker.md").write_text("[[Nope One]] [[Other Nope]]\n[[Home]]\n", encoding="utf-8")
        (root / "Daily" / "Linker2.md").write_text("[[Nope One]]\n[[Home]]\n", encoding="utf-8")
        (root / "kb.config.json").write_text(json.dumps({"ignore_links": ["source:Daily/", "*nope*"]}), encoding="utf-8")
        vault, found = lint(root, ("links", "config"))
        broken = sorted(f"{f.path}: {f.message}" for f in found if f.check == "links" and f.severity == "error")
        self.assertEqual([b for b in broken if b.startswith(("Concepts/Linker", "Daily/Linker2"))], [])
        self.assertEqual((vault.ignore_used["source:Daily/"], vault.ignore_used["*nope*"]), (1, 3))  # two in Concepts/Linker.md and the Nope.md link in Home.md
        self.assertEqual([f for f in found if f.check == "config"], [])
        (root / "kb.config.json").write_text(json.dumps({"ignore_links": ["source:Concepts/"]}), encoding="utf-8")
        _, found = lint(root, ("links",))
        paths = {f.path for f in found if f.check == "links" and f.severity == "error"}
        self.assertEqual(paths, {"Home.md", "Daily/Linker2.md"})  # only Concepts/ is silenced


class ScanSummaryTests(unittest.TestCase):
    def test_summary_line_says_how_many_files_of_each_type_were_read(self):
        code, out, _ = StatsAndCliTests.run_cli(None, "--root", str(FIXTURE))
        self.assertIn("secret scan read: 1 .json, 9 .md", out)

    def test_other_types_appear_when_listed_and_present(self):
        root = fixture_copy()
        (root / "run.py").write_text("print(1)\n", encoding="utf-8")
        (root / ".env").write_text("A=1\n", encoding="utf-8")
        (root / ".env.local").write_text("A=1\n", encoding="utf-8")
        cfg = json.loads((root / "kb.config.json").read_text(encoding="utf-8"))
        cfg["secret_scan_extensions"] = [".md", ".py", ".env"]
        (root / "kb.config.json").write_text(json.dumps(cfg), encoding="utf-8")
        code, out, _ = StatsAndCliTests.run_cli(None, "--root", str(root))
        self.assertIn("secret scan read: 2 .env*, 9 .md, 1 .py", out)
        stats = json.loads(StatsAndCliTests.run_cli(None, "--root", str(root), "--stats", "--json")[1])["stats"]
        self.assertEqual(stats["secret_scan_files_by_type"], {".env*": 2, ".md": 9, ".py": 1})
        self.assertEqual(stats["secret_scan_files"], 12)

    def test_summary_says_when_the_scan_is_off(self):
        root = fixture_copy()
        cfg = json.loads((root / "kb.config.json").read_text(encoding="utf-8"))
        cfg["secret_scan"] = False
        (root / "kb.config.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.assertIn("secret scan: off", StatsAndCliTests.run_cli(None, "--root", str(root))[1])
        self.assertIn("secret scan: off", StatsAndCliTests.run_cli(None, "--root", str(FIXTURE), "--checks", "links")[1])


class PathScopeTests(unittest.TestCase):
    def scoped(self, path, checks=kb_lint.CHECKS, **cfg):
        root = fixture_copy()
        config, _ = kb.load_config(root)
        config.update(cfg)
        vault, found = kb_lint.run(root, config, checks, True, path)
        return vault, found

    def test_only_notes_under_the_path_are_reported(self):
        _, found = self.scoped("Projects/")
        paths = {f.path for f in found if f.check not in ("config",)}
        self.assertTrue(paths)
        self.assertTrue(all(p.startswith("Projects/") for p in paths), paths)

    def test_folder_without_slash_and_a_single_note_and_a_glob_work(self):
        _, a = self.scoped("Projects")
        _, b = self.scoped("Projects/")
        self.assertEqual([(f.check, f.path, f.message) for f in a], [(f.check, f.path, f.message) for f in b])
        vault, found = self.scoped("Home.md")
        self.assertEqual({f.path for f in found if f.check != "config"}, {"Home.md"})
        vault, found = self.scoped("concepts/*.md")
        self.assertTrue({f.path for f in found} <= {"Concepts/Graph Theory.md", "Concepts/Orphan Idea.md", "Concepts/Café Ideas.md"})

    def test_links_still_resolve_against_the_whole_vault(self):
        _, found = self.scoped("Daily/")
        self.assertEqual([f for f in found if f.check == "links"], [])   # [[Home]] lives outside the scope and is fine

    def test_links_from_notes_outside_the_scope_still_make_a_note_not_an_orphan(self):
        _, found = self.scoped("Concepts/", ("orphans",), orphans_scope=["*"])
        self.assertEqual([f.path for f in found if f.check == "orphans"], ["Concepts/Orphan Idea.md"])  # the other two are linked from Home
        _, found = self.scoped("Concepts/Graph Theory.md", ("orphans",), orphans_scope=["*"])
        self.assertEqual([f for f in found if f.check == "orphans"], [])

    def test_schema_required_and_secrets_follow_the_scope(self):
        root = fixture_copy()
        for rel in ("Projects/Alpha/Keys.md", "Concepts/Keys.md"):
            (root / rel).write_text(FAKE_KEY + "\n", encoding="utf-8")
        config, _ = kb.load_config(root)
        _, found = kb_lint.run(root, config, kb_lint.CHECKS, True, "Projects/Alpha")
        self.assertEqual({f.path for f in found if f.check == "secrets"}, {"Projects/Alpha/Keys.md"})
        self.assertEqual({f.path for f in found if f.check == "schema"}, {"Projects/Alpha/Meeting Notes 2024.md"})
        self.assertEqual([f.path for f in found if f.check == "required"], ["Projects/Alpha/Keys.md"])  # Beta lacks frontmatter too, but is outside the scope

    def test_a_path_that_matches_no_note_is_an_error(self):
        code, _, err = StatsAndCliTests.run_cli(None, "--root", str(FIXTURE), "--path", "Nowhere/")
        self.assertEqual(code, 2)
        self.assertIn("matches no note", err)

    def test_summary_names_the_path_and_stats_report_the_scope(self):
        code, out, _ = StatsAndCliTests.run_cli(None, "--root", str(FIXTURE), "--path", "Projects")
        self.assertIn("path: Projects (3 notes)", out)
        stats = json.loads(StatsAndCliTests.run_cli(None, "--root", str(FIXTURE), "--path", "Projects/Alpha", "--stats", "--json")[1])["stats"]
        self.assertEqual((stats["scope"], stats["notes_in_scope"], stats["notes"]), (["Projects/Alpha/"], 2, 10))

    def test_dot_means_the_whole_vault(self):
        a = StatsAndCliTests.run_cli(None, "--root", str(FIXTURE), "--path", ".")[1]
        self.assertNotIn("path:", a)


class PathSpeedTests(unittest.TestCase):
    def cli_with_spy(self, *args):
        opened = []
        real = kb.read_text

        def spy(path):
            opened.append(Path(path).relative_to(FIXTURE_ROOT_MARKER[0]).as_posix() if FIXTURE_ROOT_MARKER[0] in Path(path).parents else Path(path).name)
            return real(path)

        with mock.patch.object(kb, "read_text", spy):
            result = StatsAndCliTests.run_cli(None, *args)
        return result, opened

    def test_a_path_run_does_not_open_notes_outside_the_path(self):
        root = fixture_copy()
        FIXTURE_ROOT_MARKER[0] = root.resolve()
        (result, opened) = self.cli_with_spy("--root", str(root), "--path", "Daily", "--stats")
        self.assertEqual(result[0], 0)
        self.assertEqual(sorted(set(n for n in opened if n.endswith(".md"))), ["Daily/2024-05-01.md"])

    def test_orphans_are_left_out_of_a_path_run_unless_asked_for(self):
        root = fixture_copy()
        cfg = json.loads((root / "kb.config.json").read_text(encoding="utf-8"))
        cfg["orphans_scope"] = ["*"]
        (root / "kb.config.json").write_text(json.dumps(cfg), encoding="utf-8")
        code, out, _ = StatsAndCliTests.run_cli(None, "--root", str(root), "--path", "Concepts")
        self.assertNotIn("== orphans", out)
        self.assertIn("orphans not checked (add --checks to include them)", out)
        code, out, _ = StatsAndCliTests.run_cli(None, "--root", str(root), "--path", "Concepts", "--checks", "orphans")
        self.assertNotIn("orphans not checked", out)
        self.assertIn("Concepts/Orphan Idea.md", out)

    def test_an_alias_lookup_only_happens_when_a_link_needs_it(self):
        root = fixture_copy()
        FIXTURE_ROOT_MARKER[0] = root.resolve()
        (result, opened) = self.cli_with_spy("--root", str(root), "--path", "Daily", "--checks", "links")
        self.assertEqual(sorted(set(n for n in opened if n.endswith(".md"))), ["Daily/2024-05-01.md"])
        (root / "Daily" / "Ghost.md").write_text("[[Totally Unknown]]\n", encoding="utf-8")
        (result, opened) = self.cli_with_spy("--root", str(root), "--path", "Daily", "--checks", "links")
        self.assertGreater(len(set(n for n in opened if n.endswith(".md"))), 2)  # now the aliases of every note are needed

    def test_stats_for_a_path_are_about_the_path_only(self):
        stats = json.loads(StatsAndCliTests.run_cli(None, "--root", str(FIXTURE), "--path", "Projects/Alpha", "--stats", "--json")[1])["stats"]
        self.assertEqual((stats["schema_applicable"], stats["applicable_with_frontmatter"], stats["notes_with_frontmatter"]), (2, 2, 2))
        self.assertEqual(stats["waiting_marker_lines"], 2)
        rows = json.loads(StatsAndCliTests.run_cli(None, "--root", str(FIXTURE), "--path", "Projects/Alpha", "--by-folder", "--json")[1])["stats"]["by_folder"]
        self.assertEqual([r["folder"] for r in rows], ["Projects", "Projects/Alpha"])


FIXTURE_ROOT_MARKER = [None]


class CloudOnlyTests(unittest.TestCase):
    def patched_run(self, root, cloud_names, **kwargs):
        config, _ = kb.load_config(root)
        config.update(kwargs.pop("cfg", {}))
        opened = []
        real = kb.read_text

        def spy(path):
            opened.append(Path(path).name)
            return real(path)

        flags = {n: f for n, f in cloud_names.items()}
        with mock.patch.object(kb, "file_attributes", lambda p: flags.get(Path(p).name, 0)), mock.patch.object(kb, "read_text", spy):
            vault, found = kb_lint.run(root, config, kb_lint.CHECKS, True)
        return vault, found, opened

    def test_cloud_only_files_are_not_opened_not_counted_as_notes_and_counted_separately(self):
        root = fixture_copy()
        vault, found, opened = self.patched_run(root, {"Graph Theory.md": 0x400000, "diagram.png": 0x1000})
        self.assertNotIn("Graph Theory.md", opened)
        self.assertNotIn("Graph Theory.md", [Path(n).name for n in vault.notes])
        self.assertEqual(sorted(vault.cloud_only), ["Concepts/Graph Theory.md", "assets/diagram.png"])
        self.assertFalse([f for f in found if f.path == "Concepts/Graph Theory.md"])

    def test_every_documented_attribute_flag_counts(self):
        for flag in (0x1000, 0x40000, 0x400000, 0x400000 | 0x20):
            root = fixture_copy()
            vault, _, _ = self.patched_run(root, {"Orphan Idea.md": flag})
            self.assertIn("Concepts/Orphan Idea.md", vault.cloud_only, hex(flag))
        root = fixture_copy()
        vault, _, _ = self.patched_run(root, {"Orphan Idea.md": 0x20})  # an ordinary "archive" attribute is not cloud-only
        self.assertEqual(vault.cloud_only, [])

    def test_a_cloud_only_note_is_still_a_valid_link_target(self):
        root = fixture_copy()
        _, found, _ = self.patched_run(root, {"Graph Theory.md": 0x400000})
        broken = [f.message for f in found if f.check == "links" and "Graph Theory" in f.message and "unresolved" in f.message]
        self.assertEqual(broken, [])

    def test_summary_and_stats_count_skipped_cloud_only_files(self):
        root = fixture_copy()
        with mock.patch.object(kb, "file_attributes", lambda p: 0x400000 if Path(p).suffix in (".png", ".pdf") else 0):
            code, out, _ = StatsAndCliTests.run_cli(None, "--root", str(root))
            stats = json.loads(StatsAndCliTests.run_cli(None, "--root", str(root), "--stats", "--json")[1])["stats"]
        self.assertIn("skipped 2 cloud-only files (not downloaded)", out)
        self.assertEqual(stats["cloud_only_skipped"], 2)

    def test_nothing_is_reported_when_no_file_is_cloud_only(self):
        self.assertNotIn("cloud-only", StatsAndCliTests.run_cli(None, "--root", str(FIXTURE))[1])

    def test_real_files_report_no_cloud_attribute_on_this_system(self):
        self.assertEqual(kb.file_attributes(FIXTURE / "kb.config.json") & kb.CLOUD_ATTRS, 0)
        self.assertEqual(kb.file_attributes(FIXTURE / "no-such-file"), 0)


class FoundAtHintTests(unittest.TestCase):
    def test_unresolved_links_name_the_file_with_the_same_name_elsewhere(self):
        root = fixture_copy()
        (root / "Archive").mkdir()
        (root / "Archive" / "Moved Note.md").write_text("# Moved\n[[Home]]\n", encoding="utf-8")
        (root / "Links.md").write_text("[a](Old/Moved%20Note.md) [[Old/Folder/Moved Note]] [[Truly Missing]]\n[[Home]]\n", encoding="utf-8")
        _, found = lint(root, ("links",))
        msgs = [f.message for f in found if f.path == "Links.md"]
        self.assertIn("unresolved md link -> Old/Moved%20Note.md; found at Archive/Moved Note.md", msgs)
        self.assertIn("unresolved wiki link -> Old/Folder/Moved Note; found at Archive/Moved Note.md", msgs)
        self.assertIn("unresolved wiki link -> Truly Missing", msgs)      # no hint without a candidate

    def test_many_candidates_are_capped(self):
        root = fixture_copy()
        for i in range(5):
            (root / f"dir{i}").mkdir()
            (root / f"dir{i}" / "Same.md").write_text("# s\n[[Home]]\n", encoding="utf-8")
        (root / "Links.md").write_text("[x](gone/Same.md)\n[[Home]]\n", encoding="utf-8")
        _, found = lint(root, ("links",))
        msg = [f.message for f in found if f.path == "Links.md"][0]
        self.assertIn("found at dir0/Same.md, dir1/Same.md, dir2/Same.md (+2 more)", msg)

    def test_a_moved_attachment_is_found_too(self):
        root = fixture_copy()
        (root / "Links.md").write_text("![x](images/diagram.png)\n[[Home]]\n", encoding="utf-8")
        _, found = lint(root, ("links",))
        self.assertIn("unresolved md link -> images/diagram.png; found at assets/diagram.png", [f.message for f in found if f.path == "Links.md"])


class OrphanOkTypesTests(unittest.TestCase):
    def test_notes_of_the_listed_types_are_not_orphans(self):
        root = fixture_copy()
        (root / "Mail.md").write_text(note_with_type("source"), encoding="utf-8")
        (root / "Draft.md").write_text(note_with_type("idea"), encoding="utf-8")
        _, found = lint(root, ("orphans",), orphans_scope=["*"], orphan_ok_types=["source"])
        self.assertEqual(sorted(f.path for f in found), ["Concepts/Orphan Idea.md", "Draft.md"])
        _, found = lint(root, ("orphans",), orphans_scope=["*"])
        self.assertIn("Mail.md", [f.path for f in found])         # default: no exceptions

    def test_a_list_valued_type_and_several_listed_types(self):
        root = fixture_copy()
        (root / "Multi.md").write_text("---\ntitle: M\ntype:\n  - note\n  - source\n---\nbody\n", encoding="utf-8")
        _, found = lint(root, ("orphans",), orphans_scope=["*"], orphan_ok_types=["source", "idea"])
        self.assertEqual([f.path for f in found], [])


def note_with_type(kind):
    return f"---\ntitle: N\ntype: {kind}\nstatus: draft\nupdated: 2024-01-01\n---\nbody\n"


class SecretFileTests(unittest.TestCase):
    def make(self):
        root = fixture_copy()
        for rel in ("keys/server.pem", "keys/id.key", "Config/secrets.json", ".local_secrets/token.txt", "deep/.local_secrets/x.dat",
                    "notes/readme.txt", "keys/public.pub", ".git/hooks/x.pem", ".obsidian/plugin.key"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(FAKE_KEY + "\n", encoding="utf-8")
        return root

    def test_key_files_are_flagged_by_extension_or_folder_whatever_else_is_in_the_name(self):
        root = self.make()
        _, found = lint(root, ("secrets",))
        flagged = sorted(f.path for f in found if f.check == "secret-file")
        # .obsidian is in the default exempt_dirs, .git is never read, secrets.json is not a key file by its name
        self.assertEqual(flagged, [".local_secrets/token.txt", "deep/.local_secrets/x.dat", "keys/id.key", "keys/server.pem"])
        self.assertTrue(all(f.severity == "error" for f in found if f.check == "secret-file"))

    def test_only_the_path_is_reported_and_the_file_is_never_read(self):
        root = self.make()
        opened = []
        real = kb.read_text
        with mock.patch.object(kb, "read_text", lambda p: (opened.append(Path(p).name), real(p))[1]):
            _, found = lint(root, ("secrets",))
        self.assertTrue([f for f in found if f.check == "secret-file"])
        for name in ("server.pem", "id.key", "token.txt", "x.dat"):
            self.assertNotIn(name, opened)
        self.assertNotIn(FAKE_KEY, json.dumps([f.__dict__ for f in found]))
        self.assertTrue(all("content not read" in f.message for f in found if f.check == "secret-file"))

    def test_patterns_are_configurable_and_can_be_switched_off(self):
        root = self.make()
        (root / "wallet.pfx").write_text("x", encoding="utf-8")
        _, found = lint(root, ("secrets",), secret_file_patterns=["*.pfx"])
        self.assertEqual([f.path for f in found if f.check == "secret-file"], ["wallet.pfx"])
        _, found = lint(root, ("secrets",), secret_file_patterns=[])
        self.assertEqual([f for f in found if f.check == "secret-file"], [])
        _, found = lint(root, ("secrets",), secret_scan=False)
        self.assertEqual(found, [])

    def test_secret_scan_exclude_and_scope_apply(self):
        root = self.make()
        _, found = lint(root, ("secrets",), secret_scan_exclude={"keys/": "test keys"})
        self.assertNotIn("keys/id.key", [f.path for f in found])
        vault = kb.Vault(root, kb.load_config(root)[0])
        vault.set_scope("Config/")
        (root / "Config" / "store.p12").write_text("x", encoding="utf-8")
        vault = kb.Vault(root, kb.load_config(root)[0])
        vault.set_scope("Config/")
        self.assertEqual([f.path for f in kb_lint.check_secret_files(vault)], ["Config/store.p12"])

    def test_names_that_merely_contain_the_word_are_not_flagged(self):
        root = fixture_copy()
        for rel in ("my secrets notes.md", "keyboard.txt", "pemmican.md", "secretsauce.md"):
            (root / rel).write_text("x\n", encoding="utf-8")
        _, found = lint(root, ("secrets",))
        self.assertEqual([f for f in found if f.check == "secret-file"], [])

    def test_secret_file_counts_in_the_folder_breakdown_even_for_folders_without_notes(self):
        root = self.make()
        vault, found = lint(root, ("secrets",))
        rows = {r["folder"]: r for r in kb_lint.folder_breakdown(vault, found, 1)}
        self.assertEqual((rows["keys"]["secrets"], rows["keys"]["notes"]), (2, 0))
        self.assertEqual(rows[".local_secrets"]["secrets"], 1)


class KeyFileExtensionTests(unittest.TestCase):
    def flagged(self, names, **cfg):
        root = fixture_copy()
        for rel in names:
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("x", encoding="utf-8")
        _, found = lint(root, ("secrets",), **cfg)
        return sorted(f.path for f in found if f.check == "secret-file")

    def test_every_key_extension_in_the_list_is_flagged(self):
        names = ["a.key", "b.pem", "c.pfx", "d.p12", "e.jks", "f.keystore", "g.ppk", "id_rsa", "id_rsa_backup", "sub/ID_RSA.old"]
        self.assertEqual(self.flagged(names), sorted(names))

    def test_extensions_are_matched_without_regard_to_case(self):
        self.assertEqual(self.flagged(["Server.PEM", "A.Key"]), ["A.Key", "Server.PEM"])

    def test_code_documentation_and_public_keys_are_never_key_files_because_of_their_name(self):
        names = ["secrets.py", "secrets.md", "Secrets.js", "secret_manager.ts", "my secrets.md", "id_rsa.pub", "id_rsa_notes.md",
                 "secrets.ps1", "secrets.sh", "secrets.html", "id_rsa.py"]
        self.assertEqual(self.flagged(names), [])
        # ... even when an older config still lists a name pattern such as "secrets.*"
        self.assertEqual(self.flagged(names, secret_file_patterns=["secrets.*", "id_rsa*", "*secret*"]), [])

    def test_an_old_name_pattern_still_flags_data_files_that_are_not_code(self):
        self.assertEqual(self.flagged(["secrets.yml", "secrets.json", "secrets.py"], secret_file_patterns=["secrets.*"]),
                         ["secrets.json", "secrets.yml"])

    def test_a_plain_extension_pattern_is_taken_literally_so_py_can_be_listed_on_purpose(self):
        self.assertEqual(self.flagged(["a.py", "b.md"], secret_file_patterns=["*.py"]), ["a.py"])

    def test_the_old_secrets_star_default_is_gone(self):
        cfg, _ = kb.load_config(Path(tempfile.mkdtemp(prefix="kbc-")))
        self.assertNotIn("secrets.*", cfg["secret_file_patterns"])
        for ext in ("*.key", "*.pem", "*.pfx", "*.p12", "*.jks", "*.keystore", "*.ppk", "id_rsa*"):
            self.assertIn(ext, cfg["secret_file_patterns"])

    def test_the_local_secrets_folder_still_flags_files_but_not_code_or_docs_in_it(self):
        self.assertEqual(self.flagged([".local_secrets/token.txt", ".local_secrets/notes.md", ".local_secrets/run.py"]),
                         [".local_secrets/token.txt"])


class EverySecretCheckHonoursTheSameSkipRulesTests(unittest.TestCase):
    """One rule for all secret checks: secret_scan_exclude, exempt_dirs and dependency folders are skipped."""
    SKIPPED = ("templates", "excluded", "vendor", "site-packages/pkg", "app/venv", "build_output")

    def make(self):
        root = fixture_copy()
        for folder in self.SKIPPED:
            for name, content in (("note.md", FAKE_KEY + "\n"), ("tool.ps1", FAKE_KEY + "\n"), (".env", FAKE_KEY + "\n"),
                                  ("server.pem", "x"), ("id_rsa", "x")):
                path = root / folder / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
        (root / "kept").mkdir()
        (root / "kept" / "note.md").write_text(FAKE_KEY + "\n", encoding="utf-8")
        (root / "kept" / "server.pem").write_text("x", encoding="utf-8")
        return root

    CFG = dict(secret_scan_extensions=[".md", ".ps1", ".env"], secret_scan_exclude={"excluded/": "test data", "build_output/": "generated"})

    def test_no_secret_check_reports_anything_in_skipped_places(self):
        root = self.make()
        _, found = lint(root, ("secrets",), **self.CFG)
        paths = sorted({f.path for f in found})
        self.assertEqual(paths, ["kept/note.md", "kept/server.pem"], paths)
        self.assertEqual(sorted(f.check for f in found), ["secret-file", "secrets"])

    def test_content_in_an_exempt_dir_is_not_scanned_and_not_counted(self):
        root = self.make()
        vault, found = lint(root, ("secrets",), **self.CFG)
        self.assertEqual(vault.secret_scan_counts[".md"], 10)  # 9 notes of the sample vault (its templates note is exempt) + kept/note.md
        self.assertNotIn("templates/note.md", [f.path for f in found])

    def test_exempt_dirs_from_the_config_apply_to_key_files_and_env_files_too(self):
        root = self.make()
        _, found = lint(root, ("secrets",), exempt_dirs=["kept/"], **self.CFG)
        paths = {f.path for f in found}
        self.assertNotIn("kept/server.pem", paths)
        self.assertNotIn("kept/note.md", paths)
        self.assertIn("templates/note.md", paths)   # no longer exempt in this config

    def test_dependency_folders_are_skipped_even_when_exempt_dirs_does_not_list_them(self):
        root = self.make()
        _, found = lint(root, ("secrets",), exempt_dirs=[], secret_scan_exclude={}, secret_scan_extensions=[".md", ".ps1", ".env"])
        paths = {f.path for f in found}
        for skipped in ("vendor/", "site-packages/", "app/venv/"):
            self.assertFalse([p for p in paths if skipped in p], skipped)
        self.assertIn("templates/note.md", paths)

    def test_dependency_files_skipped_are_counted_in_stats_and_the_summary(self):
        root = self.make()
        vault, found = lint(root, ("secrets",), **self.CFG)
        skipped = kb_lint.dependency_files_skipped(vault)
        self.assertEqual(skipped, sum(1 for f in vault.files if any(d in f for d in ("vendor/", "site-packages/", "/venv/"))))
        code, out, _ = StatsAndCliTests.run_cli(None, "--root", str(root))
        self.assertIn(f"secret checks skipped {skipped} files in dependency folders", out)
        stats = json.loads(StatsAndCliTests.run_cli(None, "--root", str(root), "--stats", "--json")[1])["stats"]
        self.assertEqual(stats["secret_scan_skipped_dependency_files"], skipped)

    def test_the_summary_says_nothing_when_no_dependency_files_exist(self):
        self.assertNotIn("dependency folders", StatsAndCliTests.run_cli(None, "--root", str(FIXTURE))[1])

    def test_secret_in_example_in_a_skipped_place_is_also_skipped(self):
        root = fixture_copy()
        (root / "vendor").mkdir()
        (root / "vendor" / "doc.md").write_text("```" + chr(10) + FAKE_KEY + chr(10) + "```" + chr(10), encoding="utf-8")
        _, found = lint(root, ("secrets",))
        self.assertEqual([f for f in found if f.path.startswith("vendor/")], [])


class VersionFlagTests(unittest.TestCase):
    def test_every_tool_reports_the_kit_version(self):
        import kb_backup, kb_frontmatter, kb_index
        self.assertEqual(kb.KIT_VERSION, "1.8.1")
        for module in (kb_lint, kb_index, kb_frontmatter, kb_backup):
            out = io.StringIO()
            with self.assertRaises(SystemExit) as ctx, redirect_stdout(out):
                module.main(["--version"])
            self.assertEqual(ctx.exception.code, 0)
            self.assertEqual(out.getvalue().strip(), "kit 1.8.1")

    def test_stats_carry_the_version(self):
        stats = json.loads(StatsAndCliTests.run_cli(None, "--root", str(FIXTURE), "--stats", "--json")[1])["stats"]
        self.assertEqual(stats["kit_version"], "1.8.1")


class KitDocumentsTests(unittest.TestCase):
    KIT = HERE.parent

    def read(self, rel):
        return (self.KIT / rel).read_text(encoding="utf-8")

    def test_install_log_template_has_the_columns_and_the_clock_rule(self):
        text = self.read("templates/INSTALL-LOG.md")
        self.assertIn("| Time (from the clock) | Phase | Action | Problem | Fix |", text)
        self.assertIn("Times come from the clock, not from memory", text)
        self.assertIn("Run `date`", text)
        self.assertIn("Never write an estimated time", text)

    def test_phase_zero_of_the_prompt_opens_the_log_and_the_report_is_filled_from_it(self):
        prompt = self.read("BOOTSTRAP-PROMPT.md")
        phase0 = prompt.split("**Phase 1")[0]
        self.assertIn("templates/INSTALL-LOG.md", phase0)
        self.assertIn("from the clock", phase0)
        self.assertIn("from the install log", prompt)
        self.assertIn("Security findings during install", prompt)

    def test_guide_has_the_security_and_upgrade_sections(self):
        guide = self.read("GUIDE.md")
        for heading in ("## Security findings during install", "## Upgrading the kit"):
            self.assertIn(heading, guide)
        security = guide.split("## Security findings during install")[1].split("## Upgrading the kit")[0]
        for phrase in ("Compare byte for byte", "outside the vault and outside any synced folder", "never deletes permanently",
                       "recycle bin", "version history", "Only the owner empties them", "who approved"):
            self.assertIn(phrase, security)
        upgrading = guide.split("## Upgrading the kit")[1].split("## Safety")[0]
        for phrase in ("Back up the old tools", "Do not edit or overwrite `kb.config.json`", "Run the tests", "--version", "Log it"):
            self.assertIn(phrase, upgrading)

    def test_the_safety_section_no_longer_claims_the_tools_never_change_notes(self):
        safety = self.read("GUIDE.md").split("## Safety")[1]
        self.assertNotIn("never change your notes", safety)
        self.assertIn("kb_frontmatter --write", safety)


class BeforeAfterQueryTestDocsTests(unittest.TestCase):
    KIT = HERE.parent

    def read(self, rel):
        return (self.KIT / rel).read_text(encoding="utf-8")

    def table_rows(self, text, header_start):
        lines = text.split("\n")
        start = next(i for i, ln in enumerate(lines) if ln.startswith(header_start))
        rows = []
        for ln in lines[start:]:
            if not ln.startswith("|"):
                break
            rows.append([c.strip() for c in ln.strip().strip("|").split("|")])
        return rows

    def test_query_test_table_has_the_new_columns_and_two_rows_per_question(self):
        rows = self.table_rows(self.read("templates/QUERY-TEST.md"), "| # | Question |")
        header, data = rows[0], rows[2:]
        for column in ("Run (before / after)", "Files opened", "Tokens (est.)"):
            self.assertIn(column, header)
        self.assertTrue(all(len(r) == len(header) for r in rows), "every row has the same number of cells")
        run_col = header.index("Run (before / after)")
        self.assertEqual(len(data), 20)
        self.assertEqual([r[run_col] for r in data], ["before", "after"] * 10)
        self.assertEqual([r[0] for r in data][-2:], ["10 (trap)", "10 (trap)"])

    def test_query_test_rules(self):
        text = self.read("templates/QUERY-TEST.md")
        for phrase in ("The owner writes the questions and the expected answers, not the agent",
                       "before** the run and **outside the vault",
                       "written down in the folder and stable in time",
                       "only in the owner's head or in an email",
                       'The right answer is "not found in the vault"',
                       "same questions, word for word",
                       "Never present an estimate as a measurement",
                       "characters read divided by 4",
                       "(before - after) / before"):
            self.assertIn(phrase, text.replace("**", "**"), phrase)

    def test_result_block_compares_before_and_after(self):
        text = self.read("templates/QUERY-TEST.md")
        result = text.split("## Result")[1]
        for phrase in ("**Before**", "**After**", "**Saving**", "Files opened (total / average)", "Tokens (total / average)"):
            self.assertIn(phrase, result)

    def test_guide_explains_the_method_and_links_it_to_the_rollout(self):
        guide = self.read("GUIDE.md")
        self.assertIn("## Measuring the saving: the query test before and after", guide)
        section = guide.split("## Measuring the saving")[1].split("## Daily use")[0]
        for phrase in ("folder you will migrate later", "Run \"before\"", "Run \"after\"", "same questions word for word", "files opened",
                       "(est.)", "The difference is the proof", "You** write the questions", "trap", "not found in the vault",
                       "an indication, not a benchmark"):
            self.assertIn(phrase, section, phrase)
        self.assertIn("Query test, before and after", guide)
        self.assertIn("run before and after a folder is migrated", guide)

    def test_prompt_phases_4_and_5_carry_the_before_and_after_runs(self):
        prompt = self.read("BOOTSTRAP-PROMPT.md")
        phase4 = prompt.split("**Phase 4")[1].split("**Phase 5")[0]
        for phrase in ("I write the questions and the expected answers", "you never write or edit them", "(b) **Baseline:**",
                       "not migrated yet", "run: before", "distinct files you opened", "(est.)"):
            self.assertIn(phrase, phase4, phrase)
        phase5 = prompt.split("**Phase 5")[1].split("**Phase 6")[0]
        for phrase in ("the same questions word for word", "run: after", "the saving"):
            self.assertIn(phrase, phase5, phrase)

    def test_install_report_has_a_before_after_block_without_question_text(self):
        report = self.read("templates/INSTALL-REPORT.md")
        section = report.split("## 5. Query test")[1].split("## 6.")[0]
        for phrase in ("Before migration", "After migration", "Saving", "Files opened (total / avg)", "Tokens (total / avg)",
                       "measured / estimated", "counts only, no question text", "Did accuracy hold"):
            self.assertIn(phrase, section, phrase)
        self.assertNotIn("estimate is fine", section)

    def test_the_kit_version_is_bumped(self):
        self.assertEqual(kb.KIT_VERSION, "1.8.1")


class QueryStatsDocsTests(unittest.TestCase):
    KIT = HERE.parent

    def read(self, rel):
        return (self.KIT / rel).read_text(encoding="utf-8")

    def test_tools_readme_documents_the_tool_its_privacy_and_its_limits(self):
        section = self.read("tools/README.md").split("## kb_querystats")[1].split("## kb_index")[0]
        for phrase in ("--session", "--latest --project-dir", "--only-under", "--no-paths", "--include-bash", "--last N",
                       "own fresh session", "distinct", "usage", "cache read", "context peak", "never prints what was said",
                       "fields not found", "heuristic", "sub-agents", "use an estimate and mark it"):
            self.assertIn(phrase, section, phrase)
        self.assertIn("Six helpers", self.read("tools/README.md"))

    def test_guide_prefers_the_measurement_and_keeps_the_estimate_as_a_fallback(self):
        section = self.read("GUIDE.md").split("## Measuring the saving")[1].split("## Daily use")[0]
        for phrase in ("Measure, do not ask the agent", "kb_querystats.py --session", "needs no cooperation from the agent",
                       "(est.)", "its own fresh session", "measured with kb_querystats"):
            self.assertIn(phrase, section, phrase)

    def test_query_test_template_and_report_ask_whether_it_was_measured(self):
        template = self.read("templates/QUERY-TEST.md")
        self.assertIn("Preferred: measure with `kb_querystats.py`", template)
        self.assertIn("**Measured with kb_querystats:** yes / no", template)
        self.assertIn("Never present an estimate as a measurement", template)
        report = self.read("templates/INSTALL-REPORT.md").split("## 5. Query test")[1].split("## 6.")[0]
        self.assertIn("Measured with `kb_querystats`", report)

    def test_the_tool_is_wired_to_the_same_version_and_privacy_rules_as_the_others(self):
        source = (self.KIT / "tools" / "kb_querystats.py").read_text(encoding="utf-8")
        self.assertIn("never prints what was said", " ".join(source.split()))
        self.assertEqual(kb.KIT_VERSION, "1.8.1")


FAKE_VALUES = {"@L9@": "get_access_token_interactively_long_name", "@L8@": "get_access_token_interactively_long", "@L1@": "get_access_token_from_login", "@L2@": "ACCESS_TOKEN_NAME", "@L3@": "ConvertTo-SecureString", "@L4@": "Get-AccessTokenInteractive", "@L5@": "Invoke-RestMethod", "@L6@": "fetch_access_token_interactively", "@L7@": "getAccessTokenInteractively", "@L10@": "/var/run/secrets/kubernetes/token",
               "@K1@": "abcd1234efgh5678ijkl", "@K2@": "abcd1234efgh5678ijkl5678mnop", "@K3@": "Sup3rS3cretValue" + chr(33) * 2,
               "@K4@": "Zx9" * 8, "@K5@": "P4ssw0rdLiteral12345", "@K6@": "Hunter2" * 3, "@K7@": "LiteralPassw0rdValue" + chr(33),
               "@K8@": "Xk3-92jsd8-fn29sdk-33asdf", "@K9@": "Sup3rS3cr3tPassw0rdValue1"}


def fill(text):
    for marker, value in FAKE_VALUES.items():
        text = text.replace(marker, value)
    return text


class LiteralOnlyAssignmentTests(unittest.TestCase):
    """A keyword assignment is reported only when the value written there is a string literal."""

    def hits(self, name, lines, **cfg):
        root = fixture_copy()
        (root / name).write_text(fill(chr(10).join(lines)) + chr(10), encoding="utf-8")
        cfg.setdefault("secret_scan_extensions", [".md", ".py", ".ps1", ".sh", ".js", ".ts", ".yml", ".yaml", ".json", ".env", ".bat"])
        _, found = lint(root, ("secrets",), **cfg)
        return sorted(f.line for f in found if f.path == name and f.check in ("secrets", "secret-in-example"))

    def check(self, name, flagged, clean):
        self.assertEqual(self.hits(name, flagged), list(range(1, len(flagged) + 1)), f"{name}: literals must be reported")
        for line in clean:
            self.assertEqual(self.hits(name, [line]), [], f"{name}: must NOT be reported: {line}")

    def test_python(self):
        self.check("s.py",
                   ['API_KEY = "@K1@"', "password = '@K3@'", 'client_secret="@K4@"',
                    'self.token: str = "@K1@"', 'login(token="@K1@")', 'cfg = {"token": "@K1@"}'],
                   ['token = os.environ["X"]', 'token = os.getenv("TOKEN_NAME_LONG_ENOUGH")', "password = getpass.getpass()", 'secret = config["secret"]',
                    "token = @L1@()", "self.token = self._fetch_token(user, pwd)", 'token = f"Bearer {access}"',
                    "token: str = Field(...)", 'token_url = "https://login.example.com/oauth2/token"', 'password = ""', "token = None",
                    'SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")', 'api_key = "your-api-key-here"', 'secret = "@L2@"',
                    'token = "Bearer " + access', "password = input('Password: ')", "token = auth.refresh(user, password)", "secret = vault.read('db')['secret']",
                    "if token == expected_token_value_from_config:", 'password_file = "/run/secrets/db_password_file"'])

    def test_powershell(self):
        self.check("s.ps1",
                   ['$password = "@K5@"', "$apiKey = '@K1@'",
                    '$password = @L3@ "@K6@" -AsPlainText -Force',
                    '$clientSecret = "@K4@"'],
                   ["$token = @L4@ -Tenant $t", "$token = Get-Token(...)",
                    "$token = (@L5@ -Uri $u -Method Post).access_token", "$t = (@L5@ -Uri $u).access_token",
                    "$secret = $env:CLIENT_SECRET", '$password = Read-Host "Password" -AsSecureString', '$token = "Bearer $accessToken"',
                    "$token = $response.access_token", "$apiKey = $config.ApiKey", "$cred = Get-Credential",
                    '$token = [Environment]::GetEnvironmentVariable("API_TOKEN_LONG_NAME")', "$token = @L5@ -Uri $url -Method Post",
                    '$password = @L3@ $plain -AsPlainText -Force', "$token = az account get-access-token --query accessToken -o tsv",
                    '$headers = @{ Authorization = "Bearer $token" }'])

    def test_bash(self):
        self.check("s.sh",
                   ['export API_TOKEN="@K1@"', "PASSWORD=@K9@", "db_secret='@K4@'"],
                   ["TOKEN=$(az account get-access-token --query accessToken -o tsv)", 'PASSWORD="$DB_PASSWORD"', "SECRET=$(cat /run/secrets/x)",
                    "token=${GITHUB_TOKEN}", "read -s PASSWORD", "api_key=$(pass show x)", 'export TOKEN="${TOKEN:-}"', "TOKEN=`cat token.txt`",
                    "token=$1", "secret=%SECRET_FROM_ENVIRONMENT%"])

    def test_batch_files(self):
        self.assertEqual(self.hits("s.bat", ["set PASSWORD=@K9@", "set TOKEN=%ACCESS_TOKEN_FROM_ENV%"]), [1])
        self.assertEqual(self.hits("s.bat", ['set "API_KEY=@K1@"']), [])  # a quoted whole assignment is not parsed; the specific key patterns still apply

    def test_javascript_and_typescript(self):
        for name in ("s.js", "s.ts"):
            self.check(name,
                       ['const apiKey = "@K1@";', "password: '@K7@'", '{ token: "@K1@" }',
                        'headers.secret = "@K4@"'],
                       ["const token = process.env.API_TOKEN;", "token = await getToken();", 'const secret = config.get("secret")',
                        "password: process.env.DB_PASSWORD", "const token = `Bearer ${t}`", "apiKey: options.apiKey",
                        "const { token } = await auth()", "let password = prompt('Password')", "const token = localStorage.getItem('token_long_name')",
                        "token: string = this.auth.currentToken"])

    def test_yaml_json_env_and_notes_allow_an_unquoted_key_like_literal(self):
        self.check("s.yml", ["password: @K8@", '"token": "@K1@"'],
                   ["password: ${DB_PASSWORD}", "token: !secret my_token", "secret: {{ vault.secret }}", "token: \"{{ lookup('env','T') }}\"",
                    "password: changeme-changeme-changeme", "secret: ${{ secrets.DEPLOY_SECRET_VALUE }}"])
        self.check("s.json", ['{"password": "@K8@"}'], ['{"password": "${DB_PASSWORD}"}', '{"token": ""}'])
        self.check("s.env", ["API_TOKEN=@K2@", "DB_PASSWORD=@K8@"],
                   ["API_TOKEN=", "DB_PASSWORD=${DB_PASSWORD}", "TOKEN=changeme"])
        self.check("s.md", ["The password: @K8@"], ["token = @L1@()", "password: <your password>",
                                                                          "the token: expires after one hour of inactivity today"])

    def test_the_old_false_positive_shape_is_gone_a_long_function_name_on_the_right(self):
        for name, line in (("s.ps1", "$token = @L5@ -Uri https://x.example/token"), ("s.py", "token = @L6@()"),
                           ("s.ts", "const token = @L7@();"), ("s.sh", "token=@L8@")):
            self.assertEqual(self.hits(name, [line]), [], line)
        self.assertEqual(self.hits("s.md", ["token: @L9@"]), [])

    def test_identifier_prefixes_are_covered_but_longer_words_are_not(self):
        self.assertEqual(self.hits("s.py", ['db_password = "@K4@"', 'GITHUB_TOKEN = "@K4@"']), [1, 2])
        self.assertEqual(self.hits("s.py", ['tokens = "@K4@"', 'token_url = "@K4@"', 'secretary = "@K4@"']), [])

    def test_specific_key_patterns_still_report_whatever_the_right_hand_side_is(self):
        self.assertEqual(self.hits("s.py", ["token = get_it(" + FAKE_KEY + ")"]), [1])
        self.assertEqual(self.hits("s.ps1", ["Write-Host " + FAKE_KEY]), [1])

    def test_a_hit_in_a_fenced_block_of_a_note_is_still_only_the_example_warning(self):
        root = fixture_copy()
        (root / "Doc.md").write_text(fill(chr(10).join(["```", "password: @K8@", "```", "password: @K8@"])) + chr(10), encoding="utf-8")
        _, found = lint(root, ("secrets",))
        self.assertEqual(sorted((f.check, f.line) for f in found), [("secret-in-example", 2), ("secrets", 4)])

    def test_the_value_is_never_printed(self):
        root = fixture_copy()
        (root / "s.ps1").write_text(fill('$password = "@K5@"') + chr(10), encoding="utf-8")
        _, found = lint(root, ("secrets",), secret_scan_extensions=[".ps1"])
        self.assertEqual(len(found), 1)
        self.assertNotIn(FAKE_VALUES["@K5@"], json.dumps([f.__dict__ for f in found]))
        self.assertIn("keyword assignment with a literal value", found[0].message)

    def test_placeholders_paths_urls_and_interpolation_inside_literals_are_ignored(self):
        lines = ['token = "your-token-goes-here-123456"', 'password = "xxxxxxxxxxxxxxxxxxxx"', 'secret = "@L10@"',
                 'token = "https://login.example.com/oauth2/token/refresh"', 'password = "pa$$w0rd-with-$interpolation1"',
                 'token = "<paste your token here 1234>"', 'secret = "{{ SECRET_FROM_TEMPLATE_12345 }}"', 'token = "C:\\\\Users\\\\x\\\\token.txt"']
        self.assertEqual(self.hits("s.py", lines), [])
        self.assertEqual(self.hits("s.py", ['password = "https://user:@K4@@example.com/x"']), [1])  # credentials inside a URL are real


class ScriptsNotScannedTests(unittest.TestCase):
    def summary(self, root, **cfg):
        config, _ = kb.load_config(root)
        config.update(cfg)
        (root / "kb.config.json").write_text(json.dumps({k: v for k, v in config.items() if not k.startswith("_")}, default=list), encoding="utf-8")
        return StatsAndCliTests.run_cli(None, "--root", str(root))[1]

    def make(self):
        root = fixture_copy()
        for rel in ("a.py", "tools/b.ps1", "c.sh", "d.json", "e.yml", "f.js", ".env", "sub/.env.local", "g.txt", "h.png"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("x\n", encoding="utf-8")
        return root

    def test_with_the_defaults_scripts_and_json_are_scanned_and_nothing_is_left_out(self):
        root = self.make()
        out = StatsAndCliTests.run_cli(None, "--root", str(root))[1]
        self.assertNotIn("scripts not scanned", out)
        stats = json.loads(StatsAndCliTests.run_cli(None, "--root", str(root), "--stats", "--json")[1])["stats"]
        self.assertEqual(stats["scripts_not_scanned"], 0)

    def test_the_summary_counts_script_files_that_are_not_scanned(self):
        root = self.make()
        cfg = json.loads((root / "kb.config.json").read_text(encoding="utf-8"))
        cfg["secret_scan_extensions"] = [".md"]
        (root / "kb.config.json").write_text(json.dumps(cfg), encoding="utf-8")
        out = StatsAndCliTests.run_cli(None, "--root", str(root))[1]
        self.assertIn("scripts not scanned (8 script files found; see secret_scan_extensions)", out)
        stats = json.loads(StatsAndCliTests.run_cli(None, "--root", str(root), "--stats", "--json")[1])["stats"]
        self.assertEqual(stats["scripts_not_scanned"], 8)

    def test_files_that_are_covered_excluded_exempt_or_in_dependency_folders_are_not_counted(self):
        root = self.make()
        (root / "vendor").mkdir()
        (root / "vendor" / "lib.py").write_text("x\n", encoding="utf-8")
        (root / "templates" / "t.sh").write_text("x\n", encoding="utf-8")
        cfg = json.loads((root / "kb.config.json").read_text(encoding="utf-8"))
        cfg["secret_scan_extensions"] = [".md", ".py", ".ps1", ".sh", ".env"]
        cfg["secret_scan_exclude"] = {"sub/": "generated"}
        (root / "kb.config.json").write_text(json.dumps(cfg), encoding="utf-8")
        config, _ = kb.load_config(root)
        vault = kb.Vault(root, config)
        self.assertEqual(kb_lint.scripts_not_scanned(vault), 3)  # d.json, e.yml, f.js

    def test_the_line_is_absent_when_everything_is_scanned_or_the_scan_is_off(self):
        root = self.make()
        cfg = json.loads((root / "kb.config.json").read_text(encoding="utf-8"))
        cfg["secret_scan_extensions"] = [".md", ".py", ".ps1", ".sh", ".json", ".yml", ".yaml", ".js", ".ts", ".bat", ".cmd", ".env"]
        (root / "kb.config.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.assertNotIn("scripts not scanned", StatsAndCliTests.run_cli(None, "--root", str(root))[1])
        cfg["secret_scan"] = False
        (root / "kb.config.json").write_text(json.dumps(cfg), encoding="utf-8")
        out = StatsAndCliTests.run_cli(None, "--root", str(root))[1]
        self.assertNotIn("scripts not scanned", out)
        self.assertIn("secret scan: off", out)

    def test_a_vault_without_scripts_says_nothing(self):
        self.assertNotIn("scripts not scanned", StatsAndCliTests.run_cli(None, "--root", str(FIXTURE))[1])

    def test_a_path_run_counts_only_scripts_in_the_path(self):
        root = self.make()
        cfg = json.loads((root / "kb.config.json").read_text(encoding="utf-8"))
        cfg["secret_scan_extensions"] = [".md"]
        (root / "kb.config.json").write_text(json.dumps(cfg), encoding="utf-8")
        (root / "tools" / "Note.md").write_text("# n" + chr(10) + "[[Home]]" + chr(10), encoding="utf-8")   # --path needs a note to point at
        out = StatsAndCliTests.run_cli(None, "--root", str(root), "--path", "tools")[1]
        self.assertIn("scripts not scanned (1 script files found", out)

    def test_a_notes_only_config_shows_the_hint_exactly_where_the_real_finding_hides(self):
        root = fixture_copy()
        (root / "deploy.py").write_text(fill('API_TOKEN = "@K1@"') + chr(10), encoding="utf-8")
        code, out, _ = StatsAndCliTests.run_cli(None, "--root", str(root), "--checks", "secrets")
        self.assertEqual((code, "deploy.py:1" in out), (1, True))          # found with the defaults
        cfg = json.loads((root / "kb.config.json").read_text(encoding="utf-8"))
        cfg["secret_scan_extensions"] = [".md"]
        (root / "kb.config.json").write_text(json.dumps(cfg), encoding="utf-8")
        code, out, _ = StatsAndCliTests.run_cli(None, "--root", str(root), "--checks", "secrets")
        self.assertEqual(code, 0)                                          # not found when someone narrows the scan ...
        self.assertIn("scripts not scanned (1 script files found", out)    # ... but the summary says why
        cfg = json.loads((root / "kb.config.json").read_text(encoding="utf-8"))
        cfg["secret_scan_extensions"] = [".md", ".py"]
        (root / "kb.config.json").write_text(json.dumps(cfg), encoding="utf-8")
        code, out, _ = StatsAndCliTests.run_cli(None, "--root", str(root), "--checks", "secrets")
        self.assertEqual(code, 1)
        self.assertIn("deploy.py:1", out)


class ScriptAdviceDocsTests(unittest.TestCase):
    KIT = HERE.parent

    def read(self, rel):
        return (self.KIT / rel).read_text(encoding="utf-8")

    def test_prompt_phase_zero_proposes_scanning_scripts(self):
        phase0 = self.read("BOOTSTRAP-PROMPT.md").split("**Phase 1")[0]
        for phrase in ("secret scan reads notes, scripts and JSON by default", "secret_scan_extensions", ".ps1", ".yml", "ask me"):
            self.assertIn(phrase, phase0, phrase)

    def test_guide_explains_the_default_the_literal_only_rule_and_masking_pattern_files(self):
        guide = self.read("GUIDE.md")
        self.assertIn("by default reads notes, scripts and JSON", guide)
        self.assertIn("scripts not scanned", guide)
        self.assertIn("## Files that contain example secrets on purpose", guide)
        section = guide.split("## Files that contain example secrets on purpose")[1].split("## ")[0]
        for phrase in ("masking", "secret_scan_exclude", "with a reason", "Do not change the tool", "literal"):
            self.assertIn(phrase, section, phrase)

    def test_tools_readme_states_the_literal_only_rule(self):
        readme = self.read("tools/README.md")
        for phrase in ("only when the value written there is a string literal", "function call", "environment", "scripts not scanned"):
            self.assertIn(phrase, readme, phrase)


class PrivateKeyFieldTests(unittest.TestCase):
    KEY = "A1b2C3d4" * 6

    def hits(self, name, lines, **cfg):
        root = fixture_copy()
        (root / name).write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
        cfg.setdefault("secret_scan_extensions", [".md", ".json", ".txt", ".yml", ".py", ".ps1"])
        _, found = lint(root, ("secrets",), **cfg)
        return sorted((f.line, f.check, f.severity) for f in found if f.path == name)

    def test_private_key_fields_with_a_long_string_value_are_errors_with_path_and_line_only(self):
        lines = [f'"privateKey": "{self.KEY}"', f'"private_key": "{self.KEY}"', f'"privateKeyPem": "{self.KEY}"',
                 f'"client_secret": "{self.KEY[:30]}"', f'"apiKey": "{self.KEY[:24]}"',
                 '"private_key": "MIIEvQIBADANBgkq' + chr(92) + "nhkiG9w0BAQEFAASC" + self.KEY + '"']
        self.assertEqual(self.hits("identity.json", lines), [(i, "secrets", "error") for i in range(1, 7)])

    def test_the_field_must_be_a_key_with_a_real_value(self):
        clean = ['"privateKey": "${PRIVATE_KEY}"', f'"private_key_id": "{self.KEY}"', '"privateKey": ""', 'privateKey = loadKey("file")',
                 '"privateKey": "abc"', f'"privateKeyPath": "/etc/ssl/{self.KEY}"', '"private_key": "<paste the key>"',
                 '"privateKey": "your-private-key-goes-here-and-it-is-long-enough"', "privateKey: !vault |"]
        self.assertEqual(self.hits("identity.json", clean), [])

    def test_yaml_python_and_powershell_forms(self):
        self.assertEqual(self.hits("a.yml", [f"private_key: {self.KEY}"]), [(1, "secrets", "error")])
        self.assertEqual(self.hits("a.py", [f'private_key = "{self.KEY}"']), [(1, "secrets", "error")])
        self.assertEqual(self.hits("a.ps1", [f'$privateKey = "{self.KEY}"']), [(1, "secrets", "error")])
        self.assertEqual(self.hits("a.py", ["private_key = load_key(path)", "private_key = open(p).read()"]), [])

    def test_a_pem_header_is_an_error_in_every_scanned_file_type(self):
        for header in ("RSA PRIVATE KEY", "EC PRIVATE KEY", "OPENSSH PRIVATE KEY", "PRIVATE KEY", "ENCRYPTED PRIVATE KEY"):
            line = "-----BEGIN " + header + "-----"
            for name in ("k.json", "k.txt", "k.md", "k.yml", "k.py"):
                self.assertEqual(self.hits(name, [line]), [(1, "secrets", "error")], (name, header))

    def test_a_public_key_or_certificate_header_is_not_a_secret(self):
        for header in ("PUBLIC KEY", "CERTIFICATE", "RSA PUBLIC KEY"):
            self.assertEqual(self.hits("k.json", ["-----BEGIN " + header + "-----"]), [])

    def test_the_value_and_the_key_material_are_never_printed(self):
        root = fixture_copy()
        (root / "identity.json").write_text('{"private_key": "' + self.KEY + '"}' + chr(10), encoding="utf-8")
        _, found = lint(root, ("secrets",), secret_scan_extensions=[".json"])
        self.assertEqual(len(found), 1)
        self.assertNotIn(self.KEY[:12], json.dumps([f.__dict__ for f in found]))

    def test_json_is_in_the_suggested_list_and_counts_as_a_script_not_scanned(self):
        self.assertIn(".json", kb_lint.SCRIPT_SUGGESTION)
        root = fixture_copy()
        (root / "identity.json").write_text("{}", encoding="utf-8")
        self.assertNotIn("scripts not scanned", StatsAndCliTests.run_cli(None, "--root", str(root))[1])   # scanned by default now
        cfg = json.loads((root / "kb.config.json").read_text(encoding="utf-8"))
        cfg["secret_scan_extensions"] = [".md"]
        (root / "kb.config.json").write_text(json.dumps(cfg), encoding="utf-8")
        out = StatsAndCliTests.run_cli(None, "--root", str(root))[1]
        self.assertIn("scripts not scanned (1 script files found", out)
        phase0 = (HERE.parent / "BOOTSTRAP-PROMPT.md").read_text(encoding="utf-8").split("**Phase 1")[0]
        self.assertIn(".json", phase0)


class HandledFindingDocsTests(unittest.TestCase):
    def test_guide_shows_how_to_exclude_a_handled_finding_with_a_reason_and_a_date(self):
        guide = (HERE.parent / "GUIDE.md").read_text(encoding="utf-8")
        section = guide.split("## Files that contain example secrets on purpose")[1].split("## Security findings during install")[0]
        for phrase in ("a finding that was handled", "do not remove it from the scan silently", "reason and the date", "handled YYYY-MM-DD",
                       "structure only"):
            self.assertIn(phrase, section, phrase)

    def test_tools_readme_mentions_private_key_fields_and_pem_headers(self):
        readme = (HERE.parent / "tools" / "README.md").read_text(encoding="utf-8")
        for phrase in ("privateKey", "private_key", "PEM", "client_secret"):
            self.assertIn(phrase, readme, phrase)


class Stage4DocsTests(unittest.TestCase):
    KIT = HERE.parent

    def read(self, rel):
        return (self.KIT / rel).read_text(encoding="utf-8")

    def table(self, text, header_start):
        lines = text.split("\n")
        start = next(i for i, ln in enumerate(lines) if ln.startswith(header_start))
        rows = []
        for ln in lines[start:]:
            if not ln.startswith("|"):
                break
            rows.append([c.strip() for c in ln.strip().strip("|").split("|")])
        return rows

    # --- 1. scope and the trap check
    def test_query_test_limits_the_accuracy_part_to_migrated_folders_and_says_how_to_tell_the_session(self):
        text = self.read("templates/QUERY-TEST.md")
        for phrase in ("Scope: the accuracy part is asked only about folders that are already migrated",
                       "Answer only from these folders: <list>", "out of scope", "not found in the vault",
                       "The \"before\" part", "folders that are **not** migrated yet", "Record the scope you gave in the Runs table"):
            self.assertIn(phrase, text, phrase)

    def test_the_trap_is_checked_by_searching_the_whole_folder_before_the_run(self):
        text = self.read("templates/QUERY-TEST.md")
        for phrase in ("Check the trap before the run", "searches the whole folder", "also in other spellings",
                       "it is not a trap: replace the question", "trap checked YYYY-MM-DD"):
            self.assertIn(phrase, text, phrase)

    def test_prompt_phase_four_carries_scope_trap_check_conflicts_and_measurement(self):
        phase4 = self.read("BOOTSTRAP-PROMPT.md").split("**Phase 4")[1].split("**Phase 5")[0]
        for phrase in ("only about folders that are already migrated", "I tell you the folders in scope", "out of scope",
                       "I search the whole folder before the run", "CONFLICTS.md", "claim_status: mixed", "record the session id",
                       "kb_querystats.py --session", "--no-paths --include-bash"):
            self.assertIn(phrase, phase4, phrase)

    # --- 2. conflicts
    def test_conflicts_template_has_the_columns_the_rules_and_a_closed_section(self):
        text = self.read("templates/CONFLICTS.md")
        self.assertTrue(text.startswith("---\n"))
        fm = kb.parse_frontmatter(text)
        self.assertEqual((fm["type"], fm["status"]), ("index", "active"))
        header = self.table(text, "| # | Topic | Source A")[0]
        self.assertEqual(header, ["#", "Topic", "Source A (path)", "Source B (path)", "What A says", "What B says", "Who decides", "Noticed (date)"])
        for row in self.table(text, "| # | Topic | Source A"):
            self.assertEqual(len(row), len(header))
        for phrase in ("## Open", "## Closed", "Do not choose silently", "claim_status: mixed", "never paste a secret",
                       "frontmatter only", "Short paraphrase, not a copy", "Someone decides"):
            self.assertIn(phrase, text, phrase)

    def test_agent_rules_prompt_and_guide_all_tell_the_agent_what_to_do_with_a_conflict(self):
        rules = self.read("templates/AGENT-RULES.md")
        self.assertIn("If two sources disagree, do not pick one silently", rules)
        self.assertIn("_system/CONFLICTS.md", rules)
        self.assertIn("claim_status: mixed", rules)
        self.assertIn("`_system/CONFLICTS.md` from `<KIT>/templates/CONFLICTS.md`", self.read("BOOTSTRAP-PROMPT.md").split("**Phase 4")[0])
        guide = self.read("GUIDE.md")
        self.assertIn("## When two sources disagree", guide)
        section = guide.split("## When two sources disagree")[1].split("## ")[0]
        for phrase in ("must not choose one silently", "source A and source B", "who decides", "claim_status: mixed", "Closed", "answer that gives both versions"):
            self.assertIn(phrase, section, phrase)
        self.assertIn("conflict", self.read("templates/QUERY-TEST.md").split("9. ")[1].split("## Runs")[0])

    # --- 3. writing notes the brain can answer from
    def test_guide_has_the_dated_action_rule(self):
        guide = self.read("GUIDE.md")
        self.assertIn("## Writing notes the brain can answer from", guide)
        section = guide.split("## Writing notes the brain can answer from")[1].split("## When two sources disagree")[0]
        for phrase in ("Anything that changes gets a dated log line, with date and time", "YYYY-MM-DD HH:MM", "An upload, a message sent, a decision",
                       "\"What was the last one?\" can be answered only if such a record exists", "Use absolute dates", "One fact, one place",
                       "modification date", "append-only"):
            self.assertIn(phrase, section, phrase)

    # --- 4. session id and the measurement instruction
    def test_query_test_has_a_runs_table_with_the_session_id_and_the_measuring_command(self):
        text = self.read("templates/QUERY-TEST.md")
        rows = self.table(text, "| Run (before / after) | Session id")
        header = rows[0]
        self.assertEqual(header, ["Run (before / after)", "Session id", "Date and time (from the clock)", "Model", "Scope (folders)", "Measured with kb_querystats"])
        self.assertEqual([r[0] for r in rows[2:]], ["before", "after"])
        self.assertTrue(all(len(r) == len(header) for r in rows))
        for phrase in ("name (without `.jsonl`) of the session transcript file", "python -I _system/tools/kb_querystats.py --session <path to the session .jsonl> --no-paths --include-bash",
                       "the agent does not report this about itself", "that run's own fresh session", "it is a heuristic"):
            self.assertIn(phrase, text, phrase)

    def test_the_command_in_the_template_is_accepted_by_the_tool(self):
        code, out, err = run_querystats("--session", str(HERE / "fixtures" / "sessions" / "sample-session.jsonl"), "--no-paths", "--include-bash")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("kb_querystats:", out)

    def test_existing_results_tables_still_have_their_shape(self):
        rows = self.table(self.read("templates/QUERY-TEST.md"), "| # | Question |")
        self.assertEqual(len(rows), 22)           # header, separator, 20 rows
        self.assertTrue(all(len(r) == len(rows[0]) for r in rows))

    def test_version_is_1_8(self):
        self.assertEqual(kb.KIT_VERSION, "1.8.1")


def run_querystats(*args):
    import kb_querystats
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = kb_querystats.main(list(args))
    return code, out.getvalue(), err.getvalue()


class SecretInCaptureTests(unittest.TestCase):
    EXT = [".md", ".json", ".jsonl", ".txt"]

    def findings(self, root, **cfg):
        cfg.setdefault("secret_scan_extensions", self.EXT)
        _, found = lint(root, ("secrets",), **cfg)
        return sorted((f.path, f.line, f.check, f.severity) for f in found)

    def big_json(self, root, name, size=1_100_000, with_key=True, key_line=1):
        lines = ['{"entry": "' + "x" * 80 + '"}'] * (size // 90)
        if with_key:
            lines.insert(key_line - 1, '{"token": "' + FAKE_KEY + '"}')
        (root / name).write_text(chr(10).join(lines) + chr(10), encoding="utf-8")

    def test_a_finding_in_a_json_file_over_1_mb_is_a_warning_not_an_error(self):
        root = fixture_copy()
        self.big_json(root, "export.json")
        self.assertEqual(self.findings(root), [("export.json", 1, "secret-in-capture", "warning")])

    def test_the_same_finding_in_a_small_json_file_stays_an_error(self):
        root = fixture_copy()
        (root / "settings.json").write_text('{"token": "' + FAKE_KEY + '"}' + chr(10), encoding="utf-8")
        self.assertEqual(self.findings(root), [("settings.json", 1, "secrets", "error")])

    def test_one_very_long_json_line_is_captured_data_in_an_otherwise_small_file(self):
        root = fixture_copy()
        long_line = '{"a": "' + "y" * 5200 + '", "token": "' + FAKE_KEY + '"}'
        (root / "dump.json").write_text('{"token": "' + FAKE_KEY + '"}' + chr(10) + long_line + chr(10), encoding="utf-8")
        self.assertEqual(self.findings(root), [("dump.json", 1, "secrets", "error"), ("dump.json", 2, "secret-in-capture", "warning")])

    def test_the_limits_are_exact_and_only_json_family_files_are_affected(self):
        root = fixture_copy()
        edge = '{"token": "' + FAKE_KEY + '", "pad": "' + "z" * 100 + '"}'
        (root / "edge.json").write_text(edge + chr(10), encoding="utf-8")
        pad = 5000 - len('{"token": "' + FAKE_KEY + '", "pad": ""}')
        exact = '{"token": "' + FAKE_KEY + '", "pad": "' + "q" * pad + '"}'
        self.assertEqual(len(exact), 5000)
        (root / "exact.json").write_text(exact + chr(10), encoding="utf-8")
        (root / "long.txt").write_text("token " + FAKE_KEY + " " + "w" * 6000 + chr(10), encoding="utf-8")
        self.assertEqual(self.findings(root), [("edge.json", 1, "secrets", "error"), ("exact.json", 1, "secrets", "error"),
                                               ("long.txt", 1, "secrets", "error")])
        (root / "exact.json").write_text(exact.replace('"pad": "', '"pad": "q') + chr(10), encoding="utf-8")
        self.assertEqual([f for f in self.findings(root) if f[0] == "exact.json"], [("exact.json", 1, "secret-in-capture", "warning")])

    def test_jsonl_logs_count_too(self):
        root = fixture_copy()
        self.big_json(root, "events.jsonl")
        self.assertEqual(self.findings(root), [("events.jsonl", 1, "secret-in-capture", "warning")])

    def test_capture_dirs_turn_every_finding_in_them_into_a_warning_whatever_the_file_type(self):
        root = fixture_copy()
        for rel in ("reports/run.txt", "reports/deep/note.md", "keep/note.md"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(FAKE_KEY + chr(10), encoding="utf-8")
        got = self.findings(root, capture_dirs=["reports/"])
        self.assertEqual(got, [("keep/note.md", 1, "secrets", "error"), ("reports/deep/note.md", 1, "secret-in-capture", "warning"),
                               ("reports/run.txt", 1, "secret-in-capture", "warning")])
        self.assertEqual(self.findings(root), [("keep/note.md", 1, "secrets", "error"), ("reports/deep/note.md", 1, "secrets", "error"),
                                               ("reports/run.txt", 1, "secrets", "error")])

    def test_capture_dirs_follow_the_same_matching_rules_as_exempt_dirs(self):
        root = fixture_copy()
        for rel in ("a/archive/x.txt", "archive-2020/y.txt", "z/archive/deep/w.txt"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(FAKE_KEY + chr(10), encoding="utf-8")
        got = {f[0]: f[2] for f in self.findings(root, capture_dirs=["archive/", "archive-*/"])}
        self.assertEqual(got, {"a/archive/x.txt": "secret-in-capture", "archive-2020/y.txt": "secret-in-capture",
                               "z/archive/deep/w.txt": "secret-in-capture"})

    def test_a_private_key_block_is_always_an_error_even_in_captured_data(self):
        root = fixture_copy()
        self.big_json(root, "export.json", with_key=False)
        text = (root / "export.json").read_text(encoding="utf-8")
        (root / "export.json").write_text(FAKE_PEM + chr(10) + text, encoding="utf-8")
        (root / "reports").mkdir()
        (root / "reports" / "k.txt").write_text(FAKE_PEM + chr(10), encoding="utf-8")
        got = self.findings(root, capture_dirs=["reports/"])
        self.assertEqual(got, [("export.json", 1, "secrets", "error"), ("reports/k.txt", 1, "secrets", "error")])

    def test_fenced_examples_in_a_captured_note_stay_examples(self):
        root = fixture_copy()
        (root / "reports").mkdir()
        (root / "reports" / "r.md").write_text("```" + chr(10) + FAKE_KEY + chr(10) + "```" + chr(10) + FAKE_KEY + chr(10), encoding="utf-8")
        got = self.findings(root, capture_dirs=["reports/"])
        self.assertEqual(got, [("reports/r.md", 2, "secret-in-example", "warning"), ("reports/r.md", 4, "secret-in-capture", "warning")])

    def test_captures_do_not_fail_the_exit_code_unless_strict_and_are_counted_per_folder(self):
        root = fixture_copy()
        for p in list(root.rglob("*.md")):
            p.unlink()
        (root / "kb.config.json").write_text(json.dumps({"secret_scan_extensions": [".json"]}), encoding="utf-8")
        (root / "N.md").write_text("[[N]]", encoding="utf-8")
        (root / "exports").mkdir(exist_ok=True)
        self.big_json(root, "exports/big.json")
        self.assertEqual(StatsAndCliTests.run_cli(None, "--root", str(root), "--checks", "secrets")[0], 0)
        self.assertEqual(StatsAndCliTests.run_cli(None, "--root", str(root), "--checks", "secrets", "--strict")[0], 1)
        vault, found = lint(root, ("secrets",), secret_scan_extensions=[".json"])
        rows = {r["folder"]: r for r in kb_lint.folder_breakdown(vault, found, 1)}
        self.assertEqual(rows["exports"]["secret_captures"], 1)
        self.assertEqual(rows["exports"]["secrets"], 0)

    def test_the_stats_name_the_new_finding_kind(self):
        root = fixture_copy()
        self.big_json(root, "export.json")
        cfg = json.loads((root / "kb.config.json").read_text(encoding="utf-8"))
        cfg["secret_scan_extensions"] = [".md"]
        (root / "kb.config.json").write_text(json.dumps(cfg), encoding="utf-8")
        stats = json.loads(StatsAndCliTests.run_cli(None, "--root", str(root), "--stats", "--json")[1])["stats"]
        self.assertEqual(stats["findings_by_check"].get("secret-in-capture", 0), 0)  # notes only
        cfg["secret_scan_extensions"] = [".md", ".json"]
        (root / "kb.config.json").write_text(json.dumps(cfg), encoding="utf-8")
        stats = json.loads(StatsAndCliTests.run_cli(None, "--root", str(root), "--stats", "--json")[1])["stats"]
        self.assertEqual(stats["findings_by_check"]["secret-in-capture"], 1)

    def test_unused_capture_dirs_entries_are_reported_like_other_config_entries(self):
        root = fixture_copy()
        cfg = json.loads((root / "kb.config.json").read_text(encoding="utf-8"))
        cfg["capture_dirs"] = ["reports/", "nowhere/"]
        (root / "kb.config.json").write_text(json.dumps(cfg), encoding="utf-8")
        (root / "reports").mkdir()
        (root / "reports" / "r.md").write_text("x", encoding="utf-8")
        _, found = lint(root, kb_lint.CHECKS)
        self.assertEqual([f.message for f in found if f.check == "config"], ["capture_dirs entry matches nothing: nowhere/"])

    def test_capture_dirs_must_be_a_list_of_strings(self):
        root = fixture_copy()
        (root / "kb.config.json").write_text('{"capture_dirs": "reports/"}', encoding="utf-8")
        self.assertEqual(StatsAndCliTests.run_cli(None, "--root", str(root))[0], 2)

    def test_the_value_is_never_printed_for_captured_findings(self):
        root = fixture_copy()
        self.big_json(root, "export.json")
        _, found = lint(root, ("secrets",), secret_scan_extensions=[".json"])
        self.assertEqual(len(found), 1)
        self.assertNotIn(FAKE_KEY[-12:], json.dumps([f.__dict__ for f in found]))


class OversizeFilesTests(unittest.TestCase):
    def test_files_over_the_cap_are_not_read_and_the_summary_says_so(self):
        root = fixture_copy()
        (root / "huge.json").write_bytes(b"x" * 200_000)
        with mock.patch.object(kb_lint, "MAX_SCAN_BYTES", 100_000):
            cfg = json.loads((root / "kb.config.json").read_text(encoding="utf-8"))
            cfg["secret_scan_extensions"] = [".md", ".json"]
            (root / "kb.config.json").write_text(json.dumps(cfg), encoding="utf-8")
            code, out, _ = StatsAndCliTests.run_cli(None, "--root", str(root), "--checks", "secrets")
            stats = json.loads(StatsAndCliTests.run_cli(None, "--root", str(root), "--stats", "--json")[1])["stats"]
        self.assertIn("secret scan skipped 1 files over", out)
        self.assertEqual(stats["secret_scan_skipped_oversize_files"], 1)

    def test_the_default_cap_is_large_enough_for_logs_and_exports(self):
        self.assertEqual(kb_lint.MAX_SCAN_BYTES, 20_000_000)
        self.assertEqual((kb_lint.CAPTURE_BYTES, kb_lint.CAPTURE_LINE), (1_000_000, 5_000))


class FencedExampleLanguageTests(unittest.TestCase):
    LANGS = ["bash", "sh", "shell", "powershell", "ps1", "pwsh", "console", "terminal", "zsh", "cmd", "bat", "json", "yaml", "python", "js", "text", ""]

    def kinds(self, text):
        root = fixture_copy()
        (root / "Doc.md").write_text(text, encoding="utf-8")
        _, found = lint(root, ("secrets",))
        return sorted((f.line, f.check, f.severity) for f in found if f.path == "Doc.md")

    def test_a_secret_in_a_code_block_of_any_language_is_only_the_example_warning(self):
        for lang in self.LANGS:
            text = "```" + lang + chr(10) + "export TOKEN=" + FAKE_KEY + chr(10) + "```" + chr(10)
            self.assertEqual(self.kinds(text), [(2, "secret-in-example", "warning")], lang or "no language")

    def test_powershell_and_bash_literals_in_fenced_blocks(self):
        text = "\n".join(["```powershell", '$password = "' + "Zx9" * 8 + '"', "```", "```bash", 'export API_TOKEN="' + "abcd1234efgh5678ijkl" + '"', "```"])
        self.assertEqual(self.kinds(text), [(2, "secret-in-example", "warning"), (5, "secret-in-example", "warning")])

    def test_function_calls_in_fenced_blocks_are_not_reported_at_all(self):
        key = "TO" + "KEN"
        text = "\n".join(["```powershell", "$" + key.lower() + " = Get-AccessTokenInteractive -Tenant $t", "```", "```bash", key + "=$(az account get-access-token)", "```"])
        self.assertEqual(self.kinds(text), [])

    def test_info_strings_longer_fences_tildes_indentation_and_quotes(self):
        cases = {
            "info string": "```bash title=run.sh" + chr(10) + FAKE_KEY + chr(10) + "```",
            "longer fence": "````console" + chr(10) + FAKE_KEY + chr(10) + "````",
            "tildes": "~~~powershell" + chr(10) + FAKE_KEY + chr(10) + "~~~",
            "indented in a list": "- step" + chr(10) + "  ```bash" + chr(10) + "  " + FAKE_KEY + chr(10) + "  ```",
            "inside a quote": "> ```bash" + chr(10) + "> " + FAKE_KEY + chr(10) + "> ```",
            "inside a callout": "> [!example]" + chr(10) + "> ```powershell" + chr(10) + "> " + FAKE_KEY + chr(10) + "> ```",
            "nested quote": ">> ```sh" + chr(10) + ">> " + FAKE_KEY + chr(10) + ">> ```",
        }
        for name, text in cases.items():
            got = self.kinds(text)
            self.assertEqual([k[1:] for k in got], [("secret-in-example", "warning")], name)

    def test_text_after_the_closing_fence_is_a_normal_error(self):
        text = "```bash" + chr(10) + FAKE_KEY + chr(10) + "```" + chr(10) + FAKE_KEY + chr(10)
        self.assertEqual(self.kinds(text), [(2, "secret-in-example", "warning"), (4, "secrets", "error")])

    def test_links_inside_a_quoted_code_block_are_not_links(self):
        root = fixture_copy()
        (root / "Doc.md").write_text("> ```bash" + chr(10) + "> see [[Nowhere At All]]" + chr(10) + "> ```" + chr(10) + "[[Home]]" + chr(10), encoding="utf-8")
        _, found = lint(root, ("links",))
        self.assertEqual([f for f in found if f.path == "Doc.md"], [])

    def test_unclosed_fence_marks_the_rest_of_the_note_as_example(self):
        text = "```bash" + chr(10) + FAKE_KEY + chr(10) + FAKE_KEY + chr(10)
        self.assertEqual(self.kinds(text), [(2, "secret-in-example", "warning"), (3, "secret-in-example", "warning")])


class Stage5DocsTests(unittest.TestCase):
    KIT = HERE.parent

    def read(self, rel):
        return (self.KIT / rel).read_text(encoding="utf-8")

    def test_query_test_asks_for_one_question_per_message(self):
        text = self.read("templates/QUERY-TEST.md").split("## Runs")[0]
        for phrase in ("**One question per message.**", "wait for the answer before the next", "one message as one question", "--split-numbered"):
            self.assertIn(phrase, text, phrase)

    def test_prompt_phase_four_says_one_question_per_message(self):
        phase4 = self.read("BOOTSTRAP-PROMPT.md").split("**Phase 4")[1].split("**Phase 5")[0]
        self.assertIn("I ask **one question per message**", phase4)

    def test_guide_covers_one_question_logs_exports_and_example_blocks(self):
        guide = self.read("GUIDE.md")
        self.assertIn("**Send one question per message**", guide)
        self.assertIn("--split-numbered", guide)
        section = guide.split("## Logs and exports")[1].split("## Files that contain example secrets on purpose")[0]
        for phrase in ("secret-in-capture", "capture_dirs", "1 MB", "5,000 characters", "always an error", "privacy and retention matter", "--no-paths"):
            self.assertIn(phrase, section, phrase)
        self.assertIn("`secret-in-example` (warning)", guide)
        self.assertIn("`powershell`", guide)

    def test_tools_readme_documents_the_new_finding_and_the_flag(self):
        text = self.read("tools/README.md")
        for phrase in ("| `secret-in-capture` | warning |", "`capture_dirs`", "20 MB", "secret_scan_skipped_oversize_files",
                       "> ```bash", "`--split-numbered`", "several answers in one message", "(after last answer)", "Neither the questions nor the answers are ever printed"):
            self.assertIn(phrase, text, phrase)

    def test_the_example_config_lists_capture_dirs_and_still_loads(self):
        cfg = json.loads(self.read("kb.config.example.json"))
        self.assertEqual(cfg["capture_dirs"], [])
        self.assertIn("secret-in-capture", cfg["_capture_dirs"])
        self.assertIn("capture_dirs", kb.DEFAULTS)

    def test_split_numbered_is_a_documented_option_of_the_tool(self):
        import kb_querystats
        self.assertIn("--split-numbered", kb_querystats.__doc__)


class ByteOrderMarkTests(unittest.TestCase):
    """A file may start with a UTF-8 BOM, or be UTF-16 with a BOM (Windows PowerShell 5 writes that with > and Out-File)."""

    @staticmethod
    def rewrite(path, how):
        text = path.read_bytes().decode("utf-8").lstrip(kb.BOM)
        if how == "utf8-bom":
            data = kb.UTF8_BOM + text.encode("utf-8")
        elif how == "utf16-le":
            data = b"\xff\xfe" + text.encode("utf-16-le")
        else:
            data = b"\xfe\xff" + text.encode("utf-16-be")
        path.write_bytes(data)

    def signature(self, root, **cfg):
        vault, found = lint(root, kb_lint.CHECKS, schema=True, **cfg)
        return sorted((f.check, f.severity, f.path, f.line, f.message) for f in found), len(vault.notes)

    def test_every_check_gives_the_same_result_for_bom_and_utf16_notes(self):
        base = self.signature(fixture_copy())
        for how in ("utf8-bom", "utf16-le", "utf16-be"):
            root = fixture_copy()
            for note in root.rglob("*.md"):
                self.rewrite(note, how)
            self.assertEqual(self.signature(root), base, how)

    def test_secrets_in_json_files_with_a_bom_are_found_on_the_right_line(self):
        for how in ("utf8-bom", "utf16-le", "utf16-be"):
            root = fixture_copy()
            target = root / "settings.json"
            target.write_text(chr(10).join(['{', '  "name": "x",', '  "token": "' + FAKE_KEY + '"', '}']) + chr(10), encoding="utf-8")
            self.rewrite(target, how)
            _, found = lint(root, ("secrets",), secret_scan_extensions=[".md", ".json"])
            self.assertEqual([(f.path, f.line, f.check) for f in found], [("settings.json", 3, "secrets")], how)

    def test_a_key_field_on_the_first_line_of_a_bom_file_is_found(self):
        root = fixture_copy()
        target = root / "identity.json"
        target.write_text('{"privateKey": "' + "A" * 60 + '"}', encoding="utf-8")
        self.rewrite(target, "utf8-bom")
        _, found = lint(root, ("secrets",), secret_scan_extensions=[".md", ".json"])
        self.assertEqual([(f.path, f.line, f.check) for f in found], [("identity.json", 1, "secrets")])

    def test_the_config_file_may_have_a_bom_or_be_utf16(self):
        for how in ("utf8-bom", "utf16-le"):
            root = fixture_copy()
            cfg = root / "kb.config.json"
            cfg.write_text(json.dumps({"capture_dirs": ["reports/"]}), encoding="utf-8")
            self.rewrite(cfg, how)
            loaded, _ = kb.load_config(root)
            self.assertEqual(loaded["capture_dirs"], ["reports/"], how)

    def test_decode_text_and_read_text(self):
        self.assertEqual(kb.decode_text(b"\xef\xbb\xbfabc"), "abc")
        self.assertEqual(kb.decode_text(b"\xff\xfe" + "aé".encode("utf-16-le")), "aé")
        self.assertEqual(kb.decode_text("aé".encode("utf-8")), "aé")
        self.assertTrue(kb.is_utf16(b"\xff\xfea") and not kb.is_utf16(b"\xef\xbb\xbfa"))

    def test_the_index_is_the_same_for_a_bom_vault(self):
        import kb_index
        outputs = []
        for how in (None, "utf8-bom", "utf16-le"):
            root = fixture_copy()
            if how:
                for note in root.rglob("*.md"):
                    self.rewrite(note, how)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                kb_index.main(["--root", str(root)])
            outputs.append(out.getvalue())
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0], outputs[2])


class ExpiringExclusionTests(unittest.TestCase):
    PAST, FUTURE = "2000-01-01", "2999-12-31"

    def setup_vault(self, exclude, **extra):
        root = fixture_copy()
        (root / "dump").mkdir()
        (root / "dump" / "notes.md").write_text("token " + FAKE_KEY + chr(10), encoding="utf-8")
        cfg = {"secret_scan_exclude": exclude}
        cfg.update(extra)
        (root / "kb.config.json").write_text(json.dumps(cfg), encoding="utf-8")
        return root

    def findings(self, root, checks=("secrets", "config")):
        _, found = lint(root, checks)
        return sorted((f.check, f.severity, f.path, f.message) for f in found if f.check != "orphans")

    def test_the_old_form_still_excludes_without_an_end(self):
        root = self.setup_vault({"dump/": "tool output with example keys"})
        self.assertEqual(self.findings(root), [])

    def test_an_exclusion_with_a_future_date_applies(self):
        for form in ({"dump/": {"reason": "public sign-up token", "until": self.FUTURE}},
                     [{"path": "dump/", "reason": "public sign-up token", "until": self.FUTURE}]):
            self.assertEqual(self.findings(self.setup_vault(form)), [], form)

    def test_an_expired_exclusion_brings_the_finding_back_as_an_error_that_says_so(self):
        root = self.setup_vault({"dump/": {"reason": "public sign-up token", "until": self.PAST}})
        got = self.findings(root)
        secrets = [g for g in got if g[0] == "secrets"]
        self.assertEqual(len(secrets), 1)
        self.assertEqual(secrets[0][1:3], ("error", "dump/notes.md"))
        self.assertIn("exclusion expired 2000-01-01: public sign-up token", secrets[0][3])
        self.assertNotIn(FAKE_KEY[-12:], secrets[0][3])
        config = [g for g in got if g[0] == "config"]
        self.assertEqual(len(config), 1)
        self.assertIn("secret_scan_exclude entry expired 2000-01-01: dump/", config[0][3])

    def test_the_last_day_is_included(self):
        root = self.setup_vault({"dump/": {"reason": "r", "until": "2030-01-01"}})
        with mock.patch.object(kb, "today_iso", return_value="2030-01-01"):
            self.assertEqual(self.findings(root), [])
        with mock.patch.object(kb, "today_iso", return_value="2030-01-02"):
            self.assertEqual([g[0] for g in self.findings(root)], ["config", "secrets"])

    def test_key_files_come_back_too_with_the_same_note(self):
        root = self.setup_vault({"keys/": {"reason": "public key pair for a demo", "until": self.PAST}})
        (root / "keys").mkdir()
        (root / "keys" / "demo.pem").write_bytes(b"x")
        found = [g for g in self.findings(root) if g[0] == "secret-file"]
        self.assertEqual(len(found), 1)
        self.assertIn("exclusion expired 2000-01-01: public key pair for a demo", found[0][3])

    def test_a_valid_exclusion_that_also_covers_the_file_keeps_it_excluded(self):
        root = self.setup_vault({"dump/": {"reason": "old", "until": self.PAST}, "dump/notes.md": "this exact file is an example"})
        self.assertEqual([g for g in self.findings(root) if g[0] != "config"], [])

    def test_captured_and_example_warnings_also_say_the_exclusion_expired(self):
        root = self.setup_vault({"reports/": {"reason": "export", "until": self.PAST}}, capture_dirs=["reports/"])
        (root / "reports").mkdir()
        (root / "reports" / "run.md").write_text(FAKE_KEY + chr(10), encoding="utf-8")
        got = [g for g in self.findings(root) if g[0] == "secret-in-capture"]
        self.assertEqual(len(got), 1)
        self.assertIn("exclusion expired", got[0][3])

    def test_bad_values_are_refused_with_exit_code_2(self):
        bad = [{"dump/": {"reason": "r", "until": "2026-13-45"}}, {"dump/": {"reason": "r", "until": "tomorrow"}},
               {"dump/": {"reason": "r", "until": 20301231}}, {"dump/": {"reason": "r", "until": "2030-1-1"}},
               {"dump/": {"until": "2030-01-01"}}, {"dump/": {"reason": "  ", "until": "2030-01-01"}},
               {"dump/": {"reason": "r", "expires": "2030-01-01"}}, [{"reason": "r"}], ["dump/"], "dump/", {"dump/": ""}]
        for value in bad:
            root = self.setup_vault(value)
            self.assertEqual(StatsAndCliTests.run_cli(None, "--root", str(root), "--checks", "secrets")[0], 2, value)

    def test_the_config_check_warns_about_an_entry_with_a_date_that_matches_nothing(self):
        root = self.setup_vault({"nowhere/": {"reason": "r", "until": self.FUTURE}})
        got = [g for g in self.findings(root) if g[0] == "config"]
        self.assertEqual([g[3] for g in got], ["secret_scan_exclude entry matches nothing: nowhere/"])

    def test_the_example_config_documents_the_date_form(self):
        comment = json.loads((HERE.parent / "kb.config.example.json").read_text(encoding="utf-8"))["_secret_scan_exclude"]
        self.assertIn('"until"', comment)
        self.assertIn("exclusion expired", comment)
        kb.load_config(HERE.parent, HERE.parent / "kb.config.example.json")


class Stage6DocsTests(unittest.TestCase):
    KIT = HERE.parent

    def read(self, rel):
        return (self.KIT / rel).read_text(encoding="utf-8")

    def test_agent_rules_are_scoped_and_light(self):
        rules = self.read("templates/AGENT-RULES.md")
        for phrase in ("**Only when you answer a question from the notes**", "do not read the index for other work", "shared (several agents or projects use this root)",
                       "only to the notes folders listed in `required_paths`", "**only on the folders in `required_paths`**", "--schema --path <folder>",
                       "never a full-vault lint before every report"):
            self.assertIn(phrase, rules, phrase)

    def test_prompt_phase_three_puts_the_rules_in_only_then_and_for_a_shared_root(self):
        phase3 = self.read("BOOTSTRAP-PROMPT.md").split("**Phase 3")[1].split("**Phase 4")[0]
        for phrase in ("**only in this phase, never earlier**", "do not exist before it", "shared by several agents or projects",
                       "only when answering a question from the notes", "lint only the folders in `required_paths`"):
            self.assertIn(phrase, phase3, phrase)
        early = self.read("BOOTSTRAP-PROMPT.md").split("**Phase 1")[1].split("**Phase 3")[0]
        self.assertIn("do not write them to the `CLAUDE.md` at the vault root yet", early)
        self.assertIn("exist only after Phase 3", early)
        self.assertIn("local `CLAUDE.md` inside the pilot folder only", early)

    def test_query_test_and_prompt_warn_that_agent_memory_can_leak_answers(self):
        qt = self.read("templates/QUERY-TEST.md").split("## Runs")[0]
        for phrase in ("**Agent memory can leak answers.**", "auto-memory", "note path for every answer", "from memory", "counts as wrong", "memory turned off"):
            self.assertIn(phrase, qt, phrase)
        phase4 = self.read("BOOTSTRAP-PROMPT.md").split("**Phase 4")[1].split("**Phase 5")[0]
        for phrase in ("**Every answer needs the path of a note.**", "auto-memory", "graded wrong"):
            self.assertIn(phrase, phase4, phrase)
        self.assertIn("**Require a note path for every answer**", self.read("GUIDE.md"))

    def test_guide_and_readme_answer_do_i_need_obsidian(self):
        guide = self.read("GUIDE.md")
        section = guide.split("## Do I need Obsidian?")[1].split("## ")[0]
        for phrase in ("**No.**", "any folder of `.md` files", "VS Code, Notepad++", "GitHub or GitLab", "ask your agent", "resolves `[[wikilinks]]` itself", "phase 2b", "never changed"):
            self.assertIn(phrase, section, phrase)
        self.assertIn("## Do I need Obsidian?", self.read("README.md"))

    def test_scan_defaults_are_documented_everywhere_they_are_mentioned(self):
        for rel in ("tools/README.md", "GUIDE.md", "BOOTSTRAP-PROMPT.md"):
            text = self.read(rel)
            self.assertNotIn("reads only notes", text, rel)
            self.assertIn("scripts and JSON", text, rel)
        cfg = json.loads(self.read("kb.config.example.json"))
        self.assertEqual(cfg["secret_scan_extensions"], list(kb.DEFAULT_SCAN_EXTENSIONS))
        self.assertIn("scripts and JSON", cfg["_secret_scan_extensions"])

    def test_index_scope_is_documented_and_in_the_example_config(self):
        cfg = json.loads(self.read("kb.config.example.json"))
        self.assertEqual(cfg["index_scope"], "valid")
        for word in ("valid", "required", "all"):
            self.assertIn(word, cfg["_index_scope"])
        readme = self.read("tools/README.md")
        for phrase in ("`index_scope`", "`valid` (default)", "Left out (valid: ...)"):
            self.assertIn(phrase, readme, phrase)
        self.assertIn("`index_scope`", self.read("GUIDE.md"))

    def test_secure_move_is_documented_in_the_readme_and_the_guide(self):
        readme = self.read("tools/README.md").split("## kb_secure_move")[1].split("## kb_index")[0]
        for phrase in ("does nothing unless you pass `--apply`", "SHA-256", "system trash", "never a permanent delete", "SECURE-MOVE-LOG.md", "who approved",
                       "outside the vault", "COPIED ONLY", "`--approved-by`", "`--trash-dir DIR`", "rotate the key", "symbolic link", "online-only"):
            self.assertIn(phrase, readme, phrase)
        guide = self.read("GUIDE.md").split("## Moving a sensitive file out of the vault")[1].split("## ")[0]
        for phrase in ("kb_secure_move.py", "outside", "SHA-256", "not a permanent delete", "dry run", "rotate the key"):
            self.assertIn(phrase, guide, phrase)
        self.assertIn("`kb_secure_move`", self.read("tools/README.md").split("\n")[2])


class IndexScopeTests(unittest.TestCase):
    """index_scope: valid (default) | required | all."""

    def build(self, root, **cfg_over):
        import kb_index
        cfg, _ = kb.load_config(root)
        cfg.update(cfg_over)
        return kb_index.build(root, cfg)

    def setup_root(self):
        root = fixture_copy()
        cfg = json.loads((root / "kb.config.json").read_text(encoding="utf-8"))
        cfg.pop("index_scope", None)
        (root / "kb.config.json").write_text(json.dumps(cfg), encoding="utf-8")
        (root / "Old").mkdir()
        (root / "Old" / "Legacy.md").write_text("---" + chr(10) + "title: Old thing" + chr(10) + "tags: [x]" + chr(10) + "---" + chr(10) + "body" + chr(10), encoding="utf-8")
        return root

    def titles(self, text):
        return sorted(line.split("|")[1].strip() for line in text.splitlines() if line.startswith("| [[") or line.startswith("| Old"))

    def test_the_default_is_valid_and_lists_only_notes_that_pass_the_schema(self):
        root = self.setup_root()
        self.assertEqual(kb.load_config(root)[0]["index_scope"], "valid")
        text = self.build(root)
        self.assertNotIn("Old/Legacy", text)
        left = [l for l in text.splitlines() if l.startswith("Left out (valid: only notes whose frontmatter passes the schema): ")]
        self.assertEqual(len(left), 1)
        self.assertTrue(left[0].endswith(" notes with frontmatter") and int(left[0].split(": ")[-1].split()[0]) >= 1)
        self.assertIn("Concepts/Caf", text)

    def test_all_lists_everything_with_a_frontmatter_and_says_nothing_was_left_out(self):
        root = self.setup_root()
        text = self.build(root, index_scope="all")
        self.assertIn("Old/Legacy", text)
        self.assertNotIn("Left out", text)

    def test_required_lists_only_notes_under_required_paths(self):
        root = self.setup_root()
        text = self.build(root, index_scope="required")
        self.assertNotIn("Old/Legacy", text)
        self.assertNotIn("Concepts/Caf", text)
        self.assertIn("Projects/", text)
        self.assertIn("Left out (required: only notes under required_paths):", text)

    def test_required_and_valid_are_subsets_of_all(self):
        root = self.setup_root()
        count = lambda t: int(t.split("Notes with frontmatter: ")[1].split(chr(10))[0])
        everything, valid, required = (count(self.build(root, index_scope=s)) for s in ("all", "valid", "required"))
        self.assertGreater(everything, valid)
        self.assertGreater(everything, required)
        self.assertGreaterEqual(required, 1)

    def test_a_note_with_a_bad_enum_value_or_date_is_left_out_of_valid(self):
        root = self.setup_root()
        (root / "Concepts" / "Bad.md").write_text(
            "---" + chr(10) + "title: Bad" + chr(10) + "type: note" + chr(10) + "status: nonsense" + chr(10) + "updated: 2024-13-40" + chr(10) + "---" + chr(10), encoding="utf-8")
        self.assertNotIn("Concepts/Bad", self.build(root))
        self.assertIn("Concepts/Bad", self.build(root, index_scope="all"))

    def test_the_output_is_identical_between_runs_and_check_works(self):
        import kb_index
        root = self.setup_root()
        self.assertEqual(self.build(root), self.build(root))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            kb_index.main(["--root", str(root), "--write"])
            self.assertEqual(kb_index.main(["--root", str(root), "--check"]), 0)

    def test_a_bad_value_is_a_configuration_error(self):
        root = self.setup_root()
        (root / "kb.config.json").write_text('{"index_scope": "everything"}', encoding="utf-8")
        with self.assertRaises(kb.ConfigError):
            kb.load_config(root)
        (root / "kb.config.json").write_text('{"index_scope": 3}', encoding="utf-8")
        with self.assertRaises(kb.ConfigError):
            kb.load_config(root)

    def test_schema_problems_is_what_lint_reports_for_the_same_note(self):
        root = self.setup_root()
        (root / "Concepts" / "Bad.md").write_text("---" + chr(10) + "title: Bad" + chr(10) + "type: wrong" + chr(10) + "---" + chr(10), encoding="utf-8")
        vault, found = lint(root, ("schema",), schema=True)
        cfg = vault.cfg
        mine = sorted(kb.schema_problems(cfg, vault.frontmatter("Concepts/Bad.md")))
        theirs = sorted(f.message for f in found if f.path == "Concepts/Bad.md" and f.check == "schema")
        self.assertEqual(mine, theirs)
        self.assertTrue(mine)


class Stage7DocsTests(unittest.TestCase):
    KIT = HERE.parent

    def read(self, rel):
        return (self.KIT / rel).read_text(encoding="utf-8")

    def test_readme_documents_expiring_exclusions_and_encodings(self):
        text = self.read("tools/README.md")
        section = text.split("## Exclusions that expire")[1].split("## Encodings")[0]
        for phrase in ('"until"', "YYYY-MM-DD", "last day is included", "exclusion expired", "another, still valid, exclusion", "configuration error (exit code 2)"):
            self.assertIn(phrase, section, phrase)
        enc = text.split("## Encodings")[1].split("Every tool answers")[0]
        for phrase in ("UTF-8 BOM", "UTF-16", "Windows PowerShell 5", "skips a UTF-16 note"):
            self.assertIn(phrase, enc, phrase)

    def test_guide_tells_how_to_handle_a_secret_that_is_public_on_purpose(self):
        guide = self.read("GUIDE.md")
        section = guide.split("**A secret that is public on purpose**")[1].split("**The same applies to a finding that was handled**")[0]
        for phrase in ("not a placeholder", "real reason and an end date", "\"until\"", "exclusion expired", "renew the date"):
            self.assertIn(phrase, section, phrase)

    def test_the_root_readme_lists_every_tool(self):
        text = self.read("README.md")
        for tool in ("kb_lint", "kb_index", "kb_frontmatter", "kb_backup", "kb_querystats", "kb_secure_move"):
            self.assertIn(tool, text, tool)
        for tool in ("kb_lint", "kb_index", "kb_frontmatter", "kb_backup", "kb_querystats", "kb_secure_move"):
            self.assertTrue((self.KIT / "tools" / (tool + ".py")).is_file(), tool)


class T18Tests(unittest.TestCase):
    """v1.8.1: path links that work, _system exempt, quiet-exclusion summary, notes without frontmatter, changed-default notes."""

    def cli(self, root, *args):
        return StatsAndCliTests.run_cli(None, "--root", str(root), *args)

    def write_config(self, root, **over):
        cfg = json.loads((root / "kb.config.json").read_text(encoding="utf-8"))
        cfg.update(over)
        (root / "kb.config.json").write_text(json.dumps(cfg), encoding="utf-8")

    # --- 1. path links work from the index file's own folder
    def test_path_style_links_are_relative_markdown_links_that_open_the_notes(self):
        import kb_index
        root = fixture_copy()
        self.write_config(root, index_link_style="markdown", index_output="_system/INDEX.generated.md")
        (root / "Concepts" / "Odd [name] (1).md").write_text(
            "---" + chr(10) + "title: Odd [name]" + chr(10) + "type: note" + chr(10) + "status: draft" + chr(10) + "updated: 2024-01-01" + chr(10) + "---" + chr(10), encoding="utf-8")
        cfg, _ = kb.load_config(root)
        text = kb_index.build(root, cfg)
        self.assertNotIn("[[", text)
        links = re.findall(r"\]\(((?:[^()]|\([^()]*\))*)\)", "\n".join(l for l in text.splitlines() if l.startswith("| [")))
        self.assertTrue(links)
        from urllib.parse import unquote
        for link in links:
            target = (root / "_system" / unquote(link)).resolve()
            self.assertTrue(target.is_file(), link)
        self.assertIn("(../Concepts/Graph%20Theory.md)", text)
        self.assertIn("[Odd \\[name\\]](../Concepts/Odd%20%5Bname%5D%20(1).md)", text)

    def test_the_default_wikilink_style_is_unchanged(self):
        import kb_index
        root = fixture_copy()
        cfg, _ = kb.load_config(root)
        self.assertIn("[[Concepts/Graph Theory\\|", kb_index.build(root, cfg))

    # --- 2. _system is not knowledge
    def test_system_pages_with_placeholders_are_exempt_from_the_schema_and_the_index_by_default(self):
        import kb_index
        root = fixture_copy()
        (root / "_system").mkdir()
        (root / "_system" / "CONFLICTS.md").write_text(
            "---" + chr(10) + "title: Conflicts" + chr(10) + "type: index" + chr(10) + "domain: <your-domain>" + chr(10) + "status: active" + chr(10) + "updated: YYYY-MM-DD" + chr(10) + "---" + chr(10) + "body" + chr(10), encoding="utf-8")
        _, found = lint(root, kb_lint.CHECKS, schema=True)
        self.assertEqual([f for f in found if f.path.startswith("_system/")], [])
        cfg, _ = kb.load_config(root)
        self.assertNotIn("_system/CONFLICTS", kb_index.build(root, cfg))
        self.write_config(root, system_dir_exempt=False)
        _, found = lint(root, kb_lint.CHECKS, schema=True)
        self.assertTrue([f for f in found if f.path == "_system/CONFLICTS.md" and f.check == "schema"])

    def test_system_exemption_is_only_for_the_top_level_system_folder(self):
        root = fixture_copy()
        (root / "Projects" / "_system").mkdir()
        (root / "Projects" / "_system" / "Page.md").write_text("---" + chr(10) + "title: P" + chr(10) + "updated: nope" + chr(10) + "---" + chr(10), encoding="utf-8")
        _, found = lint(root, ("schema",), schema=True)
        self.assertTrue([f for f in found if f.path == "Projects/_system/Page.md"])

    def test_the_prompt_and_guide_tell_the_agent_to_fill_the_placeholders_and_to_ask_about_obsidian(self):
        kit = HERE.parent
        prompt = (kit / "BOOTSTRAP-PROMPT.md").read_text(encoding="utf-8")
        self.assertIn("system_dir_exempt", (kit / "kb.config.example.json").read_text(encoding="utf-8"))
        phase1 = prompt.split("**Phase 1")[1].split("**Phase 2")[0]
        phase3 = prompt.split("**Phase 3")[1].split("**Phase 4")[0]
        for text in (phase1, phase3):
            self.assertIn("every placeholder", text)
        phase0 = prompt.split("**Phase 0")[1].split("**Phase 1")[0]
        for phrase in ("Do you use Obsidian", "index_link_style", "\"markdown\""):
            self.assertIn(phrase, phase0, phrase)

    # --- 5. quiet exclusions are counted in the summary
    def test_the_summary_counts_excluded_files_and_ignored_links(self):
        root = fixture_copy()
        (root / "dump").mkdir()
        (root / "dump" / "a.md").write_text("x", encoding="utf-8")
        (root / "dump" / "b.md").write_text("x", encoding="utf-8")
        self.write_config(root, secret_scan_exclude={"dump/": "exports"})
        code, out, _ = self.cli(root)
        self.assertIn("2 files excluded from secret scan", out)
        self.assertRegex(out, r"\d+ links ignored \(see kb\.config\.json\)")
        stats = json.loads(self.cli(root, "--stats", "--json")[1])["stats"]
        self.assertEqual(stats["secret_scan_excluded_files"], 2)
        self.assertGreaterEqual(stats["links_ignored"], 1)

    def test_nothing_is_said_when_nothing_is_excluded_or_ignored(self):
        root = fixture_copy()
        self.write_config(root, ignore_links=[], secret_scan_exclude={})
        out = self.cli(root, "--checks", "secrets")[1]
        self.assertNotIn("excluded from secret scan", out)
        self.assertNotIn("links ignored", out)

    # --- 6. the index says how many notes have no frontmatter at all
    def test_the_index_counts_notes_without_frontmatter(self):
        import kb_index
        root = fixture_copy()
        (root / "Loose.md").write_text("no frontmatter here" + chr(10), encoding="utf-8")
        (root / "Loose2.md").write_text("none either" + chr(10), encoding="utf-8")
        cfg, _ = kb.load_config(root)
        text = kb_index.build(root, cfg)
        self.assertRegex(text, r"Notes without frontmatter \(not in this index\): \d+")
        shown = int(re.search(r"Notes without frontmatter \(not in this index\): (\d+)", text).group(1))
        self.assertGreaterEqual(shown, 2)
        # a vault whose notes all have frontmatter says nothing
        for p in list(root.rglob("*.md")):
            if "---" not in p.read_text(encoding="utf-8")[:4] and not kb.Vault(root, cfg).is_exempt(p.relative_to(root).as_posix()):
                p.write_text("---" + chr(10) + "title: t" + chr(10) + "type: note" + chr(10) + "status: draft" + chr(10) + "updated: 2024-01-01" + chr(10) + "---" + chr(10), encoding="utf-8")
        self.assertNotIn("Notes without frontmatter", kb_index.build(root, cfg))

    # --- 7. defaults that changed
    def test_a_config_that_does_not_set_the_changed_defaults_gets_a_note_from_the_tool_that_uses_them(self):
        import kb_index
        root = fixture_copy()
        cfg = json.loads((root / "kb.config.json").read_text(encoding="utf-8"))
        cfg.pop("index_scope", None)
        cfg.pop("secret_scan_extensions", None)
        (root / "kb.config.json").write_text(json.dumps(cfg), encoding="utf-8")
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            kb_index.main(["--root", str(root)])
        self.assertIn("note: default changed in 1.8: index_scope is not set in kb.config.json", err.getvalue())
        self.assertNotIn("secret_scan_extensions", err.getvalue())
        code, out, lint_err = self.cli(root)
        self.assertIn("default changed in 1.8: secret_scan_extensions is not set in kb.config.json", lint_err)
        self.assertNotIn("index_scope", lint_err)
        self.assertIn("set it explicitly", lint_err)

    def test_no_note_when_the_keys_are_set_when_there_is_no_config_file_or_the_scan_is_off(self):
        import kb_index
        root = fixture_copy()
        self.write_config(root, index_scope="all", secret_scan_extensions=[".md"])
        self.assertEqual(self.cli(root)[2], "")
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            kb_index.main(["--root", str(root)])
        self.assertEqual(err.getvalue(), "")
        empty = Path(tempfile.mkdtemp(prefix="kbn-"))
        (empty / "A.md").write_text("x", encoding="utf-8")
        self.assertNotIn("default changed", self.cli(empty)[2])
        cfg = json.loads((root / "kb.config.json").read_text(encoding="utf-8"))
        cfg.pop("secret_scan_extensions")
        cfg["secret_scan"] = False
        (root / "kb.config.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.assertNotIn("default changed", self.cli(root)[2])

    def test_the_kit_version_is_1_8_1_in_every_tool(self):
        self.assertEqual(kb.KIT_VERSION, "1.8.1")


class T18ColleagueReportTests(unittest.TestCase):
    KIT = HERE.parent

    def read(self, rel):
        return (self.KIT / rel).read_text(encoding="utf-8")

    def cli(self, root, *args):
        return StatsAndCliTests.run_cli(None, "--root", str(root), *args)

    def test_the_generated_index_is_not_counted_as_a_place_where_something_waits(self):
        root = fixture_copy()
        (root / "Note W.md").write_text("---" + chr(10) + "title: W" + chr(10) + "type: note" + chr(10) + "status: draft" + chr(10) + "updated: 2024-01-01" + chr(10)
                                        + "summary: Waiting on the vendor" + chr(10) + "---" + chr(10) + "Waiting on the vendor" + chr(10), encoding="utf-8")
        cfg = json.loads((root / "kb.config.json").read_text(encoding="utf-8"))
        cfg["index_columns"] = ["title", "summary"]
        (root / "kb.config.json").write_text(json.dumps(cfg), encoding="utf-8")
        import kb_index
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            kb_index.main(["--root", str(root), "--write"])
        self.assertIn("Waiting on the vendor", (root / "Index.generated.md").read_text(encoding="utf-8"))
        stats = json.loads(self.cli(root, "--stats", "--json")[1])["stats"]
        self.assertNotIn("Index.generated.md", stats["waiting_marker_by_note"])
        self.assertEqual(stats["waiting_marker_by_note"]["Note W.md"], 2)

    def test_the_summary_says_when_orphans_are_checked_only_in_part_of_the_vault(self):
        root = fixture_copy()
        out = self.cli(root)[1]
        self.assertIn("orphans checked only in Projects/, Home.md", out)
        stats = json.loads(self.cli(root, "--stats", "--json")[1])["stats"]
        self.assertEqual(stats["orphans_checked_in"], ["Projects/", "Home.md"])
        cfg = json.loads((root / "kb.config.json").read_text(encoding="utf-8"))
        cfg["orphans_scope"] = ["*"]
        (root / "kb.config.json").write_text(json.dumps(cfg), encoding="utf-8")
        self.assertNotIn("orphans checked only", self.cli(root)[1])

    def test_entries_that_ship_with_the_example_config_do_not_warn_but_your_own_do(self):
        root = fixture_copy()
        example = json.loads(self.read("kb.config.example.json"))
        cfg = {k: example[k] for k in ("exempt_names", "exempt_dirs", "secret_scan_exclude")}
        (root / "kb.config.json").write_text(json.dumps(cfg), encoding="utf-8")
        _, found = lint(root, kb_lint.CHECKS)
        self.assertEqual([f.message for f in found if f.check == "config"], [])
        cfg["exempt_dirs"] = cfg["exempt_dirs"] + ["my-typo/"]
        (root / "kb.config.json").write_text(json.dumps(cfg), encoding="utf-8")
        _, found = lint(root, kb_lint.CHECKS)
        self.assertEqual([f.message for f in found if f.check == "config"], ["exempt_dirs entry matches nothing: my-typo/"])

    def test_the_shipped_entries_are_exactly_the_ones_in_the_example_config(self):
        example = json.loads(self.read("kb.config.example.json"))
        for key, shipped in kb_lint.SHIPPED_ENTRIES.items():
            self.assertEqual(shipped, set(example[key]), key)

    def test_the_prompt_names_the_pilot_commands_and_the_cleanliness_rule(self):
        prompt = self.read("BOOTSTRAP-PROMPT.md")
        phase2 = prompt.split("**Phase 2 ")[1].split("**Phase 2b")[0]
        for phrase in ("kb_frontmatter.py --map <file> --write", "--merge", "kb_backup.py --verify", "**0 errors** in the pilot folder", "add `source:<that note's path>` to `ignore_links`",
                       "write the reason in the install log", "ask me whether I want the body corrected"):
            self.assertIn(phrase, phase2, phrase)
        phase1 = prompt.split("**Phase 1")[1].split("**Phase 2")[0]
        self.assertIn("`index_frontmatter.domain`", phase1)
        phase3 = prompt.split("**Phase 3")[1].split("**Phase 4")[0]
        for phrase in ("narrows the orphan check", "not a fix", "orphans_scope"):
            self.assertIn(phrase, phase3, phrase)
        phase0 = prompt.split("**Phase 0")[1].split("**Phase 1")[0]
        for phrase in ("leave out any `__pycache__` folder", "YYYY-MM-DD", "inside the table under \"Log\""):
            self.assertIn(phrase, phase0, phrase)

    def test_the_guide_opening_no_longer_says_obsidian_stays_the_viewer(self):
        guide = self.read("GUIDE.md")
        self.assertNotIn("Obsidian stays the viewer and editor. The kit adds", guide)
        self.assertNotIn("two small tools", guide)
        self.assertIn("works with or without Obsidian", guide)
        section = guide.split("## Installing without Obsidian")[1].split("## ")[0]
        for phrase in ("index_link_style", "relative Markdown link", "VS Code", "GitHub", "`_system/`", "system_dir_exempt"):
            self.assertIn(phrase, section, phrase)

    def test_the_tools_readme_has_no_stale_both_and_explains_the_quiet_settings(self):
        text = self.read("tools/README.md")
        self.assertNotIn("Both read", text)
        for phrase in ("Every tool reads `kb.config.json`", "orphans checked only in", "not a fix", "First-run warnings", "Skipped test", "symbolic link",
                       "files excluded from secret scan", "links ignored", "default changed in 1.8", "Pages under `_system/`", "system_dir_exempt"):
            self.assertIn(phrase, text, phrase)

    def test_agent_rules_have_a_pilot_version_note(self):
        rules = self.read("templates/AGENT-RULES.md")
        for phrase in ("PILOT VERSION", "local CLAUDE.md inside the pilot folder", "do not exist until Phase 3"):
            self.assertIn(phrase, rules, phrase)


class LinkStyleTests(unittest.TestCase):
    def build(self, root, **cfg_over):
        import kb_index
        cfg, _ = kb.load_config(root)
        cfg.update(cfg_over)
        return kb_index.build(root, cfg)

    def plain_root(self):
        root = fixture_copy()
        shutil.rmtree(root / ".obsidian", ignore_errors=True)
        cfg = json.loads((root / "kb.config.json").read_text(encoding="utf-8"))
        cfg.pop("index_link_style", None)
        (root / "kb.config.json").write_text(json.dumps(cfg), encoding="utf-8")
        return root

    def test_the_default_is_auto(self):
        self.assertEqual(kb.load_config(Path(tempfile.mkdtemp(prefix="kbl-")))[0]["index_link_style"], "auto")

    def test_auto_gives_wikilinks_in_an_obsidian_vault_and_markdown_links_elsewhere(self):
        obsidian = fixture_copy()
        self.assertTrue((obsidian / ".obsidian").is_dir())
        self.assertIn("[[Concepts/Graph Theory\\|", self.build(obsidian))
        plain = self.plain_root()
        text = self.build(plain)
        self.assertNotIn("[[", text)
        self.assertIn("[Graph Theory](Concepts/Graph%20Theory.md)", text)

    def test_explicit_values_win_and_path_is_another_name_for_markdown(self):
        plain = self.plain_root()
        self.assertIn("[[Concepts/Graph Theory\\|", self.build(plain, index_link_style="wikilink"))
        self.assertEqual(self.build(plain, index_link_style="markdown"), self.build(plain, index_link_style="path"))
        obsidian = fixture_copy()
        self.assertIn("[Graph Theory](Concepts/Graph%20Theory.md)", self.build(obsidian, index_link_style="markdown"))

    def test_the_pipe_in_a_title_cannot_break_the_table_row(self):
        plain = self.plain_root()
        (plain / "Pipe Note.md").write_text(
            "---" + chr(10) + "title: A | B" + chr(10) + "type: note" + chr(10) + "status: draft" + chr(10) + "updated: 2024-01-01" + chr(10) + "---" + chr(10), encoding="utf-8")
        text = self.build(plain)
        row = next(l for l in text.splitlines() if "Pipe%20Note.md" in l)
        self.assertIn("[A \\| B](Pipe%20Note.md)", row)
        columns = len([c for c in text.splitlines() if c.startswith("| title")][0].strip("|").split("|"))
        self.assertEqual(len(re.split(r"(?<!\\)\|", row.strip().strip("|"))), columns)

    def test_a_bad_value_is_refused(self):
        root = fixture_copy()
        (root / "kb.config.json").write_text('{"index_link_style": "html"}', encoding="utf-8")
        with self.assertRaises(kb.ConfigError):
            kb.load_config(root)


class DefaultsAndRuntimeTests(unittest.TestCase):
    def test_claude_md_and_agents_md_are_exempt_by_default(self):
        root = fixture_copy()
        (root / "kb.config.json").write_text(json.dumps({"required_fields": ["title"]}), encoding="utf-8")
        for name in ("CLAUDE.md", "AGENTS.md", "README.md", "Other.md"):
            (root / name).write_text("[[Home]]\n", encoding="utf-8")
        _, found = lint(root, ("orphans",), schema=True)
        flagged = {f.path for f in found if f.check in ("frontmatter", "orphans")}
        self.assertTrue({"Other.md"} <= flagged)
        self.assertFalse(flagged & {"CLAUDE.md", "AGENTS.md", "README.md"})

    def test_the_example_config_lists_the_new_options(self):
        cfg, notes = kb.load_config(HERE.parent, HERE.parent / "kb.config.example.json")
        self.assertEqual(notes, [])
        self.assertEqual(cfg["orphan_ok_types"], ["source"])
        self.assertEqual(cfg["exempt_names"], ["README.md", "CLAUDE.md", "AGENTS.md"])
        self.assertIn(".local_secrets/", cfg["secret_file_patterns"])

    def test_summary_line_and_stats_carry_the_run_time(self):
        code, out, _ = StatsAndCliTests.run_cli(None, "--root", str(FIXTURE))
        self.assertRegex(out, r"; in \d+\.\ds\n$")
        stats = json.loads(StatsAndCliTests.run_cli(None, "--root", str(FIXTURE), "--stats", "--json")[1])["stats"]
        self.assertIsInstance(stats["seconds"], float)


if __name__ == "__main__":
    unittest.main()
