#!/usr/bin/env python3
"""kb_index: build a computed index of the notes in a vault from their frontmatter (stdlib only).

Read-only by default (prints to stdout). With --write it writes ONE file, `index_output` from
kb.config.json, and nothing else. It refuses to overwrite an existing file that was not generated
by this tool. --check exits 1 when the file is missing or stale. The output has no timestamp, so
repeated runs are byte-identical.

Usage:  python -I kb_index.py [--root VAULT] [--config FILE] [--write | --check]
Exit code: 0 = ok, 1 = --check found a missing/stale index, 2 = bad usage or config.
"""
from __future__ import annotations

import argparse
import posixpath
import re
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_common as kb  # noqa: E402

MISSING = "(missing)"


def collect(vault: kb.Vault):
    """(pages, left_out): the notes that belong in the index under `index_scope`, and how many notes with frontmatter were left out.
    required = only notes under required_paths; valid = notes whose frontmatter passes the schema; all = every note with frontmatter."""
    scope = vault.cfg["index_scope"]
    pages, left_out = [], 0
    vault.no_frontmatter = 0
    for rel in vault.notes:
        if vault.is_exempt(rel):
            continue
        fm = vault.frontmatter(rel)
        if fm is None:
            vault.no_frontmatter += 1
            continue
        if scope == "required" and not vault.in_required_scope(rel):
            left_out += 1
        elif scope == "valid" and kb.schema_problems(vault.cfg, fm):
            left_out += 1
        else:
            pages.append((rel, fm))
    return pages, left_out


def cell_value(value) -> str:
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value)
    return re.sub(r"\s+", " ", str(value or "")).strip()


def esc(text: str) -> str:
    return text.replace("|", "\\|")


def quoted(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def redacted(cfg, fm) -> bool:
    return any(cell_value(fm.get(field)) in values for field, values in cfg["index_redact"].items())


def title_of(rel, fm) -> str:
    return cell_value(fm.get("title")) or rel.rsplit("/", 1)[-1][:-3]


def render_cell(vault, column, rel, fm) -> str:
    cfg = vault.cfg
    if column == "title":
        title = esc(title_of(rel, fm))
        if vault.link_style() == "wikilink":
            return f"[[{rel[:-3]}\\|{title}]]"
        # a plain Markdown link, relative to the folder of the index file, so it opens in VS Code, GitHub and any viewer
        start = posixpath.dirname(cfg["index_output"]) or "."
        target = quote(posixpath.relpath(rel, start), safe="/._-~()")
        return f"[{title.replace('[', chr(92) + '[').replace(']', chr(92) + ']')}]({target})"
    if column == "path":
        return f"`{rel}`"
    if column == "summary" and redacted(cfg, fm):
        return "(redacted)"
    return esc(cell_value(fm.get(column)))


def newest_date(pages, fields) -> str:
    dates = [str(v)[:10] for _, fm in pages for f in fields for v in kb.as_list(fm.get(f))
             if kb.real_date(str(v))]
    return max(dates) if dates else ""


def frontmatter_block(cfg, pages):
    lines = ["---"]
    for key, value in cfg["index_frontmatter"].items():
        if isinstance(value, list):
            lines.append(f"{key}: [" + ", ".join(quoted(str(v)) for v in value) + "]")
        else:
            lines.append(f"{key}: {quoted(str(value))}")
    newest = newest_date(pages, cfg["date_fields"])
    if newest and "updated" not in cfg["index_frontmatter"]:
        lines.append(f"updated: {newest}")
    lines.append("---")
    return lines if len(lines) > 2 else []


SCOPE_TEXT = {"required": "only notes under required_paths", "valid": "only notes whose frontmatter passes the schema"}


def render(vault: kb.Vault, pages, left_out: int = 0) -> str:
    cfg = vault.cfg
    columns, group_by = cfg["index_columns"], cfg["index_group_by"]
    lines = frontmatter_block(cfg, pages) + [GENERATED, "", f"# {cfg['index_title']}", "",
                                              f"Notes with frontmatter: {len(pages)}"]
    if getattr(vault, "no_frontmatter", 0):
        lines.append(f"Notes without frontmatter (not in this index): {vault.no_frontmatter}")
    if left_out:
        lines.append(f"Left out ({cfg['index_scope']}: {SCOPE_TEXT[cfg['index_scope']]}): {left_out} notes with frontmatter")

    def emit(items, depth):
        if depth == len(group_by):
            lines.extend(["", "| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)])
            for rel, fm in sorted(items, key=lambda it: it[0]):
                lines.append("| " + " | ".join(render_cell(vault, c, rel, fm) for c in columns) + " |")
            return
        field = group_by[depth]
        groups = defaultdict(list)
        for rel, fm in items:
            groups[cell_value(fm.get(field)) or MISSING].append((rel, fm))
        for key in sorted(groups):
            lines.extend(["", f"{'#' * (2 + depth)} {field}: {key}"])
            emit(groups[key], depth + 1)

    emit(pages, 0)
    return "\n".join(lines) + "\n"


GENERATED = kb.GENERATED_MARKER


def build(root, cfg) -> str:
    vault = kb.Vault(root, cfg)
    pages, left_out = collect(vault)
    return render(vault, pages, left_out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build a computed note index from frontmatter (read-only unless --write).")
    ap.add_argument("--version", action="version", version=f"kit {kb.KIT_VERSION}")
    ap.add_argument("--root", default=".", help="vault folder (default: current directory)")
    ap.add_argument("--config", default=None, help=f"config file (default: <root>/{kb.CONFIG_NAME})")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="write the index_output file (the only file written)")
    mode.add_argument("--check", action="store_true", help="exit 1 if the index_output file is missing or stale")
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
    for note in notes + kb.default_changed_notes(cfg, "kb_index"):
        print(f"note: {note}", file=sys.stderr)
    text = build(root, cfg)
    out = root.resolve() / cfg["index_output"]
    if args.write:
        if out.exists() and GENERATED not in kb.read_text(out):
            print(f"refusing to overwrite {cfg['index_output']}: it was not generated by kb_index.py", file=sys.stderr)
            return 2
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print(f"wrote {cfg['index_output']}")
        return 0
    if args.check:
        current = kb.read_text(out) if out.is_file() else None
        if current != text:
            print(f"{cfg['index_output']} is {'missing' if current is None else 'stale'}; run with --write")
            return 1
        print(f"{cfg['index_output']} is up to date")
        return 0
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
