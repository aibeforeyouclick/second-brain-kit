#!/usr/bin/env python3
"""kb_frontmatter: add frontmatter to notes from a JSON mapping file, and prove no body byte changed.

The mapping is one JSON object: {"relative/path.md": {"title": "...", "type": "...", ...}, ...}.
For every listed note the tool
  * leaves it alone when it already has a frontmatter (with --merge it adds only the missing fields),
  * writes `updated` from the file's modification date when the mapping does not give one,
  * quotes every text value safely (JSON strings are valid YAML), keeps the file's BOM and line endings,
  * after each write reads the file back and compares the SHA-256 of the body with the one taken before;
    if they differ it restores the original bytes and stops.

Nothing is written without --write (the default is a dry run). Files that exist only in the cloud are skipped,
never downloaded.

Usage:  python -I kb_frontmatter.py --map FILE [--root VAULT] [--config FILE] [--write] [--merge]
                                    [--no-auto-updated] [--json] [--report FILE]
Exit code: 0 = done (or dry run ok), 2 = bad usage, config or mapping (nothing written),
           3 = a verification failed: the file was restored and the run stopped.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_common as kb  # noqa: E402

SIMPLE_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_\-]*")


class MappingError(Exception):
    pass


def load_mapping(vault: kb.Vault, path: Path):
    """Validate the whole mapping first, so a mistake is found before any file is touched."""
    try:
        data = json.loads(kb.read_text(path))
    except (OSError, ValueError) as exc:
        raise MappingError(f"cannot read mapping {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise MappingError("mapping must be a JSON object: {path: {field: value}}")
    problems, mapping = [], {}
    for raw_key, fields in data.items():
        rel = str(raw_key).replace(chr(92), "/").strip()
        rel = rel[2:] if rel.startswith("./") else rel
        if not rel or rel.startswith("/") or ".." in rel.split("/") or re.match(r"^[A-Za-z]:", rel):
            problems.append(f"{raw_key!r}: path must be relative to the vault and stay inside it")
            continue
        if rel in mapping:
            problems.append(f"{rel}: listed twice")
            continue
        if not isinstance(fields, dict) or not fields:
            problems.append(f"{rel}: value must be a non-empty object of fields")
            continue
        for name, value in fields.items():
            ok_scalar = lambda v: isinstance(v, (str, int, float, bool)) and "\x00" not in str(v)
            if not isinstance(name, str) or not name.strip() or "\n" in name:
                problems.append(f"{rel}: bad field name {name!r}")
            elif not (ok_scalar(value) or (isinstance(value, list) and all(ok_scalar(v) for v in value))):
                problems.append(f"{rel}: field {name!r} must be text, number, true/false or a list of those")
        mapping[rel] = fields
    if problems:
        raise MappingError("\n".join(problems))
    return mapping


def render_value(key: str, value, date_keys) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(render_value(key, v, ()) for v in value) + "]"
    if key in date_keys and kb.real_date(value) and len(value) == 10:
        return value  # a plain date, so Obsidian shows a date property
    return json.dumps(value, ensure_ascii=False)


def render_line(key: str, value, date_keys) -> str:
    name = key if SIMPLE_KEY.fullmatch(key) else json.dumps(key, ensure_ascii=False)
    return f"{name}: {render_value(key, value, date_keys)}"


def line_ending(raw: bytes) -> bytes:
    i = raw.find(b"\n")
    return b"\r\n" if i > 0 and raw[i - 1:i] == b"\r" else b"\n"


def plan_note(vault: kb.Vault, rel: str, fields: dict, merge: bool, auto_updated: bool):
    """Compute the new bytes for one note. Returns (entry, new_raw or None)."""
    path = vault.root / rel
    raw = path.read_bytes()
    bom, fm_block, body = kb.split_bytes(raw)
    entry = {"path": rel, "body_sha256_before": kb.sha256(body)}
    if kb.is_utf16(raw):
        entry.update(action="skipped", reason="the note is UTF-16 (a Windows PowerShell file); convert it to UTF-8 first, nothing was written")
        return entry, None
    first = body.splitlines()[:1]
    if not fm_block and first and first[0].rstrip(b"\r\n \t") == b"---":
        entry.update(action="skipped", reason="unclosed frontmatter delimiter; fix the note by hand")
        return entry, None
    existing = kb.parse_frontmatter(fm_block.decode("utf-8", errors="replace")) if fm_block else None
    if existing is not None and not merge:
        entry.update(action="skipped", reason="already has frontmatter (use --merge to add missing fields)")
        return entry, None
    date_keys = set(vault.cfg["date_fields"])
    wanted = dict(fields)
    if auto_updated and "updated" not in wanted and (existing is None or "updated" not in existing):
        wanted["updated"] = datetime.date.fromtimestamp(path.stat().st_mtime).isoformat()
        entry["updated_from_mtime"] = True
    if existing is not None:
        kept = [k for k in wanted if k in existing]
        wanted = {k: v for k, v in wanted.items() if k not in existing}
        entry["kept_existing"] = kept
        if not wanted:
            entry.update(action="skipped", reason="nothing to add (all fields already present)")
            return entry, None
    nl = line_ending(fm_block or raw)
    lines = [render_line(k, v, date_keys) for k, v in wanted.items()]
    new_lines = b"".join(line.encode("utf-8") + nl for line in lines)
    if existing is None:
        new_fm = b"---" + nl + new_lines + b"---" + nl
    else:
        block_lines = fm_block.splitlines(keepends=True)
        new_fm = b"".join(block_lines[:-1]) + new_lines + block_lines[-1]
    entry.update(action="planned", fields_added=list(wanted), lines=lines)
    return entry, bom + new_fm + body


def replace_file(path: Path, data: bytes):
    """Write through a temporary sibling file, then swap it in."""
    tmp = path.with_name(".kbtmp-" + path.name)
    tmp.write_bytes(data)
    try:
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def verify_written(vault: kb.Vault, rel: str, entry: dict) -> bool:
    after = (vault.root / rel).read_bytes()
    bom, fm_block, body = kb.split_bytes(after)
    entry["body_sha256_after"] = kb.sha256(body)
    parsed = kb.parse_frontmatter(after.decode("utf-8", errors="replace"))
    return (entry["body_sha256_after"] == entry["body_sha256_before"] and parsed is not None
            and all(k in parsed for k in entry.get("fields_added", [])))


def apply(vault: kb.Vault, mapping: dict, write: bool, merge: bool, auto_updated: bool):
    report, failed = [], False
    for rel, fields in mapping.items():
        path = vault.root / rel
        if not path.is_file():
            report.append({"path": rel, "action": "error", "reason": "file not found"})
            failed = True
            break
        if rel in vault.cloud_only:
            report.append({"path": rel, "action": "skipped", "reason": "cloud-only file (not downloaded)"})
            continue
        if rel not in vault.notes:
            report.append({"path": rel, "action": "skipped", "reason": "not a note of this vault (hidden folder or not .md)"})
            continue
        if vault.is_exempt(rel):
            report.append({"path": rel, "action": "skipped", "reason": "exempt note (exempt_names / exempt_dirs)"})
            continue
        entry, new_raw = plan_note(vault, rel, fields, merge, auto_updated)
        if new_raw is None:
            report.append(entry)
            continue
        _, _, planned_body = kb.split_bytes(new_raw)
        if kb.sha256(planned_body) != entry["body_sha256_before"]:  # cannot happen; checked before touching the file
            entry.update(action="error", reason="internal check failed before writing; nothing written")
            report.append(entry)
            failed = True
            break
        if not write:
            entry["action"] = "dry-run"
            report.append(entry)
            continue
        original = path.read_bytes()
        replace_file(path, new_raw)
        if verify_written(vault, rel, entry):
            entry["action"] = "written"
            report.append(entry)
            continue
        try:
            replace_file(path, original)  # restore the exact original bytes
            restored = path.read_bytes() == original
        except OSError:
            restored = False
        entry.update(action="restored" if restored else "RESTORE FAILED",
                     reason="body hash differed after writing; original bytes put back" if restored
                     else "body hash differed and the original could not be restored: check this file by hand")
        report.append(entry)
        failed = True
        break
    return report, failed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Add frontmatter from a JSON mapping, verifying that bodies stay byte-identical.")
    ap.add_argument("--version", action="version", version=f"kit {kb.KIT_VERSION}")
    ap.add_argument("--map", required=True, help="JSON file: {relative path: {field: value}}")
    ap.add_argument("--root", default=".", help="vault folder (default: current directory)")
    ap.add_argument("--config", default=None, help=f"config file (default: <root>/{kb.CONFIG_NAME})")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="show what would be written (the default)")
    mode.add_argument("--write", action="store_true", help="write the notes")
    ap.add_argument("--merge", action="store_true", help="in notes that already have frontmatter, add only the missing fields")
    ap.add_argument("--no-auto-updated", action="store_true", help="do not add `updated` from the file modification date")
    ap.add_argument("--json", action="store_true", help="print the full report as JSON")
    ap.add_argument("--report", default=None, help="also write the JSON report to this new file")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
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
    report_path = Path(args.report) if args.report else None
    if report_path is not None and (report_path.exists() or report_path.suffix.lower() != ".json"):
        print("--report must be a new .json file (an existing file is never overwritten)", file=sys.stderr)
        return 2
    vault = kb.Vault(root, cfg)
    try:
        mapping = load_mapping(vault, Path(args.map))
    except MappingError as exc:
        print(f"mapping error:\n{exc}", file=sys.stderr)
        return 2
    report, failed = apply(vault, mapping, args.write, args.merge, not args.no_auto_updated)
    counts = {}
    for entry in report:
        counts[entry["action"]] = counts.get(entry["action"], 0) + 1
    payload = {"mode": "write" if args.write else "dry-run", "counts": counts, "notes": report}
    if report_path is not None:
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for entry in report:
            detail = entry.get("reason") or ", ".join(entry.get("fields_added", []))
            print(f"{entry['action']:>10}  {entry['path']}  {detail}")
        print(f"kb_frontmatter ({payload['mode']}): " + ", ".join(f"{n} {a}" for a, n in sorted(counts.items())))
    if failed:
        print("STOPPED: a verification failed; see the report. Nothing after that note was touched.", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
