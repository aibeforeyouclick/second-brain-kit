#!/usr/bin/env python3
"""kb_querystats: measure a query test from a Claude Code session transcript (read-only, standard library only).

Claude Code keeps every session as a JSONL file (one JSON object per line). For each question (a user turn) this tool
reports how many DISTINCT files the agent opened, how many tool calls it made, and how many tokens it used, taken from
the `usage` fields of the agent's replies. It replaces self-reported numbers in a before/after query test.

It never prints what was said or what a file contained: only counts and, unless you pass --no-paths, file paths.
The transcript format is not formally documented and may change: unknown lines are skipped and anything that cannot
be found is reported as "fields not found".

Usage:  python -I kb_querystats.py --session FILE.jsonl [options]
        python -I kb_querystats.py --latest --project-dir DIR [options]
Options: --only-under DIR   count a file as "opened" only if it is inside DIR (your vault); others are counted apart
         --last N           only the last N questions
         --include-bash     also count file paths read by cat/head/tail/sed/grep/rg/... in shell calls (a heuristic)
         --split-numbered   one message with numbered items (1. 2. 3.) answered one by one: report each item apart, only when
                            the agent's replies carry the same numbers in order, one answer per message; else totals only
         --no-paths         counts only, for a report that leaves your organization
         --json             machine-readable output
Exit code: 0 = measured, 1 = measured but some fields were not found, 2 = bad usage or unreadable file.

Tokens: input, cache read, cache write (cache creation) and output are summed over the distinct API requests of a question.
`total` is their sum; `new` leaves out the cache reads (which re-read the earlier conversation on every request);
`context peak` is the largest single request context. Cache reads dominate `total` in long sessions: compare before and
after with the same measure. Messages that arrive from other sessions or tools are not questions: their work is added to the
question before them, so run the query test in a fresh session that only receives your questions.
"""
from __future__ import annotations

import argparse
import json
import posixpath
import re
import shlex
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_common as kb  # noqa: E402

USAGE_FIELDS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
READ_TOOLS = {"Read": ("file_path", "path"), "NotebookRead": ("notebook_path", "file_path")}
SEARCH_TOOLS = {"Grep", "Glob"}
SHELL_TOOLS = {"Bash", "PowerShell"}
READ_COMMANDS = {"cat", "head", "tail", "less", "more", "sed", "awk", "grep", "egrep", "rg", "bat", "type", "wc", "nl",
                 "get-content", "gc", "select-string", "sls"}
GLOB_CHARS = set("*?[]{}")
ABSOLUTE = re.compile(r"^(?:[A-Za-z]:/|/)")


def load_lines(path: Path):
    """Yield (number, object) for every JSON object line; count the lines that are not JSON objects."""
    skipped = 0
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        for number, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if isinstance(obj, dict):
                yield number, obj
            else:
                skipped += 1
    yield -1, {"__skipped__": skipped}


def origin_kind(obj):
    origin = obj.get("origin")
    if isinstance(origin, dict):
        origin = origin.get("kind")
    return origin if isinstance(origin, str) else None


def is_question(obj) -> bool:
    """A real user prompt: not a tool result, not injected markup, not a message from another session or a summary."""
    if obj.get("type") != "user" or obj.get("isMeta") or obj.get("isCompactSummary") or obj.get("isSidechain"):
        return False
    kind = origin_kind(obj)
    if kind is not None and kind != "human":
        return False
    message = obj.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    texts = []
    if isinstance(content, str):
        texts = [content]
        has_content = bool(content.strip())
    elif isinstance(content, list):
        blocks = [b for b in content if isinstance(b, dict)]
        if not blocks or all(b.get("type") == "tool_result" for b in blocks):
            return False
        texts = [b.get("text", "") for b in blocks if b.get("type") == "text" and isinstance(b.get("text"), str)]
        has_content = any(t.strip() for t in texts) or any(b.get("type") == "image" for b in blocks)
    else:
        return False
    if kind is None and texts and texts[0].lstrip().startswith("<"):
        return False  # injected command or system markup without an origin
    return has_content


def normalize(raw: str, cwd) -> str:
    path = raw.strip().replace(chr(92), "/")
    if not ABSOLUTE.match(path) and isinstance(cwd, str) and cwd:
        path = cwd.replace(chr(92), "/").rstrip("/") + "/" + path
    return posixpath.normpath(path)


