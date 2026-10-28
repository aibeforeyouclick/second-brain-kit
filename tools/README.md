# Tools

Six helpers for a vault of Markdown notes (`kb_lint`, `kb_index`, `kb_frontmatter`, `kb_backup`, `kb_querystats`, `kb_secure_move`), plus a shared module (`kb_common.py`). Only `kb_frontmatter --write`, `kb_index --write`, `kb_backup --dest` and `kb_secure_move --apply` ever write, and each writes one clearly named thing. Python 3 standard library only. Both read `kb.config.json` from the vault root (copy `../kb.config.example.json`). Copy the whole `tools/` folder together; the scripts import `kb_common.py` from their own folder.

## kb_lint

Read-only health check for a folder of Markdown notes, built for Obsidian vaults. Python 3 standard library only, no network, never writes or renames anything.

```
python -I tools/kb_lint.py --root <vault>            # all checks
python -I tools/kb_lint.py --root <vault> --schema   # also validate frontmatter of every note that has one
python -I tools/kb_lint.py --root <vault> --stats    # counts instead of the findings list (add --json for machines)
python -I tools/kb_lint.py --root <vault> --stats --by-folder --depth 2   # counts per folder: notes, % frontmatter, broken links, orphans, secrets
python -I tools/kb_lint.py --root <vault> --stats --fields                # how many notes use each frontmatter property
python -I tools/kb_lint.py --root <vault> --path Projects/Alpha           # report only one subfolder (links still resolve against the whole vault)
python -I -m unittest discover -s tests -v           # run the tests (from the kit folder)
```

`--root` defaults to the current folder. Settings come from `kb.config.json` in the vault root (copy `kb.config.example.json`); without it the built-in defaults apply. Exit code: `0` clean, `1` errors (warnings too with `--strict`), `2` bad usage or config.

