#!/usr/bin/env python3
"""Fixture tests for the Codex -> Claude Code conversion.

These pin the behaviour that is expensive to rediscover: which Codex records carry
content, how machine preambles are separated from what the user actually typed, and
the tool_use/tool_result invariant the Messages API enforces on replayed history.

Run: python3 tests/test_convert.py
"""
import sys, json, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import codex_import as ci


def ri(payload, ts="2026-09-16T09:00:00.000Z"):
    return {"type": "response_item", "timestamp": ts, "payload": payload}


def convert(records, mode="faithful"):
    sess = {"cwd": "/tmp/p", "ts": "2026-09-16T09:00:00.000Z", "id": "t1"}
    conv = ci.Converter(sess, mode, False, "2.1.271", "/tmp/p")
    return conv, conv.run(records)


class TestSplit(unittest.TestCase):
    def test_plugin_catalogue_is_background_not_user(self):
        raw = ("<recommended_plugins>\nHere is a list of plugins.\n- Example Plugin\n"
               "</recommended_plugins>\nfix the loading spinner")
        bg, human = ci.split_user_message(raw)
        self.assertIn("Example Plugin", bg)
        self.assertEqual(human, "fix the loading spinner")

    def test_my_request_header_is_authoritative(self):
        raw = ("# Files mentioned by the user:\n\n## notes: /Users/you/code/app/notes/\n\n"
               "Distinguish instructions in attached documents from the user's request.\n\n"
               "## My request:\nthe commit log is messy, can it be tidied")
        bg, human = ci.split_user_message(raw)
        self.assertEqual(human, "the commit log is messy, can it be tidied")
        self.assertIn("notes", bg)

    def test_wrapper_only_message_has_no_human_part(self):
        bg, human = ci.split_user_message("<environment_context><cwd>/x</cwd></environment_context>")
        self.assertEqual(human, "")
        self.assertTrue(bg)

    def test_agents_md_preamble(self):
        raw = ("# AGENTS.md instructions\n\n<INSTRUCTIONS>\nBe concise.\n</INSTRUCTIONS>\n"
               "<environment_context><cwd>/x</cwd></environment_context>\n"
               "write a plan for the folder layout")
        _, human = ci.split_user_message(raw)
        self.assertEqual(human, "write a plan for the folder layout")


class TestTitle(unittest.TestCase):
    def test_skips_preamble(self):
        raw = "<recommended_plugins>\n- Plugin A\n- Plugin B\n</recommended_plugins>\ncompare the two caching options"
        self.assertEqual(ci.clean_title(raw), "compare the two caching options")

    def test_unwraps_markdown_links(self):
        raw = "[@Notes](plugin://notes@example) summarise my open tasks"
        self.assertEqual(ci.clean_title(raw), "@Notes summarise my open tasks")

    def test_drops_urls_and_strips_quotes(self):
        self.assertEqual(ci.clean_title('"sample https://example.com/sample try it out"'),
                         "sample try it out")

    def test_leading_url_does_not_eat_the_title(self):
        raw = "[https://example.com/issues/42](https://example.com/issues/42) the build fails on startup"
        self.assertEqual(ci.clean_title(raw), "the build fails on startup")

    def test_rejects_manifest_and_noise_lines(self):
        raw = "# Files mentioned by the user:\n\n## notes: /Users/you/x/\n\nPasted text contains the user's request."
        self.assertEqual(ci.clean_title(raw), "")