def looks_like_file(value: str) -> bool:
    last = value.replace(chr(92), "/").rstrip("/").rsplit("/", 1)[-1]
    return not (GLOB_CHARS & set(value)) and "." in last.strip(".") and not value.endswith(("/", chr(92)))


def shell_file_paths(command: str):
    """Heuristic: file-looking arguments of reading commands (cat, head, grep, ...). Never printed as a command."""
    found = []
    for segment in re.split(r"[|;&]+|\n", command):
        try:
            tokens = shlex.split(segment, posix=False)
        except ValueError:
            tokens = segment.split()
        if not tokens or tokens[0].lower().strip("()") not in READ_COMMANDS:
            continue
        for token in tokens[1:]:
            token = token.strip("\"'")
            if token.startswith("-") or "$" in token or "=" in token or not looks_like_file(token):
                continue
            if "/" in token or chr(92) in token or re.search(r"\.[A-Za-z0-9]{1,6}$", token):
                found.append(token)
    return found


NUMBERED = re.compile(r"^[\s>]*[#*_`\s]*(?:(?:Q|Question|\u05e9\u05d0\u05dc\u05d4)\s*)?(\d{1,2})\s*[.):\-\u2013]")
MAX_ITEMS = 30
ZERO = {f: 0 for f in USAGE_FIELDS}


def new_turn(index, timestamp):
    return {"question": index, "timestamp": timestamp, "tool_calls": Counter(), "files": {}, "searched": {}, "bash_files": {},
            "requests": {}, "sidechain_requests": 0}


def message_text(obj) -> str:
    """The text blocks of a message, only to look for list numbers; the text itself is never stored or printed."""
    message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
    return ""


def line_numbers(text: str):
    """Numbers that open a line: '3.', '3)', 'Q3:', '**3.' ..."""
    return [int(m.group(1)) for m in (NUMBERED.match(line) for line in text.splitlines()) if m]


def count_items(text: str) -> int:
    """How many numbered items (1, 2, 3, ... in order) the user's message holds; 0 when fewer than two."""
    expected = 1
    for number in line_numbers(text):
        if number == expected:
            expected += 1
    return expected - 1 if 2 <= expected - 1 <= MAX_ITEMS else 0


def record_tool(turn, name, tool_input, cwd, include_bash):
    turn["tool_calls"][name] += 1
    if not isinstance(tool_input, dict):
        return
    if name in READ_TOOLS:
        for key in READ_TOOLS[name]:
            if isinstance(tool_input.get(key), str):
                norm = normalize(tool_input[key], cwd)
                turn["files"].setdefault(norm.lower(), norm)
                break
    elif name in SEARCH_TOOLS:
        # One location per search: the path argument; a Glob without a path uses its pattern; a Grep without a path
        # searches the working directory. A literal file path counts as an opened file, anything else as a searched location.
        target = tool_input.get("path") if isinstance(tool_input.get("path"), str) and tool_input["path"].strip() else None
        if target is None and name == "Glob" and isinstance(tool_input.get("pattern"), str) and tool_input["pattern"].strip():
            target = tool_input["pattern"]
        if target is None and isinstance(cwd, str):
            target = cwd
        if target:
            norm = normalize(target, cwd)
            if looks_like_file(target):
                turn["files"].setdefault(norm.lower(), norm)
            else:
                turn["searched"].setdefault(norm.lower(), norm)
    elif name in SHELL_TOOLS and include_bash and isinstance(tool_input.get("command"), str):
        for raw in shell_file_paths(tool_input["command"]):
            norm = normalize(raw, cwd)
            turn["bash_files"].setdefault(norm.lower(), norm)


def start_split(turn, text):
    """Prepare a question that holds numbered items to be divided at the agent's answer markers."""
    items = count_items(text)
    if items:
        turn.update(items=items, part=new_turn(None, turn["timestamp"]), parts=[], rid_part={}, bad=None, last_close=None)


def part_of(turn, rid):
    """The part a request belongs to: the one that was open when its first line arrived."""
    return turn["rid_part"].setdefault(rid, turn["part"])


def has_work(t):
    return bool(t["requests"] or t["tool_calls"])


