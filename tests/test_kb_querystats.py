"""Unit tests for kb_querystats: a synthetic session fixture plus small builders for the edge cases."""
import hashlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))
import kb_common as kb  # noqa: E402
import kb_querystats  # noqa: E402

FIXTURE = HERE / "fixtures" / "sessions" / "sample-session.jsonl"
SENTINELS = ("SENTINEL-QUESTION-ONE", "SENTINEL-QUESTION-TWO", "SENTINEL-QUESTION-THREE", "SENTINEL-ASSISTANT-TEXT",
             "SENTINEL-FILE-CONTENT", "SENTINEL-GREP-PATTERN", "SENTINEL-PEER-MESSAGE", "SENTINEL-BASH-DESCRIPTION",
             "SENTINEL-INJECTED", "cat notes", "head -5")


def run(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = kb_querystats.main(list(args))
    return code, out.getvalue(), err.getvalue()


def stats_json(*args):
    code, out, _ = run(*args, "--json")
    return code, json.loads(out)


def write_session(lines):
    path = Path(tempfile.mkdtemp(prefix="kbq-")) / "session.jsonl"
    path.write_text("\n".join(l if isinstance(l, str) else json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    return path


def human(text="a question", **extra):
    return dict({"type": "user", "cwd": "C:/v", "origin": {"kind": "human"}, "message": {"role": "user", "content": text}}, **extra)


def tool_result():
    return {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "x"}]}}


def reply(rid, usage=(1, 2, 3, 4), tools=(), cwd="C:/v", **extra):
    fields = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
    message = {"id": "m" + rid, "role": "assistant", "content": [dict(type="tool_use", id=f"{rid}-{i}", name=n, input=inp)
                                                              for i, (n, inp) in enumerate(tools)] or [{"type": "text", "text": "ok"}]}
    if usage is not None:
        message["usage"] = dict(zip(fields, usage))
    return dict({"type": "assistant", "requestId": rid, "cwd": cwd, "message": message}, **extra)


class FixtureNumbersTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.code, cls.data = stats_json("--session", str(FIXTURE))
        cls.rows = {r["question"]: r for r in cls.data["questions"]}

    def test_three_questions_and_nothing_else_counts_as_a_question(self):
        self.assertEqual(sorted(self.rows), [1, 2, 3])        # tool results, peer messages and injected markup are not questions
        self.assertEqual(self.data["totals"]["questions"], 3)
        self.assertEqual(self.code, 0)

    def test_question_one_counts_distinct_files_calls_and_requests(self):
        q = self.rows[1]
        self.assertEqual((q["tool_calls"], q["unique_files"], q["searched_locations"], q["requests"]), (5, 3, 1, 3))
        self.assertEqual(q["tool_calls_by_name"], {"Glob": 1, "Grep": 1, "Read": 3})
        self.assertEqual(q["paths"]["files"], ["C:/vault/Notes/a.md", "C:/vault/Notes/b.md", "C:/vault/Notes/c.md"])  # a.md once, case-insensitive
        self.assertEqual(q["paths"]["searched_locations"], ["C:/vault/docs"])

    def test_tokens_are_summed_over_distinct_requests_not_over_lines(self):
        q = self.rows[1]
        self.assertEqual((q["input_tokens"], q["cache_read_tokens"], q["cache_write_tokens"], q["output_tokens"]), (25, 600, 20, 62))
        self.assertEqual((q["total_tokens"], q["new_tokens"], q["context_peak_tokens"]), (707, 107, 303))
        self.assertEqual(self.rows[2]["total_tokens"], 65)
        self.assertEqual(self.rows[3]["total_tokens"], 30)

    def test_totals_and_averages(self):
        t = self.data["totals"]
        self.assertEqual((t["total_tokens"], t["new_tokens"], t["requests"], t["tool_calls"]), (802, 132, 5, 6))
        self.assertEqual(t["session_unique_files"], 3)
        self.assertEqual(t["average_unique_files"], 1.0)
        self.assertEqual(t["average_total_tokens"], 267)

    def test_unreadable_lines_are_skipped_and_counted(self):
        self.assertEqual(self.data["stats"], {"lines_read": 14, "lines_skipped": 2, "usage_lines": 6})
        self.assertEqual(self.data["notes"], [])

    def test_shell_commands_are_not_counted_as_files_unless_asked(self):
        self.assertEqual(self.rows[2]["unique_files"], 0)
        _, data = stats_json("--session", str(FIXTURE), "--include-bash")
        q2 = {r["question"]: r for r in data["questions"]}[2]
        self.assertEqual((q2["unique_files"], q2["shell_calls"]), (1, 1))
        self.assertEqual(q2["paths"]["files"], ["C:/vault/notes/x.md"])


