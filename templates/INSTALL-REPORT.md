---
title: "Second Brain Kit – install report"
type: synthesis
domain: <your-domain>
status: done
updated: YYYY-MM-DD
summary: "Anonymous report on how the kit installed and performed in this vault, with improvement ideas, for the kit maintainers."
---
# Second Brain Kit – install report

**Privacy first (read before writing):** this report leaves your organization. It must contain **no** content from the vault: no note text, no file or folder names, no project, customer, product, person or system names, no domain names you chose, no screenshots of notes. Use counts, ranges and generic descriptions only ("a folder of meeting notes", not its name). Error messages: keep the message, replace any path or name with `<path>` / `<name>`. If in doubt, leave it out.

The kit never sends anything by itself. The owner reviews this file and decides whether to share it.

## 1. Environment
- OS and version:
- Python version (`python --version`):
- Agent used (Claude Code CLI / desktop / other) and model if known:
- Obsidian used? (yes / no / only as viewer):
- Vault size: notes `<100 / 100–500 / 500–2,000 / 2,000+`; folders (count):
- Share of non-Markdown documents (Word/PDF/PowerPoint/Excel): `none / <25% / 25–75% / >75%`

## 2. Install and phases
| Phase | Done? | Time (min) | Problems (generic) |
|---|---|---|---|
| 0 Inventory | | | |
| 1 Schema | | | |
| 2 Pilot (notes in pilot: __) | | | |
| 2b Non-Markdown companion notes (count: __) | | | |
| 3 Required mode + index | | | |
| 4 Query test | | | |
| 5 Roll-out (folders done: __ of __) | | | |

- Steps in `GUIDE.md` or `BOOTSTRAP-PROMPT.md` that were unclear or wrong:
- Errors (sanitized message + what fixed it):

## 3. Numbers (counts only)
| Metric | Before | After |
|---|---|---|
| Notes | | |
| % notes with frontmatter | | |
| Broken links | | |
| Orphans | | |
| Secret findings (count only) | | |
| `kb_lint` run time (s) | | |
| `kb_index` run time (s) | | |

- False positives of the linter (which check, generic pattern):
- Body unchanged in every edited note? (yes / no, how verified):

## 4. Schema fit
- Required fields kept as-is / changed (which field names, not values):
- Number of domains chosen: __; number of types: __; types you added or removed (generic):
- Existing properties that did not map cleanly (property *names* only if generic, e.g. `tags`, `date`):

## 5. Query test
- Score: __/10 (trap question answered "not found"? yes / no)
- Failure kinds (e.g. wrong page, missing source, answer not in vault, guessed):
- **Before / after (same questions, same model, new session each run; counts only, no question text):**
  | | Questions | Right | Files opened (total / avg) | Tokens (total / avg) |
  |---|---|---|---|---|
  | Before migration | __ | __ | __ / __ | __ / __ |
  | After migration | __ | __ | __ / __ | __ / __ |
  | Saving | | | __ % | __ % |
- Tokens are: measured / estimated (characters read / 4). Say which; never present an estimate as a measurement. Measured with `kb_querystats` (files and tokens taken from the session transcripts)? yes / no. Token column compared: total / new.
- Did accuracy hold (after at least as good as before, and at least 8 of 10 on the accuracy test)? yes / no
- Sample size and caveats (few questions, one model, one folder):

## 6. Recommendations
1. Top improvement:
2.
3.
- Missing feature you wanted:
- Something to remove or simplify:
- Would you recommend the kit to another team? (yes / no / why):

## 7. Privacy check (must be "yes" before sharing)
- [ ] No note text, file/folder names, project/customer/person/system names, or chosen domain names.
- [ ] Error messages sanitized.
- [ ] Owner reviewed this file.