def end_turn(turn):
    """Decide whether a question could be split. Records only a status and a generic reason, never text."""
    if "items" not in turn:
        return
    items, parts, tail = turn["items"], turn.pop("parts"), turn.pop("part")
    bad = turn.pop("bad")
    for key in ("rid_part", "last_close", "items"):
        turn.pop(key, None)
    label = turn["question"]
    if bad is None and len(parts) < items:
        bad = f"answer markers found for {len(parts)} of {items} items"
    if bad is not None:
        turn["split"] = {"status": "not split", "reason": bad, "items": items}
        return
    for number, part in enumerate(parts, 1):
        part["question"] = f"{label}.{number}"
    if has_work(tail):
        tail["question"] = f"{label} (after last answer)"
        parts.append(tail)
    turn["split"] = {"status": "split", "items": items}
    turn["parts"] = parts


def close_part(turn, obj, number, rid):
    """The agent answered item `number` in this message: it ends the open part. Reliable only if answers come one by one, in order."""
    if turn["bad"] is not None:
        return
    if rid == turn["last_close"]:
        turn["bad"] = "several answers in one message"
    elif number != len(turn["parts"]) + 1:
        turn["bad"] = "answer markers out of order or repeated"
    else:
        turn["parts"].append(turn["part"])
        turn["part"] = new_turn(None, obj.get("timestamp"))
        turn["last_close"] = rid


def analyse(path: Path, include_bash: bool = False, split_numbered: bool = False):
    turns, notes = [], []
    pre = new_turn(0, None)
    current = pre
    seen_tool_ids, seen_requests = set(), {}
    stats = {"lines_read": 0, "lines_skipped": 0, "usage_lines": 0}
    for number, obj in load_lines(path):
        if "__skipped__" in obj:
            stats["lines_skipped"] += obj["__skipped__"]
            continue
        stats["lines_read"] += 1
        if is_question(obj):
            end_turn(current)
            current = new_turn(len(turns) + 1, obj.get("timestamp"))
            if split_numbered:
                start_split(current, message_text(obj))
            turns.append(current)
            continue
        if obj.get("type") != "assistant" or not isinstance(obj.get("message"), dict):
            continue
        message = obj["message"]
        usage = message.get("usage")
        rid = obj.get("requestId") or message.get("id") or f"line{number}"
        splitting = "items" in current
        if isinstance(usage, dict):
            stats["usage_lines"] += 1
            record = current["requests"].setdefault(rid, dict(ZERO))
            part_record = part_of(current, rid)["requests"].setdefault(rid, dict(ZERO)) if splitting else None
            if rid not in seen_requests:
                seen_requests[rid] = current
                if obj.get("isSidechain"):
                    current["sidechain_requests"] += 1
                    if splitting:
                        part_of(current, rid)["sidechain_requests"] += 1
            for field in USAGE_FIELDS:
                value = usage.get(field)
                if isinstance(value, (int, float)):
                    record[field] = max(record[field], int(value))
                    if part_record is not None:
                        part_record[field] = max(part_record[field], int(value))
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_id, name, tool_input = block.get("id"), block.get("name"), block.get("input")
            if tool_id in seen_tool_ids or not isinstance(name, str):
                continue
            seen_tool_ids.add(tool_id)
            cwd = obj.get("cwd")
            record_tool(current, name, tool_input, cwd, include_bash)
            if splitting:
                record_tool(current["part"], name, tool_input, cwd, include_bash)
        if splitting and not obj.get("isSidechain"):
            markers = sorted({n for n in line_numbers(message_text(obj)) if 1 <= n <= current["items"]})
            if len(markers) > 1 and current["bad"] is None:
                current["bad"] = "several answers in one message"
            elif markers:
                close_part(current, obj, markers[0], rid)
    end_turn(current)
    if not turns:
        notes.append("fields not found: no user questions (the transcript format may have changed)")
    if stats["usage_lines"] == 0:
        notes.append("fields not found: no usage (token counts) in the assistant messages")
    if pre["requests"] or pre["tool_calls"]:
        turns.insert(0, pre)
    return turns, notes, stats