class PrivacyTests(unittest.TestCase):
    def test_no_message_text_command_or_file_content_is_ever_printed(self):
        for flags in ((), ("--json",), ("--no-paths",), ("--no-paths", "--json"), ("--include-bash",)):
            code, out, err = run("--session", str(FIXTURE), *flags)
            for sentinel in SENTINELS:
                self.assertNotIn(sentinel, out + err, (flags, sentinel))

    def test_no_paths_removes_every_path_and_keeps_every_count(self):
        code, out, _ = run("--session", str(FIXTURE), "--no-paths")
        for fragment in ("a.md", "b.md", "c.md", "C:/vault", "docs"):
            self.assertNotIn(fragment, out)
        code, data = stats_json("--session", str(FIXTURE), "--no-paths")
        self.assertNotIn("paths", data["questions"][0])
        self.assertEqual(data["questions"][0]["unique_files"], 3)
        self.assertNotIn("a.md", json.dumps(data))

    def test_the_transcript_is_never_modified(self):
        before = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
        run("--session", str(FIXTURE))
        run("--session", str(FIXTURE), "--json", "--include-bash")
        self.assertEqual(before, hashlib.sha256(FIXTURE.read_bytes()).hexdigest())


class TextOutputTests(unittest.TestCase):
    def test_table_and_paths_section(self):
        code, out, _ = run("--session", str(FIXTURE))
        self.assertIn("kb_querystats: sample-session.jsonl  (3 questions, 6 replies with usage, 2 lines skipped)", out)
        self.assertIn("cache read", out)
        self.assertRegex(out, r"\n\s+1\s+5\s+3\s+1\s+3\s+25\s+600\s+20\s+62\s+107\s+707\s+303\n")
        self.assertIn("Q1: 3 files\n  C:/vault/Notes/a.md", out)
        self.assertIn("averages per question: 1.0 files, 267 tokens (total); distinct files over the whole run: 3", out)


class QuestionDetectionTests(unittest.TestCase):
    def questions(self, lines):
        _, data = stats_json("--session", str(write_session(lines)))
        return data["totals"]["questions"]

    def test_what_is_and_is_not_a_question(self):
        self.assertEqual(self.questions([human("q1"), human("q2")]), 2)
        self.assertEqual(self.questions([tool_result(), human("a", isMeta=True), human("b", isCompactSummary=True),
                                         human("c", isSidechain=True), human("<local-command>x</local-command>", origin=None)]), 0)
        peer = human("from a peer")
        peer["origin"] = {"kind": "peer"}
        task = human("done")
        task["origin"] = "task-notification"
        self.assertEqual(self.questions([peer, task]), 0)

    def test_a_prompt_without_origin_is_a_question_unless_it_is_injected_markup(self):
        plain = human("a plain prompt")
        del plain["origin"]
        markup = human("<system-reminder>x</system-reminder>")
        del markup["origin"]
        self.assertEqual(self.questions([plain, markup]), 1)

    def test_list_content_with_text_or_an_image_counts_and_empty_does_not(self):
        with_text = human([{"type": "text", "text": "hello"}])
        with_image = human([{"type": "image", "source": {}}])
        blank = human("   ")
        empty_list = human([])
        self.assertEqual(self.questions([with_text, with_image, blank, empty_list]), 2)

    def test_string_origin_human_is_accepted(self):
        q = human("hi")
        q["origin"] = "human"
        self.assertEqual(self.questions([q]), 1)

    def test_work_triggered_by_other_sessions_is_added_to_the_question_before_it(self):
        peer = human("peer says go", isMeta=True)
        peer["origin"] = {"kind": "peer"}
        _, data = stats_json("--session", str(write_session([human("q"), reply("r1", (1, 1, 1, 1)), peer, reply("r2", (2, 2, 2, 2))])))
        self.assertEqual((data["totals"]["questions"], data["questions"][0]["requests"], data["questions"][0]["total_tokens"]), (1, 2, 12))


