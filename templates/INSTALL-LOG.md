---
title: "Second Brain Kit – install log"
type: howto
domain: <your-domain>
status: active
updated: YYYY-MM-DD
summary: "Running log of the install: one row per step with the real time, what was done, what went wrong and how it was fixed. Used later to fill the install report."
---
# Install log

Copy this file to `_system/INSTALL-LOG-<date>.md` at the start of **Phase 0** and keep it open for the whole install. Add a row **as you work**, not afterwards.

## Rules
1. **Times come from the clock, not from memory.** Run `date` (Git Bash, macOS, Linux) or `Get-Date -Format "yyyy-MM-dd HH:mm:ss"` (PowerShell) and paste the result. Never write an estimated time. The duration of a step is the difference between two clock readings.
2. **One row per step**, including the steps that went wrong. A problem that was fixed is still a row.
3. **Privacy:** this log may feed the install report that leaves your organization. Write **no** note text, no file or folder names, no project, customer, product or person names, no paths, no values of anything found. Use generic words ("a note with a broken link", "a folder of third-party documentation", "a key file"). Counts are fine.
4. **Never write a secret** here, and never a value the linter found: only the kind of finding and the count.
5. At the end of each phase, add a line `PHASE <n> DONE` with the clock time, so the phase durations can be read off the log.

## Log
| Time (from the clock) | Phase | Action | Problem | Fix |
|---|---|---|---|---|
| YYYY-MM-DD HH:MM:SS | 0 | Started the install, copied the tools | | |
| | | | | |

## Decisions and findings that need an owner (generic)
| Time | What kind of thing | What was decided | Who approved |
|---|---|---|---|
| | | | |

(For a security finding, also follow "Security findings during install" in `GUIDE.md`.)