def summarise(turn, only_under, include_bash):
    inside = [p for p in turn["files"].values() if not only_under or p.lower().startswith(only_under)]
    outside = [p for p in turn["files"].values() if only_under and not p.lower().startswith(only_under)]
    bash_inside = [p for p in turn["bash_files"].values() if (not only_under or p.lower().startswith(only_under)) and p.lower() not in {x.lower() for x in inside}]
    sums = {f: sum(r[f] for r in turn["requests"].values()) for f in USAGE_FIELDS}
    context = [r["input_tokens"] + r["cache_read_input_tokens"] + r["cache_creation_input_tokens"] for r in turn["requests"].values()]
    row = {
        "question": turn["question"], "timestamp": turn["timestamp"],
        "tool_calls": sum(turn["tool_calls"].values()), "tool_calls_by_name": dict(sorted(turn["tool_calls"].items())),
        "shell_calls": sum(n for name, n in turn["tool_calls"].items() if name in SHELL_TOOLS),
        "unique_files": len(inside) + (len(bash_inside) if include_bash else 0),
        "files_outside_scope": len(outside),
        "searched_locations": len(turn["searched"]),
        "requests": len(turn["requests"]), "sidechain_requests": turn["sidechain_requests"],
        "input_tokens": sums["input_tokens"], "cache_read_tokens": sums["cache_read_input_tokens"],
        "cache_write_tokens": sums["cache_creation_input_tokens"], "output_tokens": sums["output_tokens"],
        "total_tokens": sum(sums.values()),
        "new_tokens": sums["input_tokens"] + sums["cache_creation_input_tokens"] + sums["output_tokens"],
        "context_peak_tokens": max(context) if context else 0,
    }
    if include_bash:
        row["shell_files_found_in_commands"] = len(bash_inside)
    paths = {"files": sorted(inside + (bash_inside if include_bash else [])), "files_outside_scope": sorted(outside),
             "searched_locations": sorted(turn["searched"].values())}
    if "split" in turn:
        row["split"] = turn["split"]
        if turn.get("parts"):
            row["parts"] = []
            paths["parts"] = []
            for part in turn["parts"]:
                part_row, part_paths = summarise(part, only_under, include_bash)
                row["parts"].append(part_row)
                paths["parts"].append(part_paths)
    return row, paths


COLUMNS = [("Q", "question"), ("tools", "tool_calls"), ("files", "unique_files"), ("searched", "searched_locations"),
           ("requests", "requests"), ("input", "input_tokens"), ("cache read", "cache_read_tokens"),
           ("cache write", "cache_write_tokens"), ("output", "output_tokens"), ("new", "new_tokens"), ("total", "total_tokens"),
           ("ctx peak", "context_peak_tokens")]


def with_parts(rows):
    """Question rows with their parts directly below (totals are taken from the questions only)."""
    flat = []
    for row in rows:
        flat.append(row)
        flat.extend(row.get("parts", []))
    return flat


def render_table(rows, totals):
    def fmt(row, key):
        value = row.get(key)
        return f"{value:,}" if isinstance(value, int) and key != "question" else ("setup" if key == "question" and value == 0 else str(value))

    data = [[fmt(r, k) for _, k in COLUMNS] for r in with_parts(rows)] + [["all"] + [fmt(totals, k) for _, k in COLUMNS[1:]]]
    heads = [h for h, _ in COLUMNS]
    widths = [max(len(heads[i]), *(len(d[i]) for d in data)) for i in range(len(heads))]
    lines = ["  ".join(h.rjust(widths[i]) for i, h in enumerate(heads))]
    lines += ["  ".join(c.rjust(widths[i]) for i, c in enumerate(d)) for d in data]
    return lines