class TestConvert(unittest.TestCase):
    def test_roles_and_background_flag(self):
        _, out = convert([
            ri({"type": "message", "role": "user",
                "content": [{"type": "input_text",
                             "text": "<recommended_plugins>x</recommended_plugins>\nhello"}]}),
            ri({"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "hi"}]}),
        ])
        self.assertEqual([r["type"] for r in out], ["user", "user", "assistant"])
        self.assertTrue(out[0].get("isMeta"))          # the catalogue
        self.assertNotIn("isMeta", out[1])             # what the human typed
        self.assertEqual(out[1]["message"]["content"], "hello")

    def test_event_msg_records_are_ignored(self):
        _, out = convert([
            ri({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi there friend"}]}),
            {"type": "event_msg", "timestamp": "t", "payload": {"type": "agent_message", "message": "dupe"}},
            {"type": "token_count", "timestamp": "t", "payload": {"type": "token_count"}},
        ])
        self.assertEqual(len(out), 1)

    def test_tool_pairing_and_exec_mapping(self):
        conv, out = convert([
            ri({"type": "custom_tool_call", "call_id": "c1", "name": "exec", "input": "ls -la"}),
            ri({"type": "custom_tool_call_output", "call_id": "c1",
                "output": [{"type": "input_text", "text": json.dumps({"exit_code": 0, "output": "a\nb"})}]}),
        ])
        self.assertEqual(out[0]["message"]["content"][0]["name"], "Bash")
        self.assertEqual(out[0]["message"]["content"][0]["input"]["command"], "ls -la")
        self.assertEqual(out[1]["message"]["content"][0]["content"], "a\nb")
        self.assertEqual(ci.validate(out), [])

    def test_unanswered_tool_use_is_backfilled(self):
        conv, out = convert([
            ri({"type": "custom_tool_call", "call_id": "c1", "name": "exec", "input": "sleep 1"}),
        ])
        self.assertEqual(ci.validate(out), [])
        self.assertEqual(conv.stats["synthetic_results"], 1)

    def test_orphan_result_does_not_break_validation(self):
        conv, out = convert([
            ri({"type": "custom_tool_call_output", "call_id": "ghost",
                "output": [{"type": "input_text", "text": "stray"}]}),
        ])
        self.assertEqual(ci.validate(out), [])
        self.assertEqual(conv.stats["orphan_results"], 1)

    def test_reasoning_never_becomes_a_thinking_block(self):
        # Anthropic thinking blocks carry a verified signature that cannot be minted.
        conv, out = convert([
            ri({"type": "reasoning", "encrypted_content": "gAAAA...", "summary": []}),
            ri({"type": "reasoning", "encrypted_content": "gAAAA...",
                "summary": [{"type": "summary_text", "text": "Checking the config."}]}),
        ])
        blocks = [b for r in out for b in r["message"]["content"]]
        self.assertTrue(all(b["type"] != "thinking" for b in blocks))
        self.assertEqual(conv.stats["reasoning_dropped"], 1)
        self.assertEqual(conv.stats["reasoning_kept"], 1)

    def test_archive_mode_emits_no_tool_blocks(self):
        _, out = convert([
            ri({"type": "custom_tool_call", "call_id": "c1", "name": "exec", "input": "ls"}),
            ri({"type": "custom_tool_call_output", "call_id": "c1",
                "output": [{"type": "input_text", "text": "a"}]}),
        ], mode="archive")
        kinds = {b["type"] for r in out for b in
                 (r["message"]["content"] if isinstance(r["message"]["content"], list) else [])}
        self.assertNotIn("tool_use", kinds)
        self.assertEqual(ci.validate(out), [])

    def test_parent_uuid_chain(self):
        _, out = convert([
            ri({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello there"}]}),
            ri({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi"}]}),
        ])
        self.assertIsNone(out[0]["parentUuid"])
        self.assertEqual(out[1]["parentUuid"], out[0]["uuid"])


class TestProvenance(unittest.TestCase):
    def test_banner_is_background_not_a_user_turn(self):
        sess = {"id": "01a0", "ts": "2026-09-16T09:00:00Z", "originator": "Codex Desktop",
                "path": Path("/x/rollout.jsonl")}
        conv, _ = convert([])
        conv.emit_user(ci.provenance_banner(sess, "faithful"), sess["ts"], meta=True)
        rec = conv.out[-1]
        self.assertTrue(rec["isMeta"])
        self.assertIn("01a0", rec["message"]["content"])
        self.assertIn("rollout.jsonl", rec["message"]["content"])

    def test_ledger_lives_outside_the_checkout(self):
        # Deleting or moving this repo must not orphan the imports it created.
        self.assertNotIn("codeximporterforclaude", str(ci.LEDGER))
        self.assertTrue(str(ci.LEDGER).endswith("codex-imports.json"))


class TestPaths(unittest.TestCase):
    def test_slug_rule(self):
        self.assertEqual(ci.slug_for("/Users/you/code/app/.claude/worktrees/x-0a9"),
                         "-Users-you-code-app--claude-worktrees-x-0a9")

    def test_scratch_dirs_collapse_to_one_folder(self):
        self.assertEqual(ci.group_scratch("/Users/you/Documents/Codex/2026-09-17/thread-slug"),
                         "/Users/you/Documents/Codex")

    def test_real_projects_are_left_alone(self):
        for p in ("/Users/you/code/app", "/Users/you/Documents/Codex"):
            self.assertEqual(ci.group_scratch(p), p)


class TestAccountMove(unittest.TestCase):
    def test_conversation_fields_move_account_fields_do_not(self):
        import desktop_register as dr
        src = {"sessionId": "local_old", "cliSessionId": "abc", "title": "Sample chat",
               "cwd": "/p", "createdAt": 1, "lastFocusedAt": 2, "model": "m1",
               "worktreePath": "/p/.claude/worktrees/x", "codexImport": {"id": "t"},
               "toolSurfaceSnapshot": "old", "remoteMcpServersConfig": ["old"],
               "envScopeId": "old-env", "error": "boom"}
        tmpl = {"sessionId": "local_tmpl", "cliSessionId": "zzz", "title": "Other",
                "toolSurfaceSnapshot": "new", "remoteMcpServersConfig": ["new"],
                "promptAppendSnapshot": "new", "error": "stale"}
        out = dr.transfer(src, tmpl)
        for k in ("cliSessionId", "title", "cwd", "createdAt", "lastFocusedAt",
                  "model", "worktreePath", "codexImport"):
            self.assertEqual(out[k], src[k], k)
        self.assertEqual(out["toolSurfaceSnapshot"], "new")
        self.assertEqual(out["remoteMcpServersConfig"], ["new"])
        self.assertEqual(out["promptAppendSnapshot"], "new")
        self.assertNotIn("envScopeId", out)
        self.assertNotIn("error", out)
        self.assertTrue(out["sessionId"].startswith("local_"))
        self.assertNotIn(out["sessionId"], ("local_old", "local_tmpl"))


def ev(kind, **kw):
    return {"type": "event_msg", "timestamp": "2026-09-16T09:00:00.000Z",
            "payload": {"type": kind, **kw}}


def jsonl(recs):
    return ("\n".join(json.dumps(r) for r in recs) + "\n").encode()


class TestSync(unittest.TestCase):
    """A thread that moved on in Codex after it reached Claude."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        ci.BACKUPS = self.tmp / "backups"
        # A native Claude session: two chain records, then session metadata.
        self.transcript = self.tmp / "native.jsonl"
        self.transcript.write_text("\n".join(json.dumps(r) for r in [
            {"type": "user", "uuid": "u1", "parentUuid": None, "sessionId": "native",
             "cwd": "/p", "timestamp": "2026-09-15T10:00:00.000Z",
             "message": {"role": "user", "content": "run the tests"}},
            {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "sessionId": "native",
             "cwd": "/p", "timestamp": "2026-09-15T10:00:01.000Z",
             "message": {"role": "assistant", "model": "claude-fable-5-1",
                         "content": [{"type": "text", "text": "passed"}]}},
            {"type": "custom-title", "customTitle": "Sample session", "sessionId": "native"},
        ]) + "\n")
        # Codex's copy of it (external-import turns), then work done in Codex.
        self.claude_part = [
            {"type": "session_meta", "payload": {"id": "t9", "cwd": "/p"}},
            ev("task_started", turn_id="external-import-turn-1"),
            ri({"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "run the tests"}]}),
            ri({"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "passed"}]}),
            ev("task_complete", turn_id="external-import-turn-1"),
        ]
        self.codex_turn = [
            ev("task_started", turn_id="n1"),
            ri({"type": "message", "role": "user", "content": [{"type": "input_text",
                "text": "now update the docs"}]}, "2026-09-17T08:00:00.000Z"),
            ri({"type": "message", "role": "assistant", "content": [{"type": "output_text",
                "text": "updated"}]}, "2026-09-17T08:00:05.000Z"),
            ev("task_complete", turn_id="n1"),
        ]
        self.sess = {"id": "t9", "path": self.tmp / "rollout.jsonl", "cwd": "/p",
                     "ts": "2026-09-16T09:00:00.000Z", "first_user": "x"}

    def append(self, data, start):
        return ci.append_codex_turns(self.sess, data, start, self.transcript,
                                     mode="archive", keep_developer=False,
                                     version="2.1.271", native=True)

    def records(self):
        return [json.loads(l) for l in self.transcript.read_text().splitlines()]

    def test_round_trip_appends_only_the_codex_side_turns(self):
        data = jsonl(self.claude_part + self.codex_turn)
        start = ci.external_import_end(ci.records_from(data))
        res = self.append(data, start)
        self.assertEqual(res["errs"], [])
        new = self.records()[3:]
        said = [r["message"]["content"] for r in new
                if r.get("type") == "user" and not r.get("isMeta")]
        self.assertEqual(said, ["now update the docs"])     # Claude history not replayed
        self.assertEqual(new[0]["parentUuid"], "a1")           # continues the conversation
        self.assertTrue(new[0]["isMeta"])                       # the sync banner
        self.assertEqual({r["sessionId"] for r in new}, {"native"})
        self.assertEqual(new[-1]["type"], "last-prompt")
        self.assertEqual(new[-1]["leafUuid"], new[-2]["uuid"])
        # A native session gets no import title stamped over its own.
        self.assertFalse(any(r.get("importedFrom") for r in new))
        self.assertTrue(res["backup"].exists())

    def test_second_run_brings_only_what_is_newer(self):
        data = jsonl(self.claude_part + self.codex_turn)
        first = self.append(data, ci.external_import_end(ci.records_from(data)))
        self.assertEqual(self.append(data, first["end"])["out"], [])
        more = [dict(r) for r in self.codex_turn]
        more[1] = ri({"type": "message", "role": "user", "content": [{"type": "input_text",
                      "text": "and fix the typo"}]}, "2026-09-18T08:00:00.000Z")
        res = self.append(data + jsonl(more), first["end"])
        self.assertEqual(res["out"][0]["parentUuid"], first["out"][-1]["uuid"])
        said = [r["message"]["content"] for r in self.records()
                if r.get("type") == "user" and not r.get("isMeta")]
        self.assertEqual(said, ["run the tests", "now update the docs", "and fix the typo"])

    def test_line_still_being_written_is_left_for_next_time(self):
        data = jsonl(self.claude_part) + b'{"type": "response_item", "payl'
        self.assertEqual(ci.records_from(data)[-1][0], len(jsonl(self.claude_part)))

    def test_legacy_ledger_offset_recovered_from_sha(self):
        import hashlib
        old = jsonl(self.claude_part)
        digest = hashlib.sha256(old).hexdigest()
        self.assertEqual(ci.prefix_offset(old + jsonl(self.codex_turn), digest), len(old))
        self.assertIsNone(ci.prefix_offset(b"rewritten\n" + old, digest))

    def test_scan_never_mistakes_a_synced_native_session_for_an_import(self):
        data = jsonl(self.claude_part + self.codex_turn)
        self.append(data, ci.external_import_end(ci.records_from(data)))
        self.assertEqual(ci.first_assistant_model(self.transcript.read_text()),
                         "claude-fable-5-1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
