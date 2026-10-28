#!/usr/bin/env python3
"""kb_lint: read-only checks for a vault of Markdown notes (Obsidian-friendly, stdlib only).

Checks
  links     unresolved wikilinks / embeds / Markdown links (Obsidian rules: a wikilink resolves by
            file name anywhere in the vault, aliases and headings supported), ambiguous names
  orphans   notes that no other note links to or embeds
  schema    (--schema) required frontmatter fields, allowed values, date format
  required  notes under `required_paths` must have a valid frontmatter (error)
  secrets   key/token-looking strings (the value is never printed, only file:line and the pattern);
            files that look like key or secret files by name (secret-file, path only)
  config    entries in kb.config.json that match nothing (so a typo or a wrong glob does not stay silent)

Everything is configured in `kb.config.json` at the vault root (see kb.config.example.json).
The tool never writes, renames or deletes anything and never uses the network.

Usage:  python -I kb_lint.py [--root VAULT] [--config FILE] [--checks a,b] [--schema]
                             [--path SUBFOLDER] [--stats [--by-folder [--depth N]] [--fields]] [--json] [--limit N] [--strict]
Exit code: 0 = clean, 1 = errors (or warnings with --strict), 2 = bad usage or config.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_common as kb  # noqa: E402

CHECKS = ("links", "orphans", "schema", "required", "secrets", "config")
TEXT_EXT = {".md", ".txt", ".json", ".yml", ".yaml", ".csv", ".toml", ".ini", ".cfg", ".env", ".js", ".ts", ".py", ".sh"}
MAX_SCAN_BYTES = 20_000_000   # larger files are not read; the summary says how many were left out
CAPTURE_BYTES = 1_000_000      # a JSON file larger than this is treated as captured data (a log or an export)
CAPTURE_LINE = 5_000           # a JSON line longer than this is minified or generated data
CAPTURE_EXT = {".json", ".jsonl", ".ndjson"}
SECRET_PATTERNS = [
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai-style-key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer-token", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{30,}", re.I)),
]
PLACEHOLDER_VALUE = re.compile(r"^(your|example|sample|placeholder|changeme|xxx|<|\$\{?)|x{6,}|\*{4,}", re.I)

# A keyword assignment (password, secret, token, api key; also client_secret, $AccessToken, GITHUB_TOKEN, ...) is reported only
# when the value written next to it IS the secret: a string literal. A value that comes from a function call, a variable, an
# environment read, a prompt or a config lookup (token = Get-Token(), $t = $env:X, key = os.environ[..], password = getpass())
# is never reported: nothing secret is written there.
SECRET_LHS = re.compile(
    r"[\w.$\-]*?(password|passwd|secret|token|api[_-]?key|private[_-]?key(?:[_-]?pem)?)(?!\w)[\"']?"
    r"(?:(?:\s*:\s*[A-Za-z_][\w\[\], .|]*)?\s*(?::=|=>|=)|\s*:)\s*", re.I)
QUOTED_LITERAL = re.compile(r"(?:[rRbBuUfF]{1,2})?([\"'`])((?:\\.|(?!\1).)*)\1")
SECURE_STRING_LITERAL = re.compile(r"ConvertTo-SecureString\s+(?:-String\s+)?([\"'])(.*?)\1", re.I)
UNQUOTED_KEYLIKE = re.compile(r"([A-Za-z0-9_\-/+=.]{24,})(?![\w(\[.])")
# Where an unquoted value is an expression (a name, a call), so only a quoted literal counts. In notes, config files and
# shell scripts an unquoted value is itself a literal, so a long key-like one counts.
EXPRESSION_EXT = {".py", ".pyi", ".ps1", ".psm1", ".psd1", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".java", ".kt", ".go",
                  ".rs", ".rb", ".php", ".cs", ".c", ".cpp", ".h", ".hpp"}
SCRIPT_SUGGESTION = (".py", ".ps1", ".sh", ".bat", ".cmd", ".js", ".ts", ".yml", ".yaml", ".json", ".env")


def literal_is_secret_like(content: str) -> bool:
    if len(content) < 16 or re.search(r"[\s${}<>%\\]", content):
        return False  # too short, or interpolation, a template, a path with backslashes
    if content.lower().startswith(("http://", "https://")) and "@" not in content:
        return False
    if content.startswith(("/", "./", "~/")) or PLACEHOLDER_VALUE.search(content):
        return False
    return not re.fullmatch(r"[A-Za-z]+(?:[_.][A-Za-z]+)+", content)  # an identifier or an environment variable name


def private_key_literal(content: str) -> bool:
    """A long string next to a field such as privateKey / private_key / privateKeyPem: a key (PEM text keeps its escaped
    line breaks, so backslashes are allowed here)."""
    if len(content) < 40 or re.search(r"[\s${}<>%]", content):
        return False
    return not (content.lower().startswith(("http://", "https://", "/", "./", "~/")) or PLACEHOLDER_VALUE.search(content))


def assignment_secret(line: str, ext: str) -> bool:
    """True when a keyword is assigned a literal that looks like a secret (see SECRET_LHS). `ext` is the file type:
    in Markdown a fenced block can hold any language (and a hit there is only a warning), so the file type decides."""
    expression_context = ext in EXPRESSION_EXT
    for m in SECRET_LHS.finditer(line):
        rest = line[m.end():]
        is_private = m.group(1).lower().startswith("private")
        secret_like = private_key_literal if is_private else literal_is_secret_like
        secure = SECURE_STRING_LITERAL.match(rest)
        if secure:
            if secret_like(secure.group(2)):
                return True
            continue
        quoted = QUOTED_LITERAL.match(rest)
        if quoted:
            if secret_like(quoted.group(2)):
                return True
            continue
        if expression_context:
            continue
        keylike = UNQUOTED_KEYLIKE.match(rest)
        if keylike:
            value = keylike.group(1)
            if (re.search(r"\d", value) and re.search(r"[A-Za-z]", value) and not value.lower().startswith(("http://", "https://"))
                    and secret_like(value)):
                return True
    return False


def scripts_not_scanned(vault: kb.Vault) -> int:
    """Script and config files (by extension) that exist but are not covered by secret_scan_extensions."""
    wanted = {e.lower() for e in vault.cfg["secret_scan_extensions"]}
    count = 0
    for rel in vault.readable_files() + vault.dotenv_files:
        name = rel.rsplit("/", 1)[-1].lower()
        ext = ".env" if name.startswith(".env") else Path(rel).suffix.lower()
        if rel == kb.CONFIG_NAME:
            continue  # the kit's own settings file is not a script
        if ext in SCRIPT_SUGGESTION and ext not in wanted and vault.in_scope(rel) and not vault.secrets_skipped(rel):
            count += 1
    return count


@dataclass
class Finding:
    check: str
    severity: str  # "error" | "warning"
    path: str
    line: int
    message: str


def applicable(vault: kb.Vault):
    return [rel for rel in vault.notes if not vault.is_exempt(rel)]


# ---- links and orphans --------------------------------------------------------
def scoped(vault: kb.Vault):
    """Applicable notes inside the --path scope (all applicable notes without --path)."""
    return [rel for rel in applicable(vault) if vault.in_scope(rel)]


def found_at_hint(vault: kb.Vault, target: str) -> str:
    """For an unresolved link: other files in the vault with the same file name (a note that was moved)."""
    name = unquote(target.split("#")[0].split("?")[0]).replace(chr(92), "/").rstrip("/").rsplit("/", 1)[-1].lower()
    if not name:
        return ""
    hits = sorted(set(vault.by_filename.get(name, [])) | set(vault.by_stem.get(name[:-3] if name.endswith(".md") else name, [])))
    if not hits:
        return ""
    more = f" (+{len(hits) - 3} more)" if len(hits) > 3 else ""
    return "; found at " + ", ".join(hits[:3]) + more


def link_ignored(vault: kb.Vault, patterns, rel: str, target: str) -> bool:
    """An ignore_links entry is a glob for the link target as written; "source:<folder or glob>" ignores
    every unresolved link found in the notes under that path instead."""
    for pat in patterns:
        if pat.lower().startswith("source:"):
            hit = kb.dir_match(pat[7:].strip(), rel)
        else:
            hit = fnmatch.fnmatch(target.lower(), pat.lower())
        if hit:
            vault.ignore_used[pat] += 1
            return True
    return False


def check_links(vault: kb.Vault, need_inbound: bool = True):
    findings, inbound, ignored = [], defaultdict(set), 0
    ignore = vault.cfg["ignore_links"]
    cloud_only = set(vault.cloud_only)
    vault.ignore_used = defaultdict(int)
    for rel in vault.notes:
        if vault.in_exempt_dir(rel):
            continue  # templates and similar hold placeholder links; they still count as link targets
        if not need_inbound and not vault.in_scope(rel):
            continue
        for kind, target, anchor, line, _embed in kb.extract_links(kb.mask(vault.text(rel))):
            if kind == "wiki" and not target:
                continue  # [[#Heading]]: a link inside the same note
            hits = vault.resolve(rel, kind, target)
            if hits is None:
                continue
            if not hits:
                alias_owner = vault.by_alias.get(target.lower(), []) if kind == "wiki" else []
                if alias_owner and vault.cfg["resolve_aliases"]:
                    hits = alias_owner
                elif link_ignored(vault, ignore, rel, target):
                    ignored += 1
                    continue
                elif alias_owner:
                    findings.append(Finding("links", "error", rel, line,
                                            f"[[{target}]] is an alias of {alias_owner[0]}; Obsidian does not resolve "
                                            f"links by alias, write [[{alias_owner[0][:-3]}|{target}]]"))
                    inbound[alias_owner[0]].add(rel)
                    continue
                else:
                    findings.append(Finding("links", "error", rel, line,
                                            f"unresolved {kind} link -> {target}{found_at_hint(vault, target)}"))
                    continue
            if kind == "wiki" and "/" not in target and len(hits) > 1:
                here = rel.rsplit("/", 1)[0] if "/" in rel else ""
                near = [h for h in hits if (h.rsplit("/", 1)[0] if "/" in h else "") == here]
                if near:
                    hits = near
                else:
                    findings.append(Finding("links", "warning", rel, line,
                                            f"ambiguous link [[{target}]] matches {len(hits)} files: " + ", ".join(hits[:4])))
            for hit in hits:
                if hit != rel:
                    inbound[hit].add(rel)
            if (anchor and kind == "wiki" and vault.cfg["check_headings"] and hits[0].lower().endswith(".md")
                    and hits[0] not in cloud_only):  # reading a cloud-only file would download it
                if kb.norm_heading(anchor) not in vault.headings_of(hits[0]):
                    findings.append(Finding("links", "warning", rel, line,
                                            f"heading not found: [[{target}#{anchor}]]"))
    return [f for f in findings if vault.in_scope(f.path)], inbound, ignored


def orphan_scope(vault: kb.Vault):
    """Patterns that limit the orphan check: orphans_scope, else required_paths, else the whole vault."""
    return vault.cfg["orphans_scope"] or vault.cfg["required_paths"] or ["*"]


def check_orphans(vault: kb.Vault, inbound):
    scope = orphan_scope(vault)
    ok_types = set(vault.cfg["orphan_ok_types"])
    out = []
    for rel in scoped(vault):
        if inbound.get(rel) or not any(kb.path_matches(p, rel) for p in scope):
            continue
        if ok_types and any(t in ok_types for t in kb.as_list((vault.frontmatter(rel) or {}).get("type", []))):
            continue  # leaf notes such as sources or correspondence are not expected to be linked
        out.append(Finding("orphans", "warning", rel, 1, "no other note links to or embeds this note"))
    return out


# ---- frontmatter --------------------------------------------------------------
def validate_fields(vault: kb.Vault, rel: str, fm: dict):
    return [Finding("schema", "error", rel, 1, message) for message in kb.schema_problems(vault.cfg, fm)]


def check_schema(vault: kb.Vault):
    """--schema: validate every non-exempt note that has a frontmatter; a note without one is only a
    warning unless it falls under required_paths (the `required` check reports those as errors)."""
    findings = []
    for rel in scoped(vault):
        fm = vault.frontmatter(rel)
        if fm is None:
            if not vault.in_required_scope(rel):
                findings.append(Finding("frontmatter", "warning", rel, 1, "no frontmatter (not under required_paths)"))
            continue
        findings += validate_fields(vault, rel, fm)
    return findings


def check_required(vault: kb.Vault, fields_too: bool):
    findings, scope = [], [r for r in scoped(vault) if vault.in_required_scope(r)]
    vault.required_pages, vault.required_with_fm = len(scope), 0
    for rel in scope:
        fm = vault.frontmatter(rel)
        if fm is None:
            findings.append(Finding("required", "error", rel, 1, "missing frontmatter (note is under required_paths)"))
            continue
        vault.required_with_fm += 1
        if fields_too:
            findings += validate_fields(vault, rel, fm)
    for pattern in vault.cfg["required_paths"]:
        if not any(kb.path_matches(pattern, r) for r in vault.notes):
            findings.append(Finding("required", "warning", kb.CONFIG_NAME, 1,
                                    f"required_paths entry matches no note: {pattern}"))
    return findings


# ---- secrets --------------------------------------------------------------------
def scannable(vault: kb.Vault):
    """Files the secret scan covers: the extensions in secret_scan_extensions (plus .env* files when
    ".env" is listed), minus secret_scan_exclude, size-capped."""
    wanted = {e.lower() for e in vault.cfg["secret_scan_extensions"]}
    vault.secret_oversize = []
    candidates = vault.readable_files() + (vault.dotenv_files if ".env" in wanted else [])
    for rel in candidates:
        name = rel.rsplit("/", 1)[-1].lower()
        if not (Path(rel).suffix.lower() in wanted or (".env" in wanted and name.startswith(".env"))):
            continue
        if not vault.in_scope(rel) or vault.secrets_skipped(rel):
            continue
        try:
            if (vault.root / rel).stat().st_size <= MAX_SCAN_BYTES:
                yield rel
            else:
                vault.secret_oversize.append(rel)
        except OSError:
            continue


SECRET_FILE_SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", ".trash", "site-packages", "vendor", "bower_components"}
# Code, documentation and public keys are never key files because of their NAME (secrets.py, secrets.md, id_rsa.pub).
# Only a pattern that is a plain extension ("*.pem") is taken literally, so you can still list "*.py" on purpose.
NOT_KEY_FILE_EXT = {".py", ".pyi", ".pyc", ".ipynb", ".md", ".markdown", ".rst", ".adoc", ".html", ".htm", ".js", ".mjs", ".cjs",
                    ".ts", ".tsx", ".jsx", ".java", ".kt", ".go", ".rs", ".rb", ".php", ".cs", ".c", ".h", ".cpp", ".hpp",
                    ".ps1", ".sh", ".bat", ".cmd", ".css", ".scss", ".pub"}
PLAIN_EXTENSION = re.compile(r"\*\.[A-Za-z0-9]+")


def secret_name_match(pattern: str, rel: str, name: str) -> bool:
    if not PLAIN_EXTENSION.fullmatch(pattern) and Path(name).suffix.lower() in NOT_KEY_FILE_EXT:
        return False
    if pattern.endswith("/") or "/" in pattern:
        return kb.dir_match(pattern, rel)
    return fnmatch.fnmatch(name.lower(), pattern.lower())


def dependency_files_skipped(vault: kb.Vault) -> int:
    """Files the tool can see inside dependency folders, which the secret checks leave out."""
    return sum(1 for rel in vault.files if vault.in_package_dir(rel) and vault.in_scope(rel))


def check_secret_files(vault: kb.Vault):
    """Files that are key files by their EXTENSION (*.pem, *.key, *.pfx, *.p12, *.jks, *.keystore, *.ppk, id_rsa*) or sit in a
    .local_secrets folder, whatever else is in their name. Code and documentation files are never reported because of their
    name. Hidden folders are included; the content is never opened. Path only. exempt_dirs, secret_scan_exclude and dependency
    folders are skipped like in every other secret check."""
    patterns, out = vault.cfg["secret_file_patterns"], []
    if not patterns:
        return out
    for dirpath, dirnames, filenames in os.walk(vault.root):
        dirnames[:] = sorted(d for d in dirnames if d.lower() not in SECRET_FILE_SKIP)
        for name in sorted(filenames):
            rel = (Path(dirpath) / name).relative_to(vault.root).as_posix()
            if not vault.in_scope(rel) or vault.secrets_skipped(rel):
                continue
            if any(secret_name_match(pat, rel, name) for pat in patterns):
                out.append(Finding("secret-file", "error", rel, 1, "key file, found by its extension or folder (content not read)"))
    return out


def mark_expired(vault: kb.Vault, findings):
    """A finding in a file whose exclusion has expired says so: the file was excluded on purpose, the date has passed."""
    for f in findings:
        info = vault.expired_exclusion(f.path)
        if info and f.check in ("secrets", "secret-file", "secret-in-capture", "secret-in-example"):
            f.message += f" (exclusion expired {info[1]}: {info[2]})"
    return findings


def check_secrets(vault: kb.Vault):
    """Secret-looking strings. In a Markdown note, a match inside a fenced code block is a separate,
    lower-severity finding (`secret-in-example`, warning) so documentation examples do not drown real hits."""
    findings, notes = [], set(vault.notes)
    vault.secret_scan_counts = defaultdict(int)
    vault.secret_oversize = []
    for rel in scannable(vault):
        try:
            text = vault.text(rel) if rel in notes else kb.read_text(vault.root / rel)  # notes are already cached
        except OSError:
            continue
        is_env = rel.rsplit("/", 1)[-1].lower().startswith(".env")
        ext = ".env" if is_env else Path(rel).suffix.lower()
        vault.secret_scan_counts[".env*" if is_env else ext] += 1
        fenced = kb.fenced_lines(text) if rel in notes else set()
        try:
            big_json = ext in CAPTURE_EXT and (vault.root / rel).stat().st_size > CAPTURE_BYTES
        except OSError:
            big_json = False
        captured_file = big_json or vault.in_capture_dir(rel)
        for no, line in enumerate(text.splitlines(), 1):
            hits = [name for name, rx in SECRET_PATTERNS if rx.search(line)]
            if not hits and assignment_secret(line, ext):
                hits = ["keyword assignment with a literal value"]  # only when no specific pattern matched: one line = one finding
            long_json_line = ext in CAPTURE_EXT and len(line) > CAPTURE_LINE
            for name in hits:
                if no in fenced:
                    findings.append(Finding("secret-in-example", "warning", rel, no,
                                            f"possible secret inside a code block: {name} (value not shown)"))
                elif (captured_file or long_json_line) and name != "private-key-block":
                    findings.append(Finding("secret-in-capture", "warning", rel, no,
                                            f"possible secret in captured data (log, export or large JSON): {name} (value not shown)"))
                else:
                    findings.append(Finding("secrets", "error", rel, no, f"possible secret: {name} (value not shown)"))
    return findings


# ---- stats ----------------------------------------------------------------------
def pool(vault: kb.Vault):
    """Notes that statistics are computed for: the --path scope, or every note."""
    return [rel for rel in vault.notes if vault.in_scope(rel)]


def waiting_counts(vault: kb.Vault):
    marker = vault.cfg["waiting_marker"].lower()
    counts = {}
    if not marker:
        return counts
    for rel in pool(vault):
        n = sum(1 for line in kb.mask(vault.text(rel)).split("\n") if marker in line.lower())
        if n:
            counts[rel] = n
    return counts


def compute_stats(vault: kb.Vault, findings, ignored=0):
    app = scoped(vault)
    with_fm = [r for r in pool(vault) if vault.frontmatter(r) is not None]
    app_fm = [r for r in app if vault.frontmatter(r) is not None]
    by_check = defaultdict(int)
    for f in findings:
        by_check[f.check] += 1
    waiting = waiting_counts(vault)
    return {
        "notes": len(vault.notes),
        "notes_with_frontmatter": len(with_fm),
        "schema_applicable": len(app),
        "applicable_with_frontmatter": len(app_fm),
        "applicable_frontmatter_pct": round(100 * len(app_fm) / len(app), 1) if app else 0.0,
        "links_ignored": ignored,
        "cloud_only_skipped": len(vault.cloud_only),
        "secret_scan_skipped_dependency_files": dependency_files_skipped(vault),
        "scripts_not_scanned": scripts_not_scanned(vault) if vault.cfg["secret_scan"] else 0,
        "secret_scan_skipped_oversize_files": len(getattr(vault, "secret_oversize", [])),
        "kit_version": kb.KIT_VERSION,
        "scope": vault.scope,
        "notes_in_scope": sum(1 for n in vault.notes if vault.in_scope(n)),
        "required_pages": getattr(vault, "required_pages", 0),
        "required_pages_with_frontmatter": getattr(vault, "required_with_fm", 0),
        "secret_scan_files": sum((getattr(vault, "secret_scan_counts", None) or {}).values()),
        "secret_scan_files_by_type": dict(sorted((getattr(vault, "secret_scan_counts", None) or {}).items())),
        "findings_by_check": dict(sorted(by_check.items())),
        "waiting_marker_lines": sum(waiting.values()),
        "waiting_marker_by_note": dict(sorted(waiting.items())),
    }


def check_config(vault: kb.Vault, links_checked: bool):
    """Warn about every entry the user wrote in kb.config.json that matches nothing in this vault.
    Hidden folders and node_modules are never read, so entries for them cannot match and are skipped."""
    cfg, findings = vault.cfg, []
    explicit = cfg.get("_explicit", set())
    every_file = vault.files + vault.dotenv_files

    def invisible(entry: str) -> bool:
        first = entry.replace(chr(92), "/").lstrip("/").split("/")[0].lower()
        return first.startswith(".") or first == "node_modules"

    def warn(key, entries, matches):
        if key not in explicit:
            return
        for entry in entries:
            if not invisible(entry) and not matches(entry):
                findings.append(Finding("config", "warning", kb.CONFIG_NAME, 1, f"{key} entry matches nothing: {entry}"))

    warn("exempt_dirs", cfg["exempt_dirs"], lambda e: any(kb.dir_match(e, f) for f in every_file))
    warn("exempt_names", cfg["exempt_names"], lambda e: any(n.rsplit("/", 1)[-1].lower() == e.lower() for n in vault.notes))
    warn("secret_scan_exclude", list(cfg["secret_scan_exclude"]), lambda e: any(kb.dir_match(e, f) for f in every_file))
    for pattern, last_day in sorted(cfg.get("_exclude_until", {}).items()):
        if kb.today_iso() > last_day:
            findings.append(Finding("config", "warning", kb.CONFIG_NAME, 1,
                                    f"secret_scan_exclude entry expired {last_day}: {pattern} (the files are scanned again; renew the date or remove the entry)"))
    warn("orphans_scope", cfg["orphans_scope"], lambda e: any(kb.path_matches(e, n) for n in vault.notes))
    warn("capture_dirs", cfg["capture_dirs"], lambda e: any(kb.dir_match(e, f) for f in every_file))
    if links_checked:
        warn("ignore_links", cfg["ignore_links"], lambda e: vault.ignore_used.get(e, 0) > 0)
    return findings


def property_counts(vault: kb.Vault):
    """How many notes (among those the schema applies to) use each frontmatter property."""
    counts, with_fm = defaultdict(int), 0
    for rel in scoped(vault):
        fm = vault.frontmatter(rel)
        if fm is None:
            continue
        with_fm += 1
        for key in fm:
            counts[key] += 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))), with_fm


def folder_breakdown(vault: kb.Vault, findings, depth: int):
    """Counts per folder for levels 1..depth. A note counts for every folder level above it;
    notes at the vault root are listed under "(root)"."""
    rows = defaultdict(lambda: {"notes": 0, "exempt": 0, "applicable": 0, "with_frontmatter": 0,
                                "broken_links": 0, "orphans": 0, "secrets": 0, "secret_examples": 0, "secret_captures": 0})

    def keys(rel):
        parts = rel.split("/")[:-1]
        return ["/".join(parts[:i]) for i in range(1, min(depth, len(parts)) + 1)] or ["(root)"]

    for rel in pool(vault):
        exempt = vault.is_exempt(rel)
        has_fm = vault.frontmatter(rel) is not None
        for key in keys(rel):
            row = rows[key]
            row["notes"] += 1
            row["exempt" if exempt else "applicable"] += 1
            if has_fm and not exempt:
                row["with_frontmatter"] += 1
    column = {"links": "broken_links", "orphans": "orphans", "secrets": "secrets", "secret-file": "secrets",
              "secret-in-example": "secret_examples", "secret-in-capture": "secret_captures"}
    for f in findings:
        if f.check in column and f.severity in ("error", "warning") and (f.check != "links" or f.severity == "error"):
            for key in keys(f.path):
                rows[key][column[f.check]] += 1  # a folder that holds only flagged non-note files gets a row too
    out = []
    for key in sorted(rows):
        row = dict(rows[key])
        row["folder"] = key
        row["frontmatter_pct"] = round(100 * row["with_frontmatter"] / row["applicable"], 1) if row["applicable"] else 0.0
        out.append(row)
    return out


def print_folder_table(rows):
    cols = ["notes", "exempt", "applicable", "frontmatter_pct", "broken_links", "orphans", "secrets", "secret_examples", "secret_captures"]
    width = max([len("folder")] + [len(r["folder"]) for r in rows])
    print("folder".ljust(width), *[c.rjust(15) for c in cols])
    for r in rows:
        print(r["folder"].ljust(width), *[str(r[c]).rjust(15) for c in cols])


def run(root, cfg, checks=CHECKS, schema=False, path=None):
    vault = kb.Vault(root, cfg)
    if path is not None and vault.set_scope(path) == 0:
        raise kb.ConfigError(f"--path matches no note: {path}")
    findings, inbound, ignored = [], {}, 0
    if "links" in checks or "orphans" in checks:
        link_findings, inbound, ignored = check_links(vault, need_inbound="orphans" in checks)
        if "links" in checks:
            findings += link_findings
    if "orphans" in checks:
        findings += check_orphans(vault, inbound)
    if "config" in checks:
        findings += check_config(vault, "links" in checks or "orphans" in checks)
    if "required" in checks and cfg["required_paths"]:
        findings += check_required(vault, fields_too=not schema)
    if schema:
        findings += check_schema(vault)
    if "secrets" in checks and cfg["secret_scan"]:
        findings += mark_expired(vault, check_secrets(vault))
        findings += mark_expired(vault, check_secret_files(vault))
    findings.sort(key=lambda f: (f.check, f.path, f.line, f.message))
    vault.links_ignored = ignored
    return vault, findings


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read-only lint for a Markdown vault.")
    ap.add_argument("--version", action="version", version=f"kit {kb.KIT_VERSION}")
    ap.add_argument("--root", default=".", help="vault folder (default: current directory)")
    ap.add_argument("--config", default=None, help=f"config file (default: <root>/{kb.CONFIG_NAME})")
    ap.add_argument("--checks", default=None, help="comma list of: " + ",".join(CHECKS)
                    + " (default: all; with --path the orphan check is left out, see --path)")
    ap.add_argument("--schema", action="store_true", help="validate frontmatter of every note that has one")
    ap.add_argument("--path", default=None, help="report only this subfolder, note or glob. Links still resolve against the whole vault (by file name, "
                         "nothing is opened). The orphan check needs the links of every note, so with --path it only runs when "
                         "you list it in --checks")
    ap.add_argument("--stats", action="store_true", help="print counts instead of the findings list")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--limit", type=int, default=40, help="max findings printed per check (text mode)")
    ap.add_argument("--strict", action="store_true", help="warnings also fail the exit code")
    ap.add_argument("--by-folder", action="store_true", help="with --stats: counts per folder (see --depth)")
    ap.add_argument("--fields", action="store_true", help="with --stats: how many notes use each frontmatter property")
    ap.add_argument("--depth", type=int, default=2, help="folder levels for --by-folder, 1-4 (default 2)")
    args = ap.parse_args(argv)
    started = time.perf_counter()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not 1 <= args.depth <= 4:
        print("--depth must be between 1 and 4", file=sys.stderr)
        return 2
    args.stats = args.stats or args.by_folder or args.fields
    orphans_left_out = args.checks is None and args.path is not None
    chosen = args.checks if args.checks is not None else ",".join(c for c in CHECKS if not (orphans_left_out and c == "orphans"))
    checks = tuple(c.strip() for c in chosen.split(",") if c.strip())
    bad = [c for c in checks if c not in CHECKS]
    if bad:
        print(f"unknown check(s): {', '.join(bad)}", file=sys.stderr)
        return 2
    root = Path(args.root)
    if not root.is_dir():
        print(f"not a folder: {root}", file=sys.stderr)
        return 2
    try:
        cfg, notes = kb.load_config(root, args.config)
    except kb.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    for note in notes:
        print(f"note: {note}", file=sys.stderr)
    try:
        vault, findings = run(root, cfg, checks, args.schema, args.path)
    except kb.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    stats = compute_stats(vault, findings, vault.links_ignored)
    elapsed = time.perf_counter() - started
    stats["seconds"] = round(elapsed, 2)
    folders = folder_breakdown(vault, findings, args.depth) if args.by_folder else None
    if folders is not None:
        stats["by_folder"] = folders
    if args.fields:
        stats["fields"], stats["fields_notes"] = property_counts(vault)
    failed = any(f.severity == "error" or args.strict for f in findings)
    if args.json:
        payload = {"stats": stats} if args.stats else {"stats": stats, "findings": [asdict(f) for f in findings]}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.stats:
        for key, value in stats.items():
            if key != "by_folder":
                print(f"{key}: {value}")
        if args.fields:
            print()
            print(f"properties used by the {stats['fields_notes']} notes that have frontmatter (schema-applicable notes only):")
            for key, n in stats["fields"].items():
                print(f"  {key}: {n}")
        if folders is not None:
            print()
            print_folder_table(folders)
    else:
        grouped = defaultdict(list)
        for f in findings:
            grouped[f.check].append(f)
        for check, items in grouped.items():
            print(f"== {check}: {len(items)}")
            for f in items[: args.limit]:
                print(f"  [{f.severity}] {f.path}:{f.line}  {f.message}")
            if len(items) > args.limit:
                print(f"  ... {len(items) - args.limit} more (use --limit or --json)")
        scanned = getattr(vault, "secret_scan_counts", None)
        if not cfg["secret_scan"] or "secrets" not in checks:
            scan_note = "secret scan: off"
        else:
            scan_note = "secret scan read: " + (", ".join(f"{n} {kind}" for kind, n in sorted(scanned.items())) or "no files")
        extra = ""
        if vault.scope is not None:
            extra += f"; path: {args.path} ({stats['notes_in_scope']} notes)"
        if cfg["secret_scan"] and "secrets" in checks and dependency_files_skipped(vault):
            extra += f"; secret checks skipped {dependency_files_skipped(vault)} files in dependency folders"
        if cfg["secret_scan"] and "secrets" in checks and scripts_not_scanned(vault):
            extra += f"; scripts not scanned ({scripts_not_scanned(vault)} script files found; see secret_scan_extensions)"
        if getattr(vault, "secret_oversize", None):
            extra += f"; secret scan skipped {len(vault.secret_oversize)} files over {MAX_SCAN_BYTES // 1_000_000} MB"
        if orphans_left_out:
            extra += "; orphans not checked (add --checks to include them)"
        if vault.cloud_only:
            extra += f"; skipped {len(vault.cloud_only)} cloud-only files (not downloaded)"
        print(f"kb_lint: {len(vault.notes)} notes, {len(findings)} findings "
              f"({sum(f.severity == 'error' for f in findings)} errors); {scan_note}{extra}; in {time.perf_counter() - started:.1f}s")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