def newest_session(directory: Path):
    files = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Per-question files opened and tokens used, from a Claude Code session transcript.")
    ap.add_argument("--version", action="version", version=f"kit {kb.KIT_VERSION}")
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--session", help="the session transcript (.jsonl)")
    source.add_argument("--latest", action="store_true", help="the most recently modified transcript in --project-dir")
    ap.add_argument("--project-dir", help="with --latest: the folder that holds the .jsonl transcripts")
    ap.add_argument("--only-under", help="count files as opened only inside this folder (for example the vault)")
    ap.add_argument("--last", type=int, default=None, help="only the last N questions")
    ap.add_argument("--include-bash", action="store_true", help="also count file paths read by shell commands (heuristic)")
    ap.add_argument("--split-numbered", action="store_true",
                    help="when one message holds numbered items and the agent answers them one by one with its own numbers, "
                         "report each item apart (only when the split is reliable; the question total stays)")
    ap.add_argument("--no-paths", action="store_true", help="counts only: no file paths in the output")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.latest:
        if not args.project_dir or not Path(args.project_dir).is_dir():
            print("--latest needs --project-dir pointing to an existing folder", file=sys.stderr)
            return 2
        session = newest_session(Path(args.project_dir))
        if session is None:
            print(f"no .jsonl transcript in {args.project_dir}", file=sys.stderr)
            return 2
    else:
        session = Path(args.session)
        if not session.is_file():
            print(f"not a file: {session}", file=sys.stderr)
            return 2
    if args.last is not None and args.last < 1:
        print("--last must be at least 1", file=sys.stderr)
        return 2
    only_under = None
    if args.only_under:
        only_under = normalize(str(Path(args.only_under).resolve()), None).lower().rstrip("/") + "/"
    try:
        turns, notes, stats = analyse(session, args.include_bash, args.split_numbered)
    except OSError as exc:
        print(f"cannot read {session}: {exc}", file=sys.stderr)
        return 2
    if args.last is not None:
        real = [t for t in turns if t["question"] > 0]
        turns = real[-args.last:]
    summaries = [summarise(t, only_under, args.include_bash) for t in turns]
    rows = [s[0] for s in summaries]
    numeric = ["tool_calls", "unique_files", "files_outside_scope", "searched_locations", "requests", "sidechain_requests",
               "input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "total_tokens", "new_tokens"]
    totals = {k: sum(r[k] for r in rows) for k in numeric}
    totals["context_peak_tokens"] = max([r["context_peak_tokens"] for r in rows] or [0])
    totals["questions"] = sum(1 for r in rows if r["question"] > 0)
    totals["session_unique_files"] = len({key for t in turns for key in t["files"] if not only_under or key.startswith(only_under)})
    questions = [r for r in rows if r["question"] > 0]
    totals["average_unique_files"] = round(sum(r["unique_files"] for r in questions) / len(questions), 2) if questions else 0
    totals["average_total_tokens"] = round(sum(r["total_tokens"] for r in questions) / len(questions)) if questions else 0
    if args.split_numbered:
        totals["questions_split"] = sum(1 for r in rows if r.get("split", {}).get("status") == "split")
        totals["questions_not_split"] = sum(1 for r in rows if r.get("split", {}).get("status") == "not split")
    payload = {"kit_version": kb.KIT_VERSION, "session_file": session.name, "scope": only_under, "stats": stats, "notes": notes,
               "totals": totals, "questions": rows}
    if not args.no_paths:
        for row, (_, paths) in zip(rows, summaries):
            row["paths"] = {k: v for k, v in paths.items() if k != "parts"}
            for part_row, one in zip(row.get("parts", []), paths.get("parts", [])):
                part_row["paths"] = one
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"kb_querystats: {session.name}  ({totals['questions']} questions, {stats['usage_lines']} replies with usage, "
              f"{stats['lines_skipped']} lines skipped)")
        print()
        for line in render_table(rows, totals):
            print(line)
        print()
        print(f"averages per question: {totals['average_unique_files']} files, {totals['average_total_tokens']:,} tokens (total); "
              f"distinct files over the whole run: {totals['session_unique_files']} (the 'all' row adds up the per-question counts)")
        if args.split_numbered:
            print("split parts: files and tokens per numbered item; a file read for two items counts in both parts, "
                  "the question row keeps the whole question (the 'all' row adds up questions only)")
            for row in rows:
                info = row.get("split")
                if info and info["status"] == "not split":
                    print(f"Q{row['question']}: not split ({info['reason']}); totals only")
        if only_under:
            print(f"files opened outside the scope: {totals['files_outside_scope']}")
        if args.include_bash:
            print("shell file counts are a heuristic (cat/head/grep/... arguments)")
        for note in notes:
            print(note)
        if not args.no_paths:
            print()
            for row, (_, paths) in zip(rows, summaries):
                label = "setup" if row["question"] == 0 else f"Q{row['question']}"
                print(f"{label}: {len(paths['files'])} files")
                for item in paths["files"]:
                    print(f"  {item}")
                if paths["files_outside_scope"]:
                    print(f"  outside scope: {len(paths['files_outside_scope'])}")
                for part_row, part_paths in zip(row.get("parts", []), paths.get("parts", [])):
                    print(f"  {part_row['question']}: {len(part_paths['files'])} files")
                    for item in part_paths["files"]:
                        print(f"    {item}")
    return 1 if notes else 0


if __name__ == "__main__":
    sys.exit(main())