## Checks
| Check | Severity | What it finds |
|---|---|---|
| `links` | error / warning | Unresolved wikilinks, embeds and Markdown links (error). Ambiguous names that match several files, and heading links whose heading does not exist (warning). |
| `orphans` | warning | Notes that no other note links to or embeds, except notes whose `type` is in `orphan_ok_types` (leaf notes such as sources). Only notes in `orphans_scope` are reported (default: the folders in `required_paths`, otherwise the whole vault); links from notes outside the scope still count. |
| `schema` (`--schema`) | error | Missing `required_fields`, values outside `enums`, bad `date_fields`. |
| `frontmatter` (`--schema`) | warning | Notes without frontmatter that are not under `required_paths`. |
| `required` | error | Notes under `required_paths` that have no frontmatter (their fields are validated too). |
| `secrets` | error | Key/token-looking strings. **By default notes, scripts and JSON are read** (`secret_scan_extensions`: `.md .json .ps1 .sh .py .js .ts .bat .cmd .yml .yaml .env`); narrow or extend the list in `kb.config.json`. The value is never printed, only `file:line` and the pattern name. The summary line says how many files of each type were read, and `scripts not scanned (N script files found; see secret_scan_extensions)` when scripts or config files (`.py .ps1 .sh .bat .cmd .js .ts .yml .yaml .json .env`) exist that are not covered. A keyword assignment (`password`, `secret`, `token`, `api key`, also `client_secret`, `$AccessToken`, `GITHUB_TOKEN`) is reported **only when the value written there is a string literal** (or, in notes, config files and shell scripts, a long key-like literal). A value that comes from a function call, a variable, an environment read, a prompt or a config lookup (`token = Get-Token()`, `key = os.environ[...]`, `$secret = $env:X`, `password = getpass()`) is never reported; interpolation, placeholders, paths, URLs and environment-variable names inside a literal are ignored. Key-material fields (`privateKey`, `private_key`, `privateKeyPem`, `client_secret`, `apiKey`) with a long string value, and any `-----BEGIN ... PRIVATE KEY-----` line (PEM), are errors in every scanned file type, with path and line only. `.json` is part of the suggested `secret_scan_extensions` list: identity and settings files often hold a key. |
| `secret-file` | error | Key files recognized by **extension**: `*.key`, `*.pem`, `*.pfx`, `*.p12`, `*.jks`, `*.keystore`, `*.ppk`, `id_rsa*`, and anything in a `.local_secrets/` folder (`secret_file_patterns`). Code, documentation and public-key files (`.py`, `.md`, `.js`, `.ts`, `.ps1`, `.sh`, `.html`, `.pub`, ...) are never reported because of their name, even if an older config still lists a name pattern such as `secrets.*`; only a plain extension pattern such as `*.py` is taken literally. Hidden folders are included. Path only, the content is never opened. |
| `config` | warning | An `secret_scan_exclude` entry whose `until` date has passed (see below). Every entry you wrote in `kb.config.json` (`exempt_dirs`, `exempt_names`, `secret_scan_exclude`, `orphans_scope`, `ignore_links`; `required_paths` under `required`) that matches nothing in this vault, so a typo or a wrong pattern does not stay silent. Entries for hidden folders and `node_modules` are skipped (they are never read). |
| `secret-in-example` | warning | The same patterns inside a fenced code block of a Markdown note (documentation examples), so real findings stay visible. Any language (`bash`, `powershell`, `console`, `json`, `yaml`, ...), `~~~` fences, indented fences, and fences inside a quote or an Obsidian callout (`> ```bash`). |
| `secret-in-capture` | warning | The same patterns in **captured data**: a `.json`/`.jsonl`/`.ndjson` file larger than 1 MB, a line of such a file longer than 5,000 characters, or any file in a folder listed in `capture_dirs`. Logs and exports hold what people typed and tools printed; the finding is a warning to deal with, not an error. A private-key block (PEM) is always an error. Files over 20 MB are not read; the summary line says how many were left out (`stats.secret_scan_skipped_oversize_files`). |

## Secret checks: one set of skip rules
Every secret check (content scan, `secret-in-example`, key files, `.env*` files) skips the same places: `secret_scan_exclude`, `exempt_dirs`, and **dependency folders** (`site-packages/`, `node_modules/`, `vendor/`, `.venv/`, `venv/`, `__pycache__/`, `bower_components/`), the last ones always, even when `exempt_dirs` does not list them: a test key inside a package is not your secret. The summary line and `stats.secret_scan_skipped_dependency_files` say how many files the tool saw inside dependency folders and left out (hidden folders and `node_modules` are never listed, so they are not counted).

## Exclusions that expire
`secret_scan_exclude` normally maps a path to the reason it is excluded. For something that is public **on purpose** and only for a while (a sign-up token without permissions, a demo key), give the exclusion a last day: `{"settings/signup.json": {"reason": "public sign-up token, no permissions", "until": "2026-12-31"}}`, or a list of `{"path", "reason", "until"}` objects. The date is `YYYY-MM-DD` and the last day is included. After it, the exclusion no longer applies: findings in those files return, `secrets` and `secret-file` as errors whose message ends with `exclusion expired YYYY-MM-DD: <reason>` (the value is never printed), and `config` warns about the entry. If another, still valid, exclusion covers the same file, the file stays excluded. A malformed date, an unknown field or an empty reason is a configuration error (exit code 2).

## Encodings
Notes, config files and the files the secret scan reads may start with a UTF-8 BOM, or be UTF-16 with a BOM (what Windows PowerShell 5 writes with `>` and `Out-File`): the BOM is ignored and the text is read normally, so frontmatter, links and secret findings (with the right line number) are the same as for a plain UTF-8 file. `kb_frontmatter` keeps a UTF-8 BOM when it writes and **skips a UTF-16 note** (it reports why and writes nothing): convert it to UTF-8 first.

Every tool answers `--version` (the kit version, also in `stats.kit_version`).

## Slow or synced folders
- On Windows, files that exist **only in the cloud** (OneDrive "online only" and similar) are never opened, because opening one downloads it. They are not counted as notes, remain valid link targets, and the summary line says how many were skipped.
- `--path <subfolder, note or glob>` reports only there and **opens only the notes in it**, which is much faster on a big or synced vault. Links are still resolved against the whole vault, by file name, without opening anything. The orphan check needs the links of every note, so with `--path` it is left out unless you ask for it with `--checks` (the summary line says so); statistics (`--stats`, `--by-folder`, `--fields`) then describe the path only.
- The summary line ends with the run time (`in 0.7s`; also `stats.seconds`).
- A broken link says where a note with the same file name is, if there is one: `unresolved md link -> old/Note.md; found at new/Note.md`.

## Obsidian behaviour
- Wikilinks resolve **by file name anywhere in the vault** (case-insensitive). The accepted forms are listed in the block below. Embeds also resolve to non-Markdown files (images, PDFs) by full file name.

  ```
  [[Note]]            by file name, anywhere in the vault
  [[Folder/Note]]     with a folder prefix
  [[Note|shown text]] alias part (the table form is Note\|shown text)
  [[Note#Heading]]    heading link, checked unless check_headings is false
  [[Note^block-id]]   block link, the target note is checked
  ![[image.png]]      embed, also for non-Markdown files
  [text](path.md)     plain Markdown link, relative to the note, %20 allowed
  ```

- A name that matches several notes resolves to the one in the same folder if there is one; otherwise it is reported as ambiguous.
- `aliases` in frontmatter are read. Obsidian does not resolve a link written with an alias as its target, so such a link is reported together with the fix (use the note name plus the alias part); set `"resolve_aliases": true` if you rely on a plugin that does.
- Frontmatter: quoted values, empty values, inline lists (`[a, b]`), multi-line lists (`key:` followed by `- item`), block scalars and nested maps are all read.
- Code blocks, inline code and `%% comments %%` are ignored for links. Folders whose name starts with a dot (`.obsidian`, `.trash`, `.git`) and `node_modules` are never read. Notes in `exempt_dirs` are not checked for schema, orphans or outgoing links, and are left out of the index. A single folder name or glob such as `venv/` or `old-*/` matches at any depth; a longer path or glob such as `docs/vendor/` or `docs/*/vendor/` matches from the vault root. Put third-party documentation and code there first: it can be half of a vault and hides the real findings. `--stats` reports exempt notes separately.

## kb.config.json keys used by this tool
`exempt_names` (default `README.md`, `CLAUDE.md`, `AGENTS.md`), `orphan_ok_types`, `secret_file_patterns`, `exempt_dirs`, `enums`, `required_fields`, `date_fields`, `required_paths` (folder prefix ending in `/`, exact path or glob), `ignore_links` (a glob for the link target as written, e.g. `*todo*`; or `source:<folder or glob>` to ignore every unresolved link found in the notes under that path), `resolve_aliases`, `check_headings`, `secret_scan`, `waiting_marker` (text that marks an open item; `--stats` counts the lines that contain it). Also: `orphans_scope`, `secret_scan_extensions` (dotted file extensions; default `[".md"]`; `.env` also covers `.env.local` and similar), `secret_scan_exclude` (object: path or glob -> mandatory reason).

## kb_frontmatter

Adds frontmatter to many notes at once from a JSON mapping, and **proves** that no body byte changed.

```
python -I tools/kb_frontmatter.py --root <vault> --map mapping.json            # dry run (default): shows what would be written
python -I tools/kb_frontmatter.py --root <vault> --map mapping.json --write    # writes, verifying every note
python -I tools/kb_frontmatter.py --root <vault> --map mapping.json --write --merge   # also add MISSING fields to notes that already have frontmatter
python -I tools/kb_frontmatter.py --root <vault> --map mapping.json --write --report hashes.json   # new .json file with a body hash per note
```

The mapping is one JSON object: `{"Folder/Note.md": {"title": "...", "type": "source", "domain": "...", "status": "draft", "summary": "...", "tags": ["a", "b"]}, ...}`. Paths are relative to the vault. The whole mapping is validated first: one mistake and nothing is written (exit 2).

- A note that already has frontmatter is skipped (with `--merge`, only fields it does not have yet are added, and its existing lines are left untouched).
- `updated` is taken from the file's modification date when the mapping does not give one (`--no-auto-updated` turns that off).
- Text values are written as JSON strings, which are valid YAML, so colons, quotes, `#`, backslashes, non-ASCII text and line breaks are safe. Dates in `date_fields` stay plain dates.
- A file's BOM and line endings (CRLF or LF) are kept. Notes are replaced through a temporary file.
- **After every write the note is read back and the SHA-256 of its body is compared with the one taken before.** If it differs, the original bytes are put back and the run stops (exit 3). The report lists the hashes.
- Skipped, never touched: exempt notes, notes in hidden folders, files that are not `.md`, notes with an unclosed frontmatter delimiter, and cloud-only files (not downloaded).

## kb_backup

Backs up only what matters and verifies it later. It writes one ZIP, always **outside** the vault.

```
python -I tools/kb_backup.py --root <vault> --dest <folder outside the vault>          # kb-backup-<date-time>.zip
python -I tools/kb_backup.py --root <vault> --verify <archive.zip>                      # compare the vault with the archive
```

- The ZIP holds every note (`*.md`, same folder structure; notes under `exempt_dirs` such as third-party documentation are left out), the whole `_system` folder, `kb.config.json`, and a manifest with a SHA-256 for each file. Attachments are not included.
- It refuses a destination inside the vault, never overwrites an archive, checks the new archive before keeping it, and lists cloud-only files in the manifest instead of downloading them.
- `--verify` checks the archive itself, then every note in it against the vault as it is now: the note must be identical, or differ **only in its frontmatter** (same body bytes, same BOM). Anything else is `BODY DIFFERS` (exit 1). Notes that no longer exist fail too, unless `--allow-missing`. Notes created after the backup are only counted. Cloud-only notes are reported as not checked.
- Together with `kb_frontmatter`: back up, write frontmatter, run `--verify`. Two independent checks that the bodies are untouched.

## kb_querystats

Measures a query test from a Claude Code session transcript instead of trusting the agent's own report. Read-only, standard library only, no network.

```
python -I tools/kb_querystats.py --session <session>.jsonl --only-under <vault>            # one table, one row per question
python -I tools/kb_querystats.py --latest --project-dir <folder with the .jsonl files> --only-under <vault>
python -I tools/kb_querystats.py --session <session>.jsonl --no-paths --json                # counts only, for a report that leaves your organization
```

Claude Code keeps every session as a JSONL file, normally under `~/.claude/projects/<project folder>/<session id>.jsonl` (the project folder is the working directory with its separators replaced by dashes). Run each query-test run (before, after) in its **own fresh session**, so that one file is one run and every question you typed is one row.

- **Files:** per question, the number of **distinct** files opened: the `Read` calls plus any file named as the `path` of a `Grep` or `Glob`. Searches over a folder or a pattern are counted separately (`searched`). `--only-under <vault>` counts a file only if it is inside the vault (the others are reported apart). `--include-bash` also counts file arguments of `cat`, `head`, `sed`, `grep`, `rg` and similar shell commands: a heuristic, off by default.
- **Tokens:** from the `usage` fields of the agent's replies, summed over the distinct API requests of the question: input, cache read, cache write and output, and `total` (their sum), `new` (without cache reads) and `context peak` (the largest single request). Cache reads re-read the earlier conversation on every request and dominate `total` in a long session, so compare before and after **with the same column**.
- **What a question is:** a message you typed. Tool results, markup injected by the program, summaries and messages from other sessions are not questions; work they trigger is added to the question before them. That is why a query test belongs in a fresh session that receives only your questions.
- **Privacy:** it never prints what was said, a command, or what a file contained: only counts and, unless `--no-paths`, file paths.
- **One question per message:** a message is one question. If you sent several together, `--split-numbered` divides a message that holds numbered items (`1.`, `2)`, `Q3:`) into one row per item, **only when** the agent's replies carry the same numbers in order and each answer is in its own message. The work up to and including the message that answers item *k* is item *k*; work after the last answer is a row "(after last answer)". Otherwise the question stays one row, with the reason (`several answers in one message`, `answer markers found for 2 of 3 items`, `answer markers out of order or repeated`) and totals only. A file read for two items counts in both parts; the question row keeps the whole message and the `all` row adds up questions only. In `--json` the parts are under `parts` and the status under `split`. Neither the questions nor the answers are ever printed.
- **Robustness:** the transcript format is not formally documented. Lines that are not recognized are skipped (and counted); if no `usage` or no user questions can be found the summary says `fields not found` and the exit code is 1. Work done by sub-agents is included only if it appears in the transcript file. `--last N` keeps the last N questions.
- It does not know about other agent programs: for those, use an estimate and mark it.

## kb_secure_move

Moves a sensitive file (a file the linter found a real secret in, a key file, an identity file) out of a synced vault, safely and with a record. Standard library only, no network. **It does nothing unless you pass `--apply`**: the default is a dry run.

```
python -I tools/kb_secure_move.py --root <vault> --file settings/identity.json --dest <folder outside the vault>                       # dry run: shows the plan
python -I tools/kb_secure_move.py --root <vault> --file settings/identity.json --dest <folder outside the vault> --apply --approved-by "NAME" --reason "why"
```

For each `--file` (repeat the flag for several): (1) copy to `--dest`, keeping the path inside the vault; (2) compare the SHA-256 of the copy with the original; (3) only if they are identical, send the original to the **system trash** (Recycle Bin, Trash), never a permanent delete; (4) append one row to the log (`<dest>/SECURE-MOVE-LOG.md`, or `--log`): when, what, from which vault, to where, SHA-256, who approved, reason and result. The content is never printed or logged.
- **Refuses** (nothing changes for that file): a path outside the vault or `..`, a folder, a missing file, a symbolic link, an online-only file (it is not opened), a destination file that already exists (nothing is overwritten). The destination must be **outside the vault** and not look like a synced folder (OneDrive, Dropbox, Google Drive, iCloud; `--allow-synced-dest` if you are sure). `--apply` needs `--approved-by`.
- If the copy does not match, the copy is removed and the original stays. If the trash refuses, the copy stays, the log row says `COPIED ONLY: original still in the vault` and the exit code is 1. If the log cannot be written, the output lists what to record by hand.
- `--trash-dir DIR` sends the original to a folder outside the vault instead of the system trash. Exit code: 0 = done, 1 = a file was not moved, 2 = bad usage.
- Moving the file does not remove the secret from the vault's history (git, sync versions, backups) and does not make the secret safe: **rotate the key**. Then add the finding to the install log.

## kb_index

Builds a computed index page from the frontmatter of your notes. Python 3 standard library only, no network. It prints to the screen by default and only writes with `--write`.

```
python -I tools/kb_index.py --root <vault>            # print the index, write nothing
python -I tools/kb_index.py --root <vault> --write    # write the file named by index_output (the only file it ever writes)
python -I tools/kb_index.py --root <vault> --check    # exit 1 if the file is missing or out of date
```

`--root` defaults to the current folder; settings come from `kb.config.json` in the vault root.

## Output
- Every note that has a frontmatter, except exempt names/folders and the index file itself, grouped by `index_group_by` (default: `type`; use `["domain", "type"]` for two levels, `[]` for one flat table). Rows are sorted by path.
- Columns come from `index_columns`. A list value such as `tags` is shown comma-separated.
- **Scope (`index_scope`):** `valid` (default) lists only notes whose frontmatter passes the schema (required fields, allowed values, real dates), `required` lists only notes under `required_paths`, `all` lists every note with a frontmatter. Old frontmatter written for another system would otherwise flood the index. When notes are left out, the page says how many (`Left out (valid: ...): N notes with frontmatter`); `kb_lint --schema` lists them.
- With `"index_link_style": "wikilink"` (default) the title is a wikilink made from the note path and its title, so the index shows up in Obsidian's graph and backlinks; use `"path"` for plain titles.
- `index_redact` hides the summary of notes whose field has one of the listed values, for example `{"domain": ["personal"]}`; the row stays, the summary becomes `(redacted)`.
- The file starts with a frontmatter built from `index_frontmatter` plus `updated` (the newest valid date found in the notes), then a marker comment. There is no timestamp, so two runs produce identical files and `--check` is stable.

## Safety
- It will not overwrite an existing file that lacks the marker comment (exit code `2`), so a wrong `index_output` cannot destroy a note.
- `index_output` must be a relative `.md` path inside the vault.
- Folders starting with a dot (`.obsidian`, `.trash`, `.git`) are never read.

Exit codes: `0` ok, `1` `--check` found a missing or stale index, `2` bad usage or config.

## kb.config.json keys used by this tool
`exempt_names`, `exempt_dirs`, `date_fields`, `index_output`, `index_title`, `index_group_by`, `index_columns`, `index_redact`, `index_link_style`, `index_frontmatter`.

Run the tests from the kit folder: `python -I -m unittest discover -s tests -v`.
