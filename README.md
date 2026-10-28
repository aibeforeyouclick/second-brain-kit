# Second Brain Kit

Turn a folder of Markdown notes into a **second brain your AI agent can maintain, search and trust**, and spend fewer tokens doing it.

Works with an existing Obsidian vault, or with any plain folder of notes. **Obsidian is optional.**

[![tests](https://github.com/aibeforeyouclick/second-brain-kit/actions/workflows/tests.yml/badge.svg)](https://github.com/aibeforeyouclick/second-brain-kit/actions/workflows/tests.yml)

## What you get
| Part | What it does |
|---|---|
| **Schema** | The same short set of fields on top of every note (title, type, domain, status, updated, summary), plus rules for marking claims as verified or not |
| **`kb_lint`** | A read-only health check: missing fields, broken `[[wikilinks]]`, orphans, and **secrets** (keys, tokens, private keys) in notes, scripts and JSON. It reports `path:line`, never the value |
| **`kb_index`** | One generated table of every note with a one-line summary. Your agent reads it first and opens only the 2–3 notes it needs |
| **`kb_frontmatter`**, **`kb_backup`** | Add fields to notes with a hash check that the note body never changed (it restores the file if it did); back up and verify |
| **`kb_querystats`** | Measures, from Claude Code's own session files, how many files and tokens each question cost, so you can prove the saving before and after |
| **`kb_secure_move`** | Takes a sensitive file out of a synced vault safely: copies it outside, checks the copy by SHA-256, sends the original to the trash, writes a log row. Dry run unless you pass `--apply` |
| **Bootstrap prompt** | Paste it into your agent. It installs the kit in phases and **stops for your approval after each one** |

## Why it works when a plain vault doesn't
A plain folder makes an agent open many files to answer one question, gives it nothing consistent to filter on, and slowly rots. The schema, the linter and the index fix that; a short routine keeps it true; a 10-question test checks it. On the author's own notes (183 notes migrated, every body hash-verified), a 10-question test scored 10 out of 10 with a source for each answer: an indication, not a benchmark.

## First real install
In our first outside install, on a large notes folder shared by many agents, broken links dropped by more than 95% once third-party docs were excluded, every frontmatter write in the pilot was hash-verified against the note body, and **on day one the built-in secret scan flagged real credential-handling problems that had gone unnoticed for months**. In the installer's words: *"the security value alone justified the install."*

## Install
Requirements: Python 3.10+ and an AI agent that can read and write files in a folder (built and tested with **Claude Code**).

1. Download the [latest release](https://github.com/aibeforeyouclick/second-brain-kit/releases) (or `git clone`) and unzip it **outside** your notes folder.
2. **Back up your notes** (the kit never deletes your notes; the only tool that moves a file is `kb_secure_move --apply`, which sends a file you name to the trash after a verified copy).
3. Open your agent **on your notes folder**.
4. Open [`BOOTSTRAP-PROMPT.md`](BOOTSTRAP-PROMPT.md), copy the text between the lines, replace `<VAULT>` with your notes folder and `<KIT>` with where you unzipped the kit, and paste it.
5. The agent works phase by phase and stops each time:

| Phase | What happens | Changes your notes? |
|---|---|---|
| 0 Inventory | Counts notes, finds third-party folders to exclude, scans for secrets | **No** (read-only) |
| 1 Schema | Proposes domains and types for *your* work; you approve | No (writes `_system/SCHEMA.md` and `kb.config.json`) |
| 2 Pilot | Adds fields to ~20–30 notes in one folder you choose | Adds fields at the top only; bodies verified by hash |
| 2b Documents | Short companion notes next to important Word/PDF files | Adds new notes; documents untouched |
| 3 Index | Builds the index and a "waiting for me" page | Adds `_system/` files |
| 4 Query test | 10 questions you write, answered from the notes only, measured | No |
| 5 Roll-out | Folder by folder | Fields only |
| 6 Report | An anonymous install report you may share with us | No |

Full explanation: [`GUIDE.md`](GUIDE.md). Tool reference: [`tools/README.md`](tools/README.md).

## Do I need Obsidian?
**No.** The kit works on any folder of `.md` files; Obsidian is just one way to look at them. Without it you can read and edit notes in any editor (VS Code, Notepad++), browse them on GitHub, or simply ask your agent: *"what do my notes say about X?"*, which is the main interface anyway. If most of your material is Word, PDF or PowerPoint, phase 2b writes a short summary note next to each important document, with a link to it, and never touches the document.

## Safety
What we can promise, and how you can check it yourself:
- **No network.** The tools use the Python standard library only and make no network calls; the install report is never sent by the kit. **Your AI agent, however, sends what it reads to its model provider:** check your organization's policy before pointing it at work notes.
- **Read-only by default.** `kb_lint` and `kb_querystats` never change files. `kb_index` writes only its own output file. `kb_frontmatter` writes only with `--write`, re-reads every note, compares the body's sha256, and restores the original on any difference. `kb_backup` writes a zip only to the `--dest` you give; `kb_secure_move` does nothing without `--apply`.
- **Secrets are never printed**, only `path:line` and the kind of finding.
- **Tested:** the test suite runs on Windows and Linux, Python 3.10 and 3.12, on every push (see the badge). Run it yourself: `python -I -m unittest discover -s tests`.
- **Scanned** for secrets with [gitleaks](https://github.com/gitleaks/gitleaks) before this release (1.8), and reviewed before publishing.
- **Notes are data, not instructions.** The bootstrap prompt tells the agent never to follow instructions found inside notes or sources (prompt injection).
- **Your approval at every phase.** The prompt tells the agent to stop and ask.

What we do **not** claim: that it is bug-free. It is young software. Read the code (it is short), run the tests, start with the read-only phase 0, and report problems in [Issues](https://github.com/aibeforeyouclick/second-brain-kit/issues). Security issues: use **Security → Report a vulnerability** (private).

## Feedback
Phase 6 writes an install report with counts and generic descriptions only, nothing from your notes. If you share it with us (open an issue and paste it), it goes into the next version. Every version so far came from a real install report.

## License
MIT © 2026 AI Before You Click. See [LICENSE](LICENSE).
