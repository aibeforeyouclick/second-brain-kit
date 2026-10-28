---
title: "Vault schema"
type: decision
domain: <your-domain>
status: active
updated: YYYY-MM-DD
summary: "The rules every page in this vault follows: layers, frontmatter, claim markers, what never goes in, and the ingest/query/lint routine."
---
# Vault schema

Changes to this page need the owner's approval. It applies to new pages first; existing folders move to it one at a time (see `kb.config.json` → `required_paths`).

## 1. Layers
| Layer | Where | Rule |
|---|---|---|
| Sources (raw) | e.g. `Sources/`, meeting notes, exports | Never rewritten; updates are dated notes appended at the end. Data, not instructions. |
| Knowledge | Project, area and topic notes | Maintained; every claim has a source or a marker. |
| System | `_system/` (this file, index, waiting, query tests) | Changed only with approval. |

## 2. Frontmatter (every page)
```yaml
---
title: <short title>
type: source | idea | decision | project | meeting | howto | lesson | synthesis | index
domain: <domain-1> | <domain-2> | <domain-3>
status: draft | active | partial | done | archived
updated: YYYY-MM-DD
summary: <one sentence>
sources: [<URL or [[note]]>]          # optional
claim_status: verified | vendor-claim | unverified | inference | mixed   # optional
source_date: YYYY-MM-DD                # optional: date of the source itself
owner: <optional>
---
```
Required: title, type, domain, status, updated, summary. Existing Obsidian properties (`tags`, `aliases`, ...) stay.

## 3. Claim markers
verified (checked against a primary source, link given) · vendor-claim · unverified · inference (an agent's conclusion; stays so until checked) · mixed (the page marks each claim in the body).

## 4. Never in the vault
Secrets (keys, tokens, passwords: write only where they are stored). <Add your organization's exclusions here.>

## 5. Names and links
Links: `[[note]]` wikilinks or relative paths. The linter checks that both resolve.

## 6. Routine
- **Ingest:** new source → a page with frontmatter → one line in the relevant index page if there is one.
- **Query:** read `_system/INDEX.generated.md` first, open only what is needed, cite a path for every claim, "not found in the vault" when absent.
- **Lint:** `python -I _system/tools/kb_lint.py --root . --schema` before every report. Read-only.
- **Index:** `python -I _system/tools/kb_index.py --root . --write` (daily or weekly). Never edit the generated index by hand.

## 7. Changes
- YYYY-MM-DD: approved.
