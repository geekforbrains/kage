"""Tests for the hook-driven progress/event machinery."""
import io
import json

from kage import hooks as H
from kage.backends.claude import ClaudeBackend


def test_build_settings_registers_expected_hooks(tmp_path):
    ev = tmp_path / "events.jsonl"
    s = H.build_settings(ev)
    assert set(s["hooks"]) == {"PreToolUse", "PostToolUse", "Stop"}
    # tool hooks carry a matcher; Stop does not
    assert s["hooks"]["PreToolUse"][0]["matcher"] == "*"
    assert "matcher" not in s["hooks"]["Stop"][0]
    cmd = s["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert "hook Stop" in cmd and str(ev) in cmd


def test_record_from_payload_summarizes_tools():
    r = H.record_from_payload("PreToolUse", {"tool_name": "Bash",
                                             "tool_input": {"command": "ls"}})
    assert r.tool == "Bash" and r.summary == "ls"
    r2 = H.record_from_payload("PreToolUse", {"tool_name": "Read",
                                              "tool_input": {"file_path": "/x/y.py"}})
    assert r2.summary == "/x/y.py"


def test_record_from_payload_askuserquestion_uses_question():
    payload = {"tool_name": "AskUserQuestion",
               "tool_input": {"questions": [{"question": "Tabs or spaces?"}]}}
    r = H.record_from_payload("PreToolUse", payload)
    assert r.tool == "AskUserQuestion"
    assert r.summary == "Tabs or spaces?"


def test_handle_hook_appends_record(tmp_path):
    ev = tmp_path / "e.jsonl"
    payload = json.dumps({"tool_name": "Grep", "tool_input": {"pattern": "foo"}})
    rc = H.handle_hook("PreToolUse", ev, stdin=io.StringIO(payload))
    assert rc == 0
    lines = ev.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["event"] == "PreToolUse" and rec["tool"] == "Grep" and rec["summary"] == "foo"


def test_handle_hook_tolerates_garbage_stdin(tmp_path):
    ev = tmp_path / "e.jsonl"
    assert H.handle_hook("Stop", ev, stdin=io.StringIO("not json")) == 0
    rec = json.loads(ev.read_text().splitlines()[0])
    assert rec["event"] == "Stop" and rec["tool"] == ""


def test_event_tail_reads_incrementally_and_detects_stop(tmp_path):
    ev = tmp_path / "e.jsonl"
    ev.write_text("")  # baseline: existing (empty) file
    tail = H.EventTail(ev)
    tail.seek_to_end()
    assert tail.poll() == []

    with ev.open("a") as f:
        f.write(json.dumps({"ts": 1, "event": "PreToolUse", "tool": "Bash", "summary": "ls"}) + "\n")
    got = tail.poll()
    assert len(got) == 1 and got[0].tool == "Bash"
    assert not tail.saw_stop

    with ev.open("a") as f:
        f.write(json.dumps({"ts": 2, "event": "Stop"}) + "\n")
    got = tail.poll()
    assert len(got) == 1 and got[0].is_stop
    assert tail.saw_stop


def test_event_tail_ignores_prior_turn(tmp_path):
    ev = tmp_path / "e.jsonl"
    ev.write_text(json.dumps({"ts": 1, "event": "Stop"}) + "\n")
    tail = H.EventTail(ev)
    tail.seek_to_end()  # baseline past the prior turn's Stop
    assert tail.poll() == []
    assert not tail.saw_stop


def test_event_tail_handles_partial_line(tmp_path):
    ev = tmp_path / "e.jsonl"
    ev.write_text("")
    tail = H.EventTail(ev)
    tail.seek_to_end()
    # write a record without its trailing newline yet
    rec = json.dumps({"ts": 1, "event": "PreToolUse", "tool": "Read", "summary": "a"})
    with ev.open("a") as f:
        f.write(rec)
    assert tail.poll() == []  # incomplete line buffered, not yet emitted
    with ev.open("a") as f:
        f.write("\n")
    got = tail.poll()
    assert len(got) == 1 and got[0].tool == "Read"


def test_final_response_reads_last_assistant_text(tmp_path, monkeypatch):
    # Build a fake transcript in the cwd-derived location the backend computes.
    import os
    from kage.backends import claude as cl
    monkeypatch.setattr(os, "getcwd", lambda: str(tmp_path))
    sid = "11111111-2222-3333-4444-555555555555"
    proj = tmp_path / ".claude_home"
    monkeypatch.setattr(cl.Path, "home", classmethod(lambda cls: proj))
    f = cl._conversation_file(sid)
    f.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "working on it"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "the answer is 42"}]}},
    ]
    f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert ClaudeBackend().final_response(sid) == "the answer is 42"


def test_final_response_missing_transcript_returns_none():
    assert ClaudeBackend().final_response("no-such-session-id") is None
