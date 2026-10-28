---
title: "Query test YYYY-MM-DD"
type: synthesis
domain: <your-domain>
status: done
updated: YYYY-MM-DD
summary: "Real questions answered only from the vault, each with a source path, run before and after a folder is migrated; graded by the owner against answers written in advance, with files opened and tokens measured."
---
# Query test YYYY-MM-DD

## Rules
1. **The owner writes the questions and the expected answers, not the agent**, **before** the run and **outside the vault**. The agent never sees the expected answers and never writes or edits a question or an answer key.
2. **Every expected answer must be written down in the folder and stable in time** (a fact that will not change next week). If an answer can change, write down the correct answer for the test day and say so ("as of YYYY-MM-DD").
3. **Trap question:** include at least one question whose answer exists **only in the owner's head or in an email** and is not in the vault. The right answer is "not found in the vault"; anything else is a miss. **Check the trap before the run:** the owner searches the whole folder (a text search for the key words, names, numbers and dates of the question, also in other spellings) and confirms that the answer really is not there. If the search finds it, it is not a trap: replace the question. Write "trap checked YYYY-MM-DD" next to it.
4. A **new** session answers from the vault only: index first (if the folder is already migrated), then search. A path for every claim. "Not found in the vault" when absent; no guessing.
5. **Before / after:** ask the same questions, word for word, about a folder **before** it is migrated (run: before) and **again after** it is migrated and the index is rebuilt (run: after). Each run is a new session, same model and settings, and the agent is not shown the earlier results. The difference between the runs is the proof of the saving.
6. **Measure for every question and run:** how many distinct files the agent opened (taken from the agent's own tool log or report), the tokens used, and whether the answer was right. **Preferred: measure with `kb_querystats.py`** on the session transcript of each run (distinct files and real token counts per question; each run in its own fresh session). If you cannot, and the agent tool shows actual token counts, write them. If not, **estimate** (characters read divided by 4) and keep "(est.)" in the column. Never present an estimate as a measurement.
7. The owner grades. Grade: right / partly / wrong (a wrong "not found" and a guessed answer to the trap both count as wrong).
8. **Scope: the accuracy part is asked only about folders that are already migrated** (frontmatter, in `required_paths`, in the index). Tell the session explicitly: "Answer only from these folders: <list>. Everything else is out of scope and the right answer for it is not found in the vault." The "before" part of the before/after comparison is asked about the folders that are **not** migrated yet. Mixing the two makes both numbers meaningless. Record the scope you gave in the Runs table.
9. **Two sources that disagree:** the right behavior is to answer with both, say that they conflict, and add a row to `_system/CONFLICTS.md` (and `claim_status: mixed` in the frontmatter of the notes involved). The agent must not pick one silently. The owner grades reporting the conflict as right when the conflict is real.
10. **One question per message.** Send each question as its own message, and wait for the answer before the next. The measurement (`kb_querystats`) counts one message as one question: ten questions in one message give one row with the total, and the cost of each question cannot be told apart. If you did send them together, run `kb_querystats.py --split-numbered`: it divides the message only when the agent answered the numbered items one by one with the same numbers; otherwise it says why it could not and gives the total only.

11. **Agent memory can leak answers.** An agent's own memory (auto-memory, saved preferences, earlier sessions, an open conversation) can hold facts that are not in the notes, and a correct answer may come from there. Require a **note path for every answer**: an answer without a path is graded "from memory" and counts as wrong, so a leak shows in the score instead of hiding in it. Where you can, run the test in a session with the agent's memory turned off.

## Runs
One row per run. The **session id** is the name (without `.jsonl`) of the session transcript file, normally under `~/.claude/projects/<project folder>/`, so that every number below can be reproduced.

| Run (before / after) | Session id | Date and time (from the clock) | Model | Scope (folders) | Measured with kb_querystats |
|---|---|---|---|---|---|
| before | | | | | yes / no |
| after | | | | | yes / no |

**After each run, measure it** (the agent does not report this about itself):
```
python -I _system/tools/kb_querystats.py --session <path to the session .jsonl> --no-paths --include-bash
```
Copy per question the `files` and `total` (or `new`) columns into the table below. `--include-bash` also counts files read through shell commands; it is a heuristic, so say so if the numbers are close. Run it on the transcript of **that run's own fresh session**.

## Questions and results
Add **one row per question per run** (the same question number appears once with `before` and once with `after`). For a vault-wide accuracy test with no before/after, write `after` in every row.

| # | Question | Run (before / after) | Answer (short) | Sources (paths) | Files opened | Tokens (est.) | Confidence | Grade (owner) |
|---|---|---|---|---|---|---|---|---|
| 1 | | before | | | | | | |
| 1 | | after | | | | | | |
| 2 | | before | | | | | | |
| 2 | | after | | | | | | |
| 3 | | before | | | | | | |
| 3 | | after | | | | | | |
| 4 | | before | | | | | | |
| 4 | | after | | | | | | |
| 5 | | before | | | | | | |
| 5 | | after | | | | | | |
| 6 | | before | | | | | | |
| 6 | | after | | | | | | |
| 7 | | before | | | | | | |
| 7 | | after | | | | | | |
| 8 | | before | | | | | | |
| 8 | | after | | | | | | |
| 9 | | before | | | | | | |
| 9 | | after | | | | | | |
| 10 (trap) | | before | | | | | | |
| 10 (trap) | | after | | | | | | |

## Result
| | Questions right | Files opened (total / average) | Tokens (total / average) | Tokens are |
|---|---|---|---|---|
| **Before** | __ / __ | __ / __ | __ / __ | measured / estimated |
| **After** | __ / __ | __ / __ | __ / __ | measured / estimated |
| **Saving** | | __ % | __ % | |

**Measured with kb_querystats:** yes / no (if yes, delete "(est.)" from the column header and say which token column you compare: total or new).

Saving = (before - after) / before. Accuracy target after migration: **at least 8 of 10**. A saving with lower accuracy is not a saving.

**Gaps found in the vault** (fix them, then note here):
