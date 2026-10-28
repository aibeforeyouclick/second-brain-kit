"""Unit tests for kb_common. Run from the kit root:  python -I -m unittest discover -s tests -v"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))
import kb_common as kb  # noqa: E402

FIXTURE_SRC = HERE / "fixtures" / "vault"


def _materialize() -> Path:
    """Copy the sample vault to a temp folder, turning `*.md.fixture` into `*.md`.

    The notes are stored as `.md.fixture` so that tools which scan the surrounding repository do not
    treat the deliberately flawed sample notes as real pages. An empty `.obsidian` folder is added
    because version control cannot store empty folders."""
    dest = Path(tempfile.mkdtemp(prefix="kbfixture-")) / "vault"
    for src in sorted(FIXTURE_SRC.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(FIXTURE_SRC).as_posix()
        if rel.endswith(".md.fixture"):
            rel = rel[: -len(".fixture")]
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
    (dest / ".obsidian").mkdir(exist_ok=True)
    return dest


FIXTURE = _materialize()


def fixture_copy() -> Path:
    dest = Path(tempfile.mkdtemp(prefix="kbvault-")) / "vault"
    shutil.copytree(FIXTURE, dest)
    return dest


def make_vault(files: dict, config=None) -> kb.Vault:
    root = Path(tempfile.mkdtemp(prefix="kbv-"))
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    cfg = json.loads(json.dumps(kb.DEFAULTS))
    cfg.update(config or {})
    return kb.Vault(root, cfg)


class FrontmatterTests(unittest.TestCase):
    def fm(self, body):
        return kb.parse_frontmatter("---\n" + body + "\n---\ntext\n")

    def test_none_without_frontmatter_and_empty_block(self):
        self.assertIsNone(kb.parse_frontmatter("# Title\n"))
        self.assertEqual(kb.parse_frontmatter("---\n---\nbody\n"), {})

    def test_scalars_quotes_and_empty_values(self):
        fm = self.fm('title: "Quoted: with colon"\nstatus: draft\nsummary:\nnote: \'single\'\ncount: 3')
        self.assertEqual(fm["title"], "Quoted: with colon")
        self.assertEqual(fm["status"], "draft")
        self.assertEqual(fm["summary"], "")
        self.assertEqual(fm["note"], "single")
        self.assertEqual(fm["count"], "3")

    def test_inline_list_with_quotes_and_commas(self):
        fm = self.fm('tags: [a, "b, c", d]')
        self.assertEqual(fm["tags"], ["a", "b, c", "d"])

    def test_multiline_list_and_following_key(self):
        fm = self.fm("tags:\n  - one\n  - \"two words\"\n  - three\nstatus: active")
        self.assertEqual(fm["tags"], ["one", "two words", "three"])
        self.assertEqual(fm["status"], "active")

    def test_multiline_inline_list(self):
        fm = self.fm("aliases: [a,\n  b,\n  c]\nstatus: x")
        self.assertEqual(fm["aliases"], ["a", "b", "c"])
        self.assertEqual(fm["status"], "x")

    def test_block_scalar_and_nested_map(self):
        fm = self.fm("summary: >\n  folded text\n  continues\nmeta:\n  inner: 1\nstatus: ok")
        self.assertEqual(fm["summary"], "folded text continues")
        self.assertEqual(fm["meta"], "")
        self.assertEqual(fm["status"], "ok")

    def test_inline_comment_crlf_bom_and_dots_terminator(self):
        text = chr(0xFEFF) + "---\r\ntitle: T # trailing comment\r\ntags:\r\n  - x\r\n...\r\nbody\r\n"
        fm = kb.parse_frontmatter(text)
        self.assertEqual((fm["title"], fm["tags"]), ("T", ["x"]))

    def test_keys_with_spaces_and_unicode(self):
        fm = self.fm("Due Date: 2024-01-02\nnaïve key: yes")
        self.assertEqual(fm["Due Date"], "2024-01-02")
        self.assertEqual(fm["naïve key"], "yes")

    def test_must_start_at_first_line(self):
        self.assertIsNone(kb.parse_frontmatter("\n---\ntitle: late\n---\n"))


class MaskAndLinkTests(unittest.TestCase):
    def links(self, text):
        return [(k, t, a, n, e) for k, t, a, n, e in kb.extract_links(kb.mask(text))]

    def test_wikilink_forms(self):
        got = self.links("[[a]] [[b|alias]] [[c#Head]] [[d#H1#H2]] ![[img.png]] [[e^block]] [[#same]]")
        names = [(t, a, e) for _, t, a, _, e in got]
        self.assertEqual(names, [("a", "", False), ("b", "", False), ("c", "Head", False), ("d", "H2", False),
                                 ("img.png", "", True), ("e", "", False), ("", "same", False)])

    def test_table_escaped_pipe(self):
        got = self.links("| [[Note Name\\|shown]] |")
        self.assertEqual(got[0][1], "Note Name")

    def test_markdown_links_plain_angle_brackets_and_titles(self):
        got = self.links('[a](x.md) [b](<with space.md>) [c](y.md "title") ![d](pic.png)')
        self.assertEqual([(t, e) for _, t, _, _, e in got], [("x.md", False), ("with space.md", False),
                                                            ("y.md", False), ("pic.png", True)])

    def test_code_fences_inline_code_and_comments_are_masked_keeping_line_numbers(self):
        text = "[[one]]\n```\n[[fenced]]\n```\n`[[inline]]` [[two]]\n%% [[commented]]\nstill comment %%\n[[three]]\n"
        got = [(t, n) for _, t, _, n, _ in self.links(text)]
        self.assertEqual(got, [("one", 1), ("two", 5), ("three", 8)])

    def test_tilde_fence_and_longer_fence(self):
        got = [t for _, t, *_ in self.links("~~~\n[[a]]\n~~~\n````\n[[b]]\n````\n[[c]]")]
        self.assertEqual(got, ["c"])

    def test_headings_ignore_fenced_code(self):
        found = kb.headings("# Top\n```\n# not a heading\n```\n## Sub Section ##\n")
        self.assertEqual(found, {"top", "sub section"})


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.v = make_vault({
            "Home.md": "x", "Notes/Alpha Plan.md": "x", "Other/Alpha Plan.md": "x", "Notes/v1.2 plan.md": "x",
            "assets/Diagram One.PNG": b"x", "docs/spec.pdf": b"x", "Notes/Sub/Deep.md": "x",
            "A/Same.md": "x", "A/Local.md": "x",
        })

    def r(self, target, kind="wiki", source="Home.md"):
        return self.v.resolve(source, kind, target)

    def test_by_name_anywhere_case_insensitive(self):
        self.assertEqual(self.r("home"), ["Home.md"])
        self.assertEqual(self.r("Deep"), ["Notes/Sub/Deep.md"])

    def test_extension_and_non_markdown_files(self):
        self.assertEqual(self.r("Home.md"), ["Home.md"])
        self.assertEqual(self.r("diagram one.png"), ["assets/Diagram One.PNG"])
        self.assertEqual(self.r("spec.pdf"), ["docs/spec.pdf"])

    def test_dotted_note_names_and_spaces(self):
        self.assertEqual(self.r("v1.2 plan"), ["Notes/v1.2 plan.md"])

    def test_duplicate_names_return_all_candidates(self):
        self.assertEqual(self.r("Alpha Plan"), ["Notes/Alpha Plan.md", "Other/Alpha Plan.md"])

    def test_path_suffix_disambiguates(self):
        self.assertEqual(self.r("Other/Alpha Plan"), ["Other/Alpha Plan.md"])
        self.assertEqual(self.r("Notes/Sub/Deep"), ["Notes/Sub/Deep.md"])

    def test_unresolved_and_empty_targets(self):
        self.assertEqual(self.r("Nope"), [])
        self.assertIsNone(self.r(""))

    def test_markdown_links_relative_url_decoded_and_external(self):
        self.assertEqual(self.r("../../Other/Alpha%20Plan.md", "md", "Notes/Sub/Deep.md"), ["Other/Alpha Plan.md"])
        self.assertEqual(self.r("Local", "md", "A/Same.md"), ["A/Local.md"])
        self.assertIsNone(self.r("https://example.com/x", "md"))
        self.assertIsNone(self.r("#section", "md"))
        self.assertEqual(self.r("missing.md", "md"), [])


class VaultWalkTests(unittest.TestCase):
    def test_dot_folders_and_dot_files_are_never_scanned(self):
        v = make_vault({"a.md": "x", ".obsidian/notes.md": "x", ".trash/old.md": "x", ".git/x.md": "x",
                        "node_modules/p/readme.md": "x", "sub/.hidden.md": "x", "sub/b.md": "x"})
        self.assertEqual(v.notes, ["a.md", "sub/b.md"])

    def test_aliases_index_reads_list_and_scalar_forms(self):
        v = make_vault({"One.md": "---\naliases: [Uno, 'Un']\n---\n", "Two.md": "---\naliases:\n  - Dos\n---\n",
                        "Three.md": "---\naliases: Tres\n---\n"})
        self.assertEqual({k: v.by_alias[k] for k in ("uno", "un", "dos", "tres")},
                         {"uno": ["One.md"], "un": ["One.md"], "dos": ["Two.md"], "tres": ["Three.md"]})

    def test_exemption_rules(self):
        v = make_vault({"README.md": "x", "templates/t.md": "x", "n.md": "x", "Index.generated.md": "x"},
                       {"exempt_names": ["readme.MD"], "index_output": "Index.generated.md"})
        self.assertEqual([r for r in v.notes if v.is_exempt(r)], ["Index.generated.md", "README.md", "templates/t.md"])
        self.assertTrue(v.in_exempt_dir("Templates/x.md"))

    def test_path_matches_forms(self):
        self.assertTrue(kb.path_matches("Projects/", "projects/a/b.md"))
        self.assertTrue(kb.path_matches("Home.md", "home.md"))
        self.assertTrue(kb.path_matches("*.md", "x.md"))
        self.assertFalse(kb.path_matches("Projects/", "Other/projects/a.md"))


class ConfigTests(unittest.TestCase):
    def write(self, data):
        root = Path(tempfile.mkdtemp(prefix="kbc-"))
        (root / kb.CONFIG_NAME).write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return root

    def test_defaults_when_no_file(self):
        cfg, notes = kb.load_config(Path(tempfile.mkdtemp(prefix="kbc-")))
        self.assertEqual(cfg["exempt_dirs"], [".obsidian/", ".trash/", "templates/", ".git/", "node_modules/", "venv/", ".venv/",
                                              "__pycache__/"])
        self.assertTrue(notes)

    def test_overrides_unknown_keys_and_comment_keys(self):
        cfg, notes = kb.load_config(self.write({"_c": "x", "waiting_marker": "Pending", "mystery": 1}))
        self.assertEqual(cfg["waiting_marker"], "Pending")
        self.assertEqual(notes, ["unknown config key ignored: mystery"])

    def test_type_errors_are_rejected(self):
        for bad in ({"exempt_names": "README.md"}, {"resolve_aliases": "yes"}, {"enums": {"type": "note"}},
                    {"waiting_marker": 3}, {"index_link_style": "html"}, {"index_frontmatter": []},
                    {"index_output": "../out.md"}, {"index_output": "/abs.md"}, {"index_output": "index.txt"}):
            with self.assertRaises(kb.ConfigError, msg=str(bad)):
                kb.load_config(self.write(bad))

    def test_invalid_json_and_non_object(self):
        for bad in ("{broken", "[1, 2]"):
            with self.assertRaises(kb.ConfigError):
                kb.load_config(self.write(bad))

    def test_utf8_bom_config_is_accepted(self):
        root = self.write("")
        (root / kb.CONFIG_NAME).write_bytes(b"\xef\xbb\xbf" + json.dumps({"waiting_marker": "X"}).encode())
        self.assertEqual(kb.load_config(root)[0]["waiting_marker"], "X")

    def test_dates(self):
        self.assertTrue(kb.real_date("2024-02-29"))
        self.assertTrue(kb.real_date("2024-05-01T10:30:00"))
        for bad in ("2023-02-29", "2024-13-01", "05/01/2024", "2024-5-1", ""):
            self.assertFalse(kb.real_date(bad), bad)


class DirMatchTests(unittest.TestCase):
    def test_single_folder_names_match_at_any_depth_and_are_case_insensitive(self):
        self.assertTrue(kb.dir_match("node_modules/", "a/b/node_modules/pkg/x.md"))
        self.assertTrue(kb.dir_match("Vendor/", "x/vendor/y.md"))
        self.assertTrue(kb.dir_match("old-*/", "a/old-2020/x.md"))       # glob + trailing slash: any depth
        self.assertFalse(kb.dir_match("old-*/", "a/older/x.md"))
        self.assertTrue(kb.dir_match("docs/*/vendor/", "docs/x/vendor/y.md"))  # glob in a longer path: from the root
        self.assertFalse(kb.dir_match("docs/*/vendor/", "z/docs/x/vendor/y.md"))
        self.assertFalse(kb.dir_match("vendor/", "x/vendors/y.md"))
        self.assertFalse(kb.dir_match("vendor/", "x/vendor.md"))  # a file named like the folder is not inside it

    def test_longer_paths_match_from_the_root_only_and_globs_work(self):
        self.assertTrue(kb.dir_match("docs/api/", "docs/api/x.md"))
        self.assertFalse(kb.dir_match("docs/api/", "more/docs/api/x.md"))
        self.assertTrue(kb.dir_match("export/data.json", "Export/Data.json"))
        self.assertTrue(kb.dir_match("*.cfg", "a/b/app.cfg"))


class FencedLinesTests(unittest.TestCase):
    def test_fence_lines_and_their_content_are_marked(self):
        text = "a\n```py\nb\n```\nc\n~~~\nd\n~~~\ne\n"
        self.assertEqual(kb.fenced_lines(text), {2, 3, 4, 6, 7, 8})

    def test_longer_fences_and_unclosed_fences(self):
        self.assertEqual(kb.fenced_lines("````\nx\n````\ny"), {1, 2, 3})
        self.assertEqual(kb.fenced_lines("```\nx\ny"), {1, 2, 3})
        self.assertEqual(kb.fenced_lines("no fences here"), set())


class DotEnvTests(unittest.TestCase):
    def test_env_files_are_collected_outside_dot_folders_but_are_not_notes(self):
        v = make_vault({"a.md": "x", ".env": "k=v", ".env.local": "k=v", ".hidden.md": "x", ".obsidian/.env": "k=v",
                        "sub/.env.production": "k=v", ".gitignore": "x"})
        self.assertEqual(sorted(v.dotenv_files), [".env", ".env.local", "sub/.env.production"])
        self.assertEqual(v.notes, ["a.md"])


class NewConfigKeysTests(unittest.TestCase):
    def test_defaults_for_the_new_keys(self):
        cfg, _ = kb.load_config(Path(tempfile.mkdtemp(prefix="kbc-")))
        self.assertEqual((cfg["orphans_scope"], cfg["secret_scan_exclude"]), ([], {}))
        self.assertEqual(cfg["secret_scan_extensions"], list(kb.DEFAULT_SCAN_EXTENSIONS))  # notes, scripts and JSON; narrow it in the config
        self.assertEqual(kb.DEFAULT_SCAN_EXTENSIONS, (".md", ".json", ".ps1", ".sh", ".py", ".js", ".ts", ".bat", ".cmd", ".yml", ".yaml", ".env"))
        self.assertEqual(cfg["index_scope"], "valid")

    def test_keys_written_in_the_config_file_are_remembered_as_explicit(self):
        root = Path(tempfile.mkdtemp(prefix="kbc-"))
        (root / kb.CONFIG_NAME).write_text(json.dumps({"exempt_dirs": ["a/"], "_note": "x"}), encoding="utf-8")
        cfg, _ = kb.load_config(root)
        self.assertEqual(cfg["_explicit"], {"exempt_dirs"})
        self.assertEqual(kb.load_config(Path(tempfile.mkdtemp(prefix="kbc-")))[0]["_explicit"], set())

    def test_the_example_config_loads_and_lists_the_third_party_folders(self):
        cfg, notes = kb.load_config(HERE.parent, HERE.parent / "kb.config.example.json")
        self.assertEqual(notes, [])
        for folder in ("node_modules/", ".venv/", "venv/", "__pycache__/", ".tmp/", "dist/", "build/"):
            self.assertIn(folder, cfg["exempt_dirs"])
        self.assertTrue(all(reason.strip() for reason in cfg["secret_scan_exclude"].values()))


if __name__ == "__main__":
    unittest.main()
