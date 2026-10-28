#!/usr/bin/env python3
"""kb_backup: back up only what matters in a vault, and verify that nothing but frontmatter changed since.

Create:  python -I kb_backup.py --root VAULT --dest FOLDER_OR_FILE.zip [--config FILE]
         Writes one ZIP with every note (*.md, same folder structure, notes under exempt_dirs left out),
         the whole `_system` folder and kb.config.json, plus a manifest with a SHA-256 for each file.
         The destination must be OUTSIDE the vault (for a synced vault: outside the synced folder).
         An existing archive is never overwritten. Files that exist only in the cloud are listed, not downloaded.

Verify:  python -I kb_backup.py --root VAULT --verify ARCHIVE.zip [--allow-missing] [--json] [--limit N]
         Checks the archive itself, then every note in it against the vault as it is now:
         the note must be identical, or differ only in its frontmatter (same body bytes and same BOM).
         Anything else is reported as BODY DIFFERS.

Exit code: 0 = ok, 1 = verification failed, 2 = bad usage or config. Standard library only, no network.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_common as kb  # noqa: E402

MANIFEST = "_kb_backup_manifest.json"
CONFIG_ENTRY = kb.CONFIG_NAME


def collect(vault: kb.Vault, config_path: Path):
    """Return (entries, skipped): entries are (archive name, source path); skipped are (path, reason)."""
    entries, skipped = [], []
    cloud = set(vault.cloud_only)
    for rel in vault.files:
        in_system = rel.startswith("_system/") and "__pycache__" not in rel.split("/")
        is_note = rel.lower().endswith(".md") and not vault.in_exempt_dir(rel)
        if not (in_system or is_note):
            continue
        if rel in cloud:
            skipped.append((rel, "cloud-only file (not downloaded)"))
            continue
        entries.append((rel, vault.root / rel))
    if config_path.is_file():
        entries.append((CONFIG_ENTRY, config_path))
    entries = sorted(set(entries), key=lambda e: e[0])
    return entries, skipped


def create(root: Path, cfg, config_path: Path, dest: str) -> int:
    vault = kb.Vault(root, cfg)
    target = Path(dest)
    if target.suffix.lower() != ".zip":
        target = target / ("kb-backup-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + ".zip")
    resolved = target.resolve()
    if resolved == vault.root or vault.root in resolved.parents:
        print("refusing: the destination is inside the vault; use a folder outside it "
              "(and outside the synced folder, if the vault is synced)", file=sys.stderr)
        return 2
    if target.exists():
        print(f"refusing to overwrite {target}", file=sys.stderr)
        return 2
    target.parent.mkdir(parents=True, exist_ok=True)
    entries, skipped = collect(vault, config_path)
    tmp = target.with_name(target.name + ".part")
    manifest = {"created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                "tool": "kb_backup", "files": [], "skipped": [{"path": p, "reason": r} for p, r in skipped]}
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, src in entries:
            data = src.read_bytes()
            mtime = datetime.datetime.fromtimestamp(src.stat().st_mtime)
            info = zipfile.ZipInfo(name, date_time=max(mtime.timetuple()[:6], (1980, 1, 1, 0, 0, 0)))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
            manifest["files"].append({"path": name, "size": len(data), "sha256": kb.sha256(data)})
        zf.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
    problems = check_archive(tmp)
    if problems:
        tmp.unlink(missing_ok=True)
        print("the new archive failed its own check, nothing kept: " + "; ".join(problems), file=sys.stderr)
        return 2
    tmp.rename(target)
    notes = sum(1 for n, _ in entries if n.lower().endswith(".md") and not n.startswith("_system/"))
    print(f"wrote {target}")
    print(f"{len(entries)} files ({notes} notes, {sum(1 for n, _ in entries if n.startswith('_system/'))} in _system, "
          f"{'config included' if config_path.is_file() else 'no config file'}), "
          f"{target.stat().st_size} bytes, archive sha256 {kb.sha256(target.read_bytes())}")
    if skipped:
        print(f"skipped {len(skipped)} cloud-only files (not downloaded): the archive is incomplete for them")
    return 0


def check_archive(path: Path):
    """Integrity of the archive itself: zip CRCs and the manifest hashes. Returns a list of problems."""
    problems = []
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad:
                problems.append(f"corrupt entry: {bad}")
            if MANIFEST in zf.namelist():
                for item in json.loads(zf.read(MANIFEST).decode("utf-8"))["files"]:
                    if item["path"] not in zf.namelist():
                        problems.append(f"missing from archive: {item['path']}")
                    elif kb.sha256(zf.read(item["path"])) != item["sha256"]:
                        problems.append(f"hash mismatch inside archive: {item['path']}")
            else:
                problems.append("manifest missing")
    except (OSError, zipfile.BadZipFile, ValueError, KeyError) as exc:
        problems.append(f"cannot read archive: {exc}")
    return problems


def verify(root: Path, cfg, archive: Path, allow_missing: bool, as_json: bool, limit: int) -> int:
    vault = kb.Vault(root, cfg)
    result = {"archive_problems": check_archive(archive), "identical": [], "frontmatter_only": [], "body_differs": [],
              "missing": [], "not_checked_cloud_only": [], "other_files_changed": [], "new_since_backup": []}
    if not result["archive_problems"]:
        with zipfile.ZipFile(archive) as zf:
            archived = [n for n in zf.namelist() if n != MANIFEST]
            for name in archived:
                if name == CONFIG_ENTRY:
                    continue
                current = vault.root / name
                if name in vault.cloud_only:
                    result["not_checked_cloud_only"].append(name)
                    continue
                if not current.is_file():
                    result["missing"].append(name)
                    continue
                now, then = current.read_bytes(), zf.read(name)
                is_note = name.lower().endswith(".md") and not name.startswith("_system/")
                if now == then:
                    result["identical"].append(name)
                elif not is_note:
                    result["other_files_changed"].append(name)
                else:
                    b1, _, body1 = kb.split_bytes(then)
                    b2, _, body2 = kb.split_bytes(now)
                    (result["frontmatter_only"] if (b1 == b2 and body1 == body2) else result["body_differs"]).append(name)
            known = set(archived)
            result["new_since_backup"] = [n for n in vault.notes if n not in known and not vault.in_exempt_dir(n)]
    failed = bool(result["archive_problems"] or result["body_differs"] or (result["missing"] and not allow_missing))
    result["ok"] = not failed
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for problem in result["archive_problems"]:
            print(f"ARCHIVE PROBLEM: {problem}")
        for label, key in (("BODY DIFFERS", "body_differs"), ("missing now", "missing"), ("not checked (cloud-only)", "not_checked_cloud_only")):
            for name in result[key][:limit]:
                print(f"{label}: {name}")
            if len(result[key]) > limit:
                print(f"{label}: ... {len(result[key]) - limit} more")
        counts = {k: len(v) for k, v in result.items() if isinstance(v, list)}
        print("kb_backup verify: " + ", ".join(f"{counts[k]} {k.replace('_', ' ')}" for k in
              ("identical", "frontmatter_only", "body_differs", "missing", "not_checked_cloud_only", "new_since_backup")) +
              ("  -> OK" if result["ok"] else "  -> FAILED"))
    return 0 if result["ok"] else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Back up the notes of a vault and verify them later.")
    ap.add_argument("--version", action="version", version=f"kit {kb.KIT_VERSION}")
    ap.add_argument("--root", default=".", help="vault folder (default: current directory)")
    ap.add_argument("--config", default=None, help=f"config file (default: <root>/{kb.CONFIG_NAME})")
    what = ap.add_mutually_exclusive_group(required=True)
    what.add_argument("--dest", help="create an archive in this folder (or at this .zip path), outside the vault")
    what.add_argument("--verify", metavar="ARCHIVE", help="verify the vault against this archive")
    ap.add_argument("--allow-missing", action="store_true", help="with --verify: notes that no longer exist do not fail the check")
    ap.add_argument("--json", action="store_true", help="with --verify: machine-readable result")
    ap.add_argument("--limit", type=int, default=20, help="with --verify: max names listed per problem")
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
    config_path = Path(args.config) if args.config else root / kb.CONFIG_NAME
    if args.dest:
        return create(root, cfg, config_path, args.dest)
    archive = Path(args.verify)
    if not archive.is_file():
        print(f"not a file: {archive}", file=sys.stderr)
        return 2
    return verify(root, cfg, archive, args.allow_missing, args.json, args.limit)


if __name__ == "__main__":
    sys.exit(main())
