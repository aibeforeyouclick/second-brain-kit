# Changelog

Versions come from real installs and the author's own use.

## 1.8 – first public release
- `kb_secure_move`: move a sensitive file out of a synced folder (copy, SHA-256 check, original to the trash, log row); dry run by default.
- Secret scan: scripts and JSON scanned by default; only literal values are flagged (not values from a function call or a variable); private-key fields and PEM blocks; captured log exports reported separately (`secret-in-capture`); exclusions can expire (`until`); BOM and UTF-16 handled.
- `kb_index`: `index_scope` (default `valid`: only notes that pass the schema).
- `kb_querystats --split-numbered` for a numbered list of questions in one message.
- Bootstrap prompt: instruction-file rules added only at phase 3 and scoped for folders shared by many agents; agent-memory warning in the query test; conflicts page; "Do I need Obsidian?".

## 1.0 – 1.7 (private)
Schema, linter, index, bootstrap prompt, install report, `kb_frontmatter` (hash-verified writes), `kb_backup`, `kb_querystats`, cloud-only file skipping, per-folder stats.
