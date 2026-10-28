# Changelog

Versions come from real installs and the author's own use.

## 1.8.1
From the first clean "colleague" install (no Obsidian, 15 documentation issues, all fixed):
- **No Obsidian needed, and now it shows:** a "without Obsidian" section; `index_link_style` defaults to `auto`: wikilinks when the folder has an `.obsidian/` config, otherwise clickable relative Markdown links that work in VS Code and on GitHub (force either with `wikilink` or `markdown`); the guide no longer says Obsidian is the viewer.
- The kit's own pages in `_system/` are no longer counted as your notes, orphans or index rows.
- Templates copied into `_system/` no longer produce schema errors; the bootstrap prompt says what to fill in.
- Bootstrap prompt names the pilot commands (`kb_backup --dest`, `kb_frontmatter --write --merge`, `kb_backup --verify`), defines "clean" (0 errors in the pilot folder) and what to do with a broken link inside a body.
- One rule for the instruction file: during the pilot only a local `CLAUDE.md` in the pilot folder; the root file at phase 3.
- Honest numbers: `--stats` and the summary show excluded files, ignored links and the orphan scope next to the counts; the index counts notes without frontmatter.
- Less first-run noise: no warnings for the example config's own default entries.
- A warning when a default that changed in 1.8 (`index_scope`, `secret_scan_extensions`) is not set explicitly, so an upgrade never changes behaviour silently.

## 1.8 – first public release
**Upgrading from 1.7:** `index_scope` now defaults to `valid` (set `"index_scope": "all"` to keep the old index), and scripts + JSON are scanned for secrets by default. See "Upgrading the kit" in GUIDE.md.

- `kb_secure_move`: move a sensitive file out of a synced folder (copy, SHA-256 check, original to the trash, log row); dry run by default.
- Secret scan: scripts and JSON scanned by default; only literal values are flagged (not values from a function call or a variable); private-key fields and PEM blocks; captured log exports reported separately (`secret-in-capture`); exclusions can expire (`until`); BOM and UTF-16 handled.
- `kb_index`: `index_scope` (default `valid`: only notes that pass the schema).
- `kb_querystats --split-numbered` for a numbered list of questions in one message.
- `kb_querystats --only-under` works with Windows paths on Linux.
- Bootstrap prompt: instruction-file rules added only at phase 3 and scoped for folders shared by many agents; agent-memory warning in the query test; conflicts page; "Do I need Obsidian?".

## 1.0 – 1.7 (private)
Schema, linter, index, bootstrap prompt, install report, `kb_frontmatter` (hash-verified writes), `kb_backup`, `kb_querystats`, cloud-only file skipping, per-folder stats.
