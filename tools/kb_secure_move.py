#!/usr/bin/env python3
"""kb_secure_move: move a sensitive file out of a synced vault, safely and with a record (standard library only).

For each --file it: (1) copies it to a folder OUTSIDE the vault, (2) compares the SHA-256 of the copy with the original,
(3) only if they are identical sends the original to the trash (the system trash, never a permanent delete), and
(4) appends one row to a log: what, from where, to where, when, who approved. It never prints or logs the content.

It does nothing unless you pass --apply. Without it you get a dry run that shows exactly what would happen.

Usage:  python -I kb_secure_move.py --root VAULT --file PATH [--file PATH ...] --dest DIR [options]
        add  --apply --approved-by "NAME"   to really do it
Options: --log FILE          the record file (default: <dest>/SECURE-MOVE-LOG.md)
         --reason TEXT       why (one line, goes into the log; never put a secret in it)
         --trash-dir DIR     send the original to this folder instead of the system trash
         --allow-synced-dest allow a destination whose path looks like OneDrive/Dropbox/Google Drive/iCloud
         --json              machine-readable output
Exit code: 0 = all files moved (or a dry run that found no problem), 1 = a file could not be moved, 2 = bad usage.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_common as kb  # noqa: E402

LOG_NAME = "SECURE-MOVE-LOG.md"
SYNC_HINTS = ("onedrive", "dropbox", "google drive", "googledrive", "icloud", "box sync")
HEADER = ("| When | What (vault path) | From (vault) | To | SHA-256 | Approved by | Reason | Result |\n"
          "|---|---|---|---|---|---|---|---|\n")


class MoveError(Exception):
    pass


def now_text() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def looks_synced(path: Path) -> bool:
    return any(hint in part.lower() for part in path.resolve().parts for hint in SYNC_HINTS)


def unique_target(folder: Path, name: str) -> Path:
    target, n = folder / name, 1
    while target.exists():
        target = folder / f"{Path(name).stem}.{n}{Path(name).suffix}"
        n += 1
    return target


def _windows_recycle(path: Path) -> str:
    import ctypes
    from ctypes import wintypes

    class FileOp(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", ctypes.c_void_p), ("pTo", ctypes.c_void_p),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL), ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", wintypes.LPCWSTR)]

    buffer = ctypes.create_unicode_buffer(str(path.resolve()) + "\0")  # the list of names ends with two NUL characters
    op = FileOp()
    op.wFunc = 3  # FO_DELETE
    op.pFrom = ctypes.cast(buffer, ctypes.c_void_p).value
    op.fFlags = 0x40 | 0x10 | 0x4 | 0x400  # ALLOWUNDO (recycle bin) | NOCONFIRMATION | SILENT | NOERRORUI
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if result != 0 or op.fAnyOperationsAborted or path.exists():
        raise MoveError(f"the system trash refused the file (code {result})")
    return "the Recycle Bin"


def move_to_trash(path: Path, trash_dir: Path | None = None) -> str:
    """Send a file to the trash (a recoverable place). Returns where it went, for the log."""
    if trash_dir is not None:
        trash_dir.mkdir(parents=True, exist_ok=True)
        target = unique_target(trash_dir, path.name)
        shutil.move(str(path), str(target))
        return str(target)
    if os.name == "nt":
        return _windows_recycle(path)
    if sys.platform == "darwin":
        folder = Path.home() / ".Trash"
        folder.mkdir(exist_ok=True)
        target = unique_target(folder, path.name)
        shutil.move(str(path), str(target))
        return str(target)
    return _freedesktop_trash(path)


def _freedesktop_trash(path: Path) -> str:
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "Trash"
    (base / "files").mkdir(parents=True, exist_ok=True)
    (base / "info").mkdir(parents=True, exist_ok=True)
    target = unique_target(base / "files", path.name)
    (base / "info" / (target.name + ".trashinfo")).write_text(
        "[Trash Info]" + chr(10) + f"Path={path.resolve()}" + chr(10)
        + f"DeletionDate={datetime.datetime.now():%Y-%m-%dT%H:%M:%S}" + chr(10), encoding="utf-8")
    shutil.move(str(path), str(target))
    return str(target)


def plan_file(root: Path, rel: str, dest: Path) -> dict:
    """Check one file and describe what would be done. Raises MoveError with a plain reason."""
    given = rel.replace(chr(92), "/").strip()
    if not given.strip("/") or ".." in given.split("/"):
        raise MoveError("the path must be inside the vault")
    source = Path(given) if Path(given).is_absolute() else root / given.strip("/")
    if source.is_symlink():
        raise MoveError("a symbolic link: not followed")
    if not inside(source, root):
        raise MoveError("the path must be inside the vault")
    if not source.is_file():
        raise MoveError("not a file")
    clean = source.resolve().relative_to(root).as_posix()
    if kb.is_cloud_only(source):
        raise MoveError("the file is online-only (not downloaded): it is not opened")
    target = dest / clean
    if target.exists():
        raise MoveError(f"the destination already has a file at {clean}: nothing is overwritten")
    return {"file": clean, "source": source, "target": target, "size": source.stat().st_size}


def move_one(root: Path, rel: str, dest: Path, apply: bool, trash_dir, approved_by: str, reason: str) -> dict:
    result = {"file": rel, "status": "error", "to": None, "sha256": None, "trash": None, "error": None}
    try:
        plan = plan_file(root, rel, dest)
        result["file"], result["to"] = plan["file"], str(plan["target"])
        before = kb.sha256(plan["source"].read_bytes())
        result["sha256"] = before
        if not apply:
            result["status"] = "dry-run"
            return result
        plan["target"].parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(plan["source"], plan["target"])
        if kb.sha256(plan["target"].read_bytes()) != before:
            plan["target"].unlink()
            raise MoveError("the copy does not match the original (SHA-256 differs); the copy was removed and the original left alone")
        try:
            result["trash"] = move_to_trash(plan["source"], trash_dir)
        except (MoveError, OSError) as exc:
            result["status"] = "copied-not-removed"
            result["error"] = f"copied and verified, but the original could not be sent to the trash: {exc}"
            return result
        result["status"] = "moved"
    except (MoveError, OSError) as exc:
        result["error"] = str(exc)
    return result


def cell(text) -> str:
    return str(text or "").replace("|", "/").replace("\n", " ").strip()


def append_log(log: Path, root: Path, results, approved_by: str, reason: str):
    """One row per file, never the content. Raises OSError if the log cannot be written."""
    log.parent.mkdir(parents=True, exist_ok=True)
    new = not log.exists()
    when = now_text()
    rows = []
    for r in results:
        outcome = {"moved": "moved; original in the trash", "copied-not-removed": "COPIED ONLY: original still in the vault"}.get(r["status"])
        if outcome is None:
            continue
        rows.append("| " + " | ".join(cell(x) for x in (when, r["file"], str(root), r["to"], r["sha256"], approved_by, reason, outcome)) + " |\n")
    if not rows:
        return 0
    with open(log, "a", encoding="utf-8", newline="\n") as fh:
        if new:
            fh.write("# Secure move log\nOne row per sensitive file taken out of the vault by kb_secure_move.py. Paths and hashes only, never content.\n\n" + HEADER)
        fh.writelines(rows)
    return len(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Move a sensitive file out of a synced vault: copy, verify, trash the original, log (dry run by default).")
    ap.add_argument("--version", action="version", version=f"kit {kb.KIT_VERSION}")
    ap.add_argument("--root", default=".", help="vault folder (default: current directory)")
    ap.add_argument("--file", action="append", required=True, help="a file inside the vault (repeat for several)")
    ap.add_argument("--dest", required=True, help="a folder OUTSIDE the vault (and outside any synced folder) to copy to")
    ap.add_argument("--log", help=f"record file (default: <dest>/{LOG_NAME})")
    ap.add_argument("--apply", action="store_true", help="really copy and remove (without it: dry run)")
    ap.add_argument("--approved-by", default="", help="who approved this move (required with --apply)")
    ap.add_argument("--reason", default="", help="one line for the log; never a secret")
    ap.add_argument("--trash-dir", help="send the original to this folder instead of the system trash")
    ap.add_argument("--allow-synced-dest", action="store_true", help="allow a destination that looks like a synced folder")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    root, dest = Path(args.root), Path(args.dest)
    if not root.is_dir():
        print(f"not a folder: {root}", file=sys.stderr)
        return 2
    root, dest = root.resolve(), dest.resolve()
    if inside(dest, root) or inside(root, dest):
        print("--dest must be a folder outside the vault (and not a parent of it): a copy inside the vault is still synced", file=sys.stderr)
        return 2
    if looks_synced(dest) and not args.allow_synced_dest:
        print("--dest looks like a synced folder (OneDrive, Dropbox, Google Drive, iCloud): pick a local folder, or pass --allow-synced-dest if you are sure", file=sys.stderr)
        return 2
    if args.apply and not args.approved_by.strip():
        print("--apply needs --approved-by NAME: the log records who approved the move", file=sys.stderr)
        return 2
    log = Path(args.log) if args.log else dest / LOG_NAME
    trash_dir = Path(args.trash_dir) if args.trash_dir else None
    if trash_dir is not None and inside(trash_dir, root):
        print("--trash-dir must be outside the vault: a trash folder inside it is still synced", file=sys.stderr)
        return 2
    results = [move_one(root, rel, dest, args.apply, trash_dir, args.approved_by.strip(), args.reason) for rel in args.file]
    logged, log_error = 0, None
    if args.apply:
        try:
            logged = append_log(log, root, results, args.approved_by.strip(), args.reason)
        except OSError as exc:
            log_error = f"the files were handled but the log could not be written ({exc}); record these by hand: " + "; ".join(
                f"{r['file']} -> {r['to']} ({r['status']})" for r in results if r["status"] in ("moved", "copied-not-removed"))
    failed = [r for r in results if r["status"] in ("error", "copied-not-removed")] + ([{}] if log_error else [])
    if args.json:
        print(json.dumps({"kit_version": kb.KIT_VERSION, "applied": args.apply, "log": str(log) if args.apply else None,
                          "logged_rows": logged, "log_error": log_error, "files": results}, ensure_ascii=False, indent=2))
    else:
        mode = "APPLIED" if args.apply else "DRY RUN (nothing was changed; add --apply --approved-by NAME to do it)"
        print(f"kb_secure_move: {mode}")
        for r in results:
            line = {"dry-run": "would move", "moved": "moved", "copied-not-removed": "COPIED ONLY", "error": "NOT moved"}[r["status"]]
            print(f"  {line}: {r['file']}" + (f" -> {r['to']}" if r["to"] else "") + (f"  sha256 {r['sha256'][:16]}..." if r["sha256"] else ""))
            if r["error"]:
                print(f"    {r['error']}")
            if r["trash"]:
                print(f"    original sent to: {r['trash']}")
        if args.apply:
            print(f"log: {log} ({logged} rows added)")
        if log_error:
            print(log_error)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