class RobustnessTests(unittest.TestCase):
    def test_no_usage_anywhere_says_fields_not_found_and_exits_1(self):
        code, data = stats_json("--session", str(write_session([human("q"), reply("r1", usage=None, tools=[("Read", {"file_path": "C:/v/a.md"})])])))
        self.assertEqual(code, 1)
        self.assertIn("fields not found: no usage (token counts) in the assistant messages", data["notes"])
        self.assertEqual(data["questions"][0]["unique_files"], 1)
        self.assertEqual(data["questions"][0]["total_tokens"], 0)
        code, out, _ = run("--session", str(write_session([human("q"), reply("r1", usage=None)])))
        self.assertIn("fields not found: no usage", out)

    def test_no_questions_says_fields_not_found(self):
        code, data = stats_json("--session", str(write_session([reply("r1")])))
        self.assertEqual(code, 1)
        self.assertTrue([n for n in data["notes"] if "no user questions" in n])
        self.assertEqual(data["totals"]["questions"], 0)
        self.assertEqual(data["questions"][0]["question"], 0)  # the work before any question is kept as "setup"

    def test_partial_usage_fields_count_as_zero(self):
        line = reply("r1")
        del line["message"]["usage"]["cache_read_input_tokens"]
        line["message"]["usage"]["output_tokens"] = "n/a"
        _, data = stats_json("--session", str(write_session([human("q"), line])))
        q = data["questions"][0]
        self.assertEqual((q["input_tokens"], q["cache_read_tokens"], q["cache_write_tokens"], q["output_tokens"]), (1, 0, 3, 0))

    def test_unknown_shapes_do_not_break_the_run(self):
        junk = [{"type": "something-new", "x": 1}, {"type": "assistant", "message": "not a dict"}, {"type": "assistant", "message": {"content": 5}},
                {"type": "user"}, {"type": "user", "message": {"content": None}}, {"type": "assistant", "message": {"content": [7, "x", {"type": "tool_use"}]}},
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read", "input": "oops"}]}}]
        code, data = stats_json("--session", str(write_session([human("q")] + junk + [reply("r1")])))
        self.assertEqual(code, 0)
        self.assertEqual(data["totals"]["questions"], 1)

    def test_repeated_request_lines_with_growing_output_tokens_keep_the_largest(self):
        a, b = reply("r1", (1, 1, 1, 5)), reply("r1", (1, 1, 1, 9))
        _, data = stats_json("--session", str(write_session([human("q"), a, b])))
        self.assertEqual((data["questions"][0]["requests"], data["questions"][0]["output_tokens"]), (1, 9))

    def test_requests_without_an_id_are_kept_apart_and_sidechain_work_is_counted(self):
        a, b = reply("x", (1, 1, 1, 1)), reply("y", (1, 1, 1, 1), isSidechain=True)
        for line in (a, b):
            del line["requestId"]
            del line["message"]["id"]
        _, data = stats_json("--session", str(write_session([human("q"), a, b])))
        self.assertEqual((data["questions"][0]["requests"], data["questions"][0]["sidechain_requests"]), (2, 1))

    def test_bom_blank_lines_and_a_very_long_line(self):
        path = write_session([human("q"), "", reply("r1", tools=[("Read", {"file_path": "C:/v/" + "x" * 5000 + ".md"})])])
        path.write_bytes(kb.UTF8_BOM + path.read_bytes())
        code, data = stats_json("--session", str(path))
        self.assertEqual((code, data["questions"][0]["unique_files"]), (0, 1))

    def test_an_empty_file_is_reported_not_crashed(self):
        path = Path(tempfile.mkdtemp(prefix="kbq-")) / "empty.jsonl"
        path.write_text("", encoding="utf-8")
        code, data = stats_json("--session", str(path))
        self.assertEqual(code, 1)
        self.assertEqual(len(data["notes"]), 2)

    def test_a_tool_use_repeated_in_two_lines_is_counted_once(self):
        a = reply("r1", tools=[("Read", {"file_path": "C:/v/a.md"})])
        _, data = stats_json("--session", str(write_session([human("q"), a, json.loads(json.dumps(a))])))
        self.assertEqual(data["questions"][0]["tool_calls"], 1)


class FileCountingTests(unittest.TestCase):
    def files(self, tools, cwd="C:/v", **flags):
        args = []
        for key, value in flags.items():
            args += [f"--{key.replace('_', '-')}"] + ([] if value is True else [value])
        _, data = stats_json("--session", str(write_session([human("q"), reply("r1", tools=tools, cwd=cwd)])), *args)
        return data["questions"][0]

    def test_relative_paths_use_the_working_directory_and_backslashes_and_case_are_normalized(self):
        q = self.files([("Read", {"file_path": "Notes/a.md"}), ("Read", {"file_path": "C:\\v\\NOTES\\A.md"}), ("Read", {"file_path": "./Notes/../Notes/a.md"})])
        self.assertEqual(q["unique_files"], 1)

    def test_read_with_offset_and_the_same_file_twice_is_one_file(self):
        q = self.files([("Read", {"file_path": "C:/v/a.md", "offset": 10, "limit": 5}), ("Read", {"file_path": "C:/v/a.md"})])
        self.assertEqual((q["tool_calls"], q["unique_files"]), (2, 1))

    def test_grep_and_glob_paths_split_into_files_and_searched_locations(self):
        q = self.files([("Grep", {"pattern": "needle", "path": "C:/v/a.md"}), ("Grep", {"pattern": "needle", "path": "C:/v/docs"}),
                        ("Grep", {"pattern": "needle"}), ("Glob", {"pattern": "**/*.md"}), ("Glob", {"pattern": "b.md"}),
                        ("Glob", {"pattern": "*.md", "path": "C:/v/docs"})])
        self.assertEqual(q["unique_files"], 2)               # a.md (Grep path) and b.md (literal Glob pattern)
        self.assertEqual(q["searched_locations"], 3)         # docs (twice), the working directory, **/*.md
        self.assertEqual(sorted(q["paths"]["files"]), ["C:/v/a.md", "C:/v/b.md"])

    def test_a_grep_pattern_is_never_taken_for_a_path(self):
        q = self.files([("Grep", {"pattern": "readme.md", "path": "C:/v/docs"})])
        self.assertEqual(q["unique_files"], 0)

    def test_only_under_counts_the_vault_and_reports_the_rest_apart(self):
        tools = [("Read", {"file_path": "C:/v/vault/a.md"}), ("Read", {"file_path": "C:/v/other/b.md"}), ("Read", {"file_path": "C:/v/VAULT/c.md"})]
        q = self.files(tools, only_under="C:/v/vault")
        self.assertEqual((q["unique_files"], q["files_outside_scope"]), (2, 1))
        self.assertEqual(q["paths"]["files_outside_scope"], ["C:/v/other/b.md"])

    def test_only_under_does_not_match_a_sibling_folder_with_the_same_prefix(self):
        q = self.files([("Read", {"file_path": "C:/v/vault-old/a.md"}), ("Read", {"file_path": "C:/v/vault/b.md"})], only_under="C:/v/vault")
        self.assertEqual((q["unique_files"], q["files_outside_scope"]), (1, 1))

    def test_notebook_read_and_non_path_inputs(self):
        q = self.files([("NotebookRead", {"notebook_path": "C:/v/n.ipynb"}), ("Read", {}), ("Read", {"file_path": 5})])
        self.assertEqual(q["unique_files"], 1)

    def test_shell_heuristic_reads_only_reading_commands_and_never_flags_or_variables(self):
        commands = ["cat notes/a.md notes/b.md", "head -n 5 notes/c.md | grep foo", "grep -rn needle docs/d.md",
                    "ls notes/e.md", "python script.py notes/f.md", "echo hi > notes/g.md", "cat $HOME/h.md", "rg pattern --glob *.md",
                    "Get-Content notes/i.md", "sed -n 1,5p notes/j.md"]
        q = self.files([("Bash", {"command": c}) for c in commands[:-2]] + [("PowerShell", {"command": commands[-2]}), ("Bash", {"command": commands[-1]})],
                       include_bash=True)
        self.assertEqual(sorted(Path(p).name for p in q["paths"]["files"]), ["a.md", "b.md", "c.md", "d.md", "i.md", "j.md"])
        self.assertEqual(q["shell_calls"], 10)

    def test_shell_files_that_were_also_read_are_not_double_counted(self):
        q = self.files([("Read", {"file_path": "C:/v/notes/a.md"}), ("Bash", {"command": "cat notes/a.md"})], include_bash=True)
        self.assertEqual(q["unique_files"], 1)

    def test_shell_command_text_is_never_part_of_the_output(self):
        marker = "SENTINEL-NEVER-PRINT"
        path = write_session([human("q"), reply("r1", tools=[("Bash", {"command": f"cat notes/{marker}.md && echo {marker}"})])])
        code, out, err = run("--session", str(path), "--include-bash", "--no-paths")
        self.assertNotIn(marker, out + err)


class SessionSelectionTests(unittest.TestCase):
    def test_latest_picks_the_newest_transcript_in_the_project_folder(self):
        folder = Path(tempfile.mkdtemp(prefix="kbq-"))
        old = folder / "old.jsonl"
        new = folder / "new.jsonl"
        old.write_text(json.dumps(human("q")) + "\n" + json.dumps(reply("r1", (1, 0, 0, 0))) + "\n", encoding="utf-8")
        new.write_text(json.dumps(human("q")) + "\n" + json.dumps(reply("r1", (7, 0, 0, 0))) + "\n", encoding="utf-8")
        os.utime(old, (time.time() - 1000, time.time() - 1000))
        _, data = stats_json("--latest", "--project-dir", str(folder))
        self.assertEqual((data["session_file"], data["totals"]["input_tokens"]), ("new.jsonl", 7))

    def test_selection_errors_exit_2(self):
        empty = Path(tempfile.mkdtemp(prefix="kbq-"))
        for args in (("--latest",), ("--latest", "--project-dir", str(empty)), ("--latest", "--project-dir", str(empty / "nope")),
                     ("--session", str(empty / "missing.jsonl")), ("--session", str(FIXTURE), "--last", "0")):
            code, out, err = run(*args)
            self.assertEqual(code, 2, args)
            self.assertTrue(err.strip())
        for args in ((), ("--session", str(FIXTURE), "--latest")):
            with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
                kb_querystats.main(list(args))

    def test_last_keeps_only_the_final_questions(self):
        _, data = stats_json("--session", str(FIXTURE), "--last", "2")
        self.assertEqual([r["question"] for r in data["questions"]], [2, 3])
        self.assertEqual(data["totals"]["questions"], 2)
        self.assertEqual(data["totals"]["total_tokens"], 95)

    def test_a_directory_is_not_a_session(self):
        self.assertEqual(run("--session", str(HERE))[0], 2)


def say(rid, text, tools=(), usage=(1, 2, 3, 4), **extra):
    row = reply(rid, usage=usage, tools=tools, **extra)
    row["message"]["content"] = [{"type": "text", "text": text}] + (row["message"]["content"] if tools else [])
    return row


def rd(name):
    return ("Read", {"file_path": "C:/v/" + name})


NUL = chr(10)
NUMBERED_Q = "Please answer:" + NUL + "1. first SENTINEL-NUM-ONE" + NUL + "2. second" + NUL + "3. third"


def split_session(extra=()):
    return write_session([
        human(NUMBERED_Q),
        reply("r1", tools=[rd("a.md")]), tool_result(),
        say("r2", "1. SENTINEL-ANSWER-ONE"),
        reply("r3", tools=[rd("b.md"), rd("a.md")]), tool_result(),
        say("r4", "**2.** done"),
        reply("r5", tools=[rd("c.md")]), tool_result(),
        say("r6", "Q3: done"),
        *extra,
    ])


class SplitNumberedTests(unittest.TestCase):
    def parts(self, session, *flags):
        code, data = stats_json("--session", str(session), "--split-numbered", *flags)
        return data, data["questions"][0]

    def test_a_reliable_message_is_split_into_one_part_per_item(self):
        data, q = self.parts(split_session())
        self.assertEqual(q["split"]["status"], "split")
        self.assertEqual([p["question"] for p in q["parts"]], ["1.1", "1.2", "1.3"])
        self.assertEqual([p["unique_files"] for p in q["parts"]], [1, 2, 1])
        self.assertEqual([p["requests"] for p in q["parts"]], [2, 2, 2])
        self.assertEqual(q["unique_files"], 3)  # the question keeps the distinct files of the whole message

    def test_parts_add_up_to_the_question_tokens_and_totals_count_questions_only(self):
        data, q = self.parts(split_session())
        for field in ("total_tokens", "new_tokens", "requests", "tool_calls"):
            self.assertEqual(sum(p[field] for p in q["parts"]), q[field], field)
        self.assertEqual(data["totals"]["total_tokens"], q["total_tokens"])
        self.assertEqual(data["totals"]["questions"], 1)
        self.assertEqual((data["totals"]["questions_split"], data["totals"]["questions_not_split"]), (1, 0))

    def test_work_after_the_last_answer_gets_its_own_row(self):
        extra = [reply("r7", tools=[rd("d.md")]), tool_result()]
        data, q = self.parts(split_session(extra))
        self.assertEqual(q["parts"][-1]["question"], "1 (after last answer)")
        self.assertEqual(len(q["parts"]), 4)
        self.assertEqual(sum(p["total_tokens"] for p in q["parts"]), q["total_tokens"])
        out = run("--session", str(split_session(extra)), "--split-numbered")[1]
        self.assertIn("1 (after last answer)", out)
        self.assertIn("1.2", out)

    def test_no_after_row_when_nothing_happens_after_the_last_answer(self):
        self.assertEqual(len(self.parts(split_session())[1]["parts"]), 3)

    def test_nothing_changes_without_the_flag(self):
        session = split_session()
        plain = stats_json("--session", str(session))[1]
        self.assertNotIn("split", plain["questions"][0])
        self.assertNotIn("parts", plain["questions"][0])
        self.assertNotIn("questions_split", plain["totals"])
        flagged = stats_json("--session", str(session), "--split-numbered")[1]
        for key in ("split", "parts"):
            flagged["questions"][0].pop(key)
        for key in ("questions_split", "questions_not_split"):
            flagged["totals"].pop(key)
        self.assertEqual(plain, flagged)
        self.assertNotIn("split parts", run("--session", str(session))[1])

    def test_several_answers_in_one_message_are_not_split(self):
        session = write_session([human(NUMBERED_Q), reply("r1", tools=[rd("a.md")]), tool_result(), say("r2", "1. a" + NUL + "2. b" + NUL + "3. c")])
        data, q = self.parts(session)
        self.assertEqual(q["split"], {"status": "not split", "reason": "several answers in one message", "items": 3})
        self.assertNotIn("parts", q)
        self.assertEqual(q["unique_files"], 1)
        self.assertIn("Q1: not split (several answers in one message); totals only", run("--session", str(session), "--split-numbered")[1])

    def test_answers_in_two_lines_of_one_request_count_as_one_message(self):
        session = write_session([human(NUMBERED_Q), say("r1", "1. a"), say("r1", "2. b"), say("r2", "3. c")])
        self.assertEqual(self.parts(session)[1]["split"]["reason"], "several answers in one message")

    def test_missing_markers_mean_no_split(self):
        session = write_session([human(NUMBERED_Q), reply("r1", tools=[rd("a.md")]), say("r2", "1. a"), say("r3", "2. b"), say("r4", "all done")])
        self.assertEqual(self.parts(session)[1]["split"]["reason"], "answer markers found for 2 of 3 items")

    def test_out_of_order_or_repeated_markers_mean_no_split(self):
        for texts in (["2. b", "1. a", "3. c"], ["1. a", "2. b", "3. c", "1. again"], ["1. a", "1. a again", "2. b", "3. c"]):
            session = write_session([human(NUMBERED_Q)] + [say(f"r{i}", t) for i, t in enumerate(texts)])
            self.assertEqual(self.parts(session)[1]["split"]["reason"], "answer markers out of order or repeated", texts)

    def test_a_message_without_numbered_items_or_with_one_item_is_left_alone(self):
        for text in ("just a question", "1. only one item", "- a" + NUL + "- b", "2. first" + NUL + "3. second"):
            session = write_session([human(text), say("r1", "1. a"), say("r2", "2. b")])
            self.assertNotIn("split", self.parts(session)[1], text)

    def test_the_marker_styles_the_user_and_the_agent_may_use(self):
        user = "Q1: a" + NUL + "Q2) b"
        session = write_session([human(user), say("r1", "## 1. a"), say("r2", "Question 2: b")])
        self.assertEqual(self.parts(session)[1]["split"]["status"], "split")
        hebrew = "\u05e9\u05d0\u05dc\u05d4 1: a" + NUL + "\u05e9\u05d0\u05dc\u05d4 2: b"
        session = write_session([human(hebrew), say("r1", "\u05e9\u05d0\u05dc\u05d4 1: x"), say("r2", "2) y")])
        self.assertEqual(self.parts(session)[1]["split"]["status"], "split")

    def test_only_the_question_that_has_items_is_split(self):
        session = write_session([human("plain"), say("r0", "ok"), human(NUMBERED_Q), say("r1", "1. a"), say("r2", "2. b"), say("r3", "3. c")])
        data = stats_json("--session", str(session), "--split-numbered")[1]
        self.assertEqual(["split" in q for q in data["questions"]], [False, True])

    def test_sidechain_messages_never_close_a_part(self):
        session = write_session([human(NUMBERED_Q), say("r1", "1. a", isSidechain=True), say("r2", "1. a"), say("r3", "2. b"), say("r4", "3. c")])
        self.assertEqual(self.parts(session)[1]["split"]["status"], "split")

    def test_neither_the_question_nor_the_answers_are_printed(self):
        session = split_session()
        for flags in ((), ("--no-paths",), ("--json",), ("--json", "--no-paths")):
            out = run("--session", str(session), "--split-numbered", *flags)[1]
            for word in ("SENTINEL-NUM-ONE", "SENTINEL-ANSWER-ONE", "first", "done"):
                self.assertNotIn(word, out, (flags, word))

    def test_no_paths_removes_part_paths_but_keeps_counts(self):
        data, q = self.parts(split_session(), "--no-paths")
        self.assertNotIn("paths", q)
        self.assertTrue(all("paths" not in p and p["unique_files"] >= 1 for p in q["parts"]))
        data, q = self.parts(split_session())
        self.assertEqual(len(q["parts"][1]["paths"]["files"]), 2)
        self.assertEqual(len(q["paths"]["files"]), 3)
        self.assertIn("1.2: 2 files", run("--session", str(split_session()), "--split-numbered")[1])

    def test_last_and_include_bash_still_work_with_the_flag(self):
        session = write_session([human("plain"), say("r0", "ok"), human(NUMBERED_Q), say("r1", "1. a"), say("r2", "2. b"), say("r3", "3. c")])
        data = stats_json("--session", str(session), "--split-numbered", "--last", "1", "--include-bash")[1]
        self.assertEqual((len(data["questions"]), data["questions"][0]["split"]["status"]), (1, "split"))


class ToolVersionTests(unittest.TestCase):
    def test_version_flag(self):
        out = io.StringIO()
        with self.assertRaises(SystemExit) as ctx, redirect_stdout(out):
            kb_querystats.main(["--version"])
        self.assertEqual((ctx.exception.code, out.getvalue().strip()), (0, f"kit {kb.KIT_VERSION}"))
        self.assertEqual(stats_json("--session", str(FIXTURE))[1]["kit_version"], kb.KIT_VERSION)


if __name__ == "__main__":
    unittest.main()
