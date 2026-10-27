from fakes import FakeLLM
from helpers import make_entry

from epistemic_monitor.extractor import Extractor, chunk_turns, format_turns, validate_falsifiers
from epistemic_monitor.transcript import KIND_ASSISTANT_TEXT, KIND_TOOL_USE, KIND_USER_TEXT, Turn


def _turns():
    return [
        Turn(uuid="u-1", kind=KIND_USER_TEXT, role="user", text="Let's use PostgreSQL as the primary database. Also never send email on my behalf, draft only."),
        Turn(uuid="a-2", kind=KIND_ASSISTANT_TEXT, role="assistant", text="Understood. I'll also run pytest before every commit."),
        Turn(uuid="a-2", kind=KIND_TOOL_USE, role="assistant", tool_name="Edit", tool_input={"file_path": "src/db/client.py"}),
    ]


def _resp(extra_entries=None, supersedes=None):
    entries = [
        {"type": "decision", "statement": "Use PostgreSQL as the primary database.", "rationale": "user choice",
         "source": "user", "evidence": "Let's use PostgreSQL", "turn_uuid": "u-1",
         "scope": {"paths": ["src/db/**"], "tools": [], "topics": ["database"]},
         "falsifiers": [{"kind": "diff_regex_forbidden", "pattern": r"\bpymongo\b"}, {"kind": "diff_regex_forbidden", "pattern": "([unclosed"},
                        {"kind": "bogus"}],
         "depends_on": ["D-9999"], "confidence": 0.95},
        {"type": "constraint", "statement": "Never send email on the user's behalf; draft only.", "source": "user",
         "evidence": "never send email on my behalf", "turn_uuid": "u-1", "confidence": 0.9},
        {"type": "process", "statement": "Run pytest before every commit.", "source": "agent", "evidence": "run pytest before every commit",
         "turn_uuid": "a-2", "confidence": 0.8},
        {"type": "decision", "statement": "low confidence noise", "source": "agent", "confidence": 0.2},
        {"type": "wat", "statement": "bad type", "confidence": 0.9},
    ] + (extra_entries or [])
    return {"entries": entries, "supersedes": supersedes or []}


def test_extraction_adds_validated_entries(store):
    llm = FakeLLM([_resp()])
    res = Extractor(store, llm, None).run(_turns(), "s1", mode="full")
    assert [e.id for e in res.added] == ["D-0001", "C-0001", "R-0001"]
    assert res.rejected == 2 and res.calls == 1
    d = store.get_entry("D-0001")
    assert d.provenance.source == "user" and d.provenance.confirmed_by_user and d.provenance.turn_uuid == "u-1"
    assert d.falsifiers == [{"kind": "diff_regex_forbidden", "pattern": r"\bpymongo\b"}]  # invalid ones dropped
    assert d.depends_on == []  # unknown id filtered
    assert "PostgreSQL" in d.anchors and "src/db" in d.anchors  # glob chars are stripped; the prefix is the useful anchor
    assert store.get_entry("R-0001").provenance.source == "agent"
    assert "EXISTING LEDGER" in llm.calls[0][1] and "[user u-1]" in llm.calls[0][1] and "used tool Edit" in llm.calls[0][1]


def test_duplicates_are_skipped_and_ledger_is_shown(store):
    make_entry(store)  # D-0001 PostgreSQL already there
    llm = FakeLLM([_resp()])
    res = Extractor(store, llm, None).run(_turns(), "s1")
    assert res.skipped_duplicates == 1 and [e.id for e in res.added] == ["C-0001", "R-0001"]
    assert "D-0001" in llm.calls[0][1]


def test_user_source_requires_a_user_turn(store):
    llm = FakeLLM([_resp()])
    only_assistant = [t for t in _turns() if t.role == "assistant"]
    res = Extractor(store, llm, None).run(only_assistant, "s1", mode="incremental")
    assert all(e.provenance.source == "agent" for e in res.added)


def test_supersede_goes_through_authority_rule(store):
    make_entry(store)  # D-0001 user
    llm = FakeLLM([{"entries": [], "supersedes": [{"old": "D-0001", "reason": "switching", "by": "agent"}]}])
    res = Extractor(store, llm, None).run(_turns(), "s1")
    assert res.superseded == [{"old": "D-0001", "by": "agent", "ok": False}]
    assert store.get_entry("D-0001").status == "active"
    llm = FakeLLM([{"entries": [], "supersedes": [{"old": "D-0001", "reason": "user said so", "by": "user"}]}])
    res = Extractor(store, llm, None).run(_turns(), "s1")
    assert res.superseded[0]["ok"] is True and store.get_entry("D-0001").status == "superseded"


def test_chunking_and_llm_errors(store):
    big = [Turn(uuid=f"u-{i}", kind=KIND_USER_TEXT, role="user", text="x" * 5000) for i in range(6)]
    assert len(chunk_turns(big, 12000)) == 3
    llm = FakeLLM(["not json at all"])
    res = Extractor(store, llm, None, max_chars_per_call=12000).run(big, "s1")
    assert res.calls == 3 and res.errors and res.added == []
    assert format_turns(big[:1], 100).endswith("x" * 50)


def test_incremental_uses_prompt_and_previous_assistant(store):
    llm = FakeLLM([{"entries": [{"type": "commitment", "statement": "Report test results before every push.", "source": "agent",
                                 "confidence": 0.9}]}])
    res = Extractor(store, llm, None).run_incremental("ok go ahead", "I will report test results before every push.", "s1")
    assert [e.id for e in res.added] == ["K-0001"]
    assert "[assistant ]" in llm.calls[0][1] and "[user ]: ok go ahead" in llm.calls[0][1]


def test_validate_falsifiers():
    out = validate_falsifiers([
        {"kind": "path_glob_required_with", "glob": "src/api/*.py", "requires": "tests/api/test_{stem}.py"},
        {"kind": "path_glob_required_with", "glob": "src/api/*.py", "requires": "no-stem"},
        {"kind": "command_regex_forbidden", "pattern": "pip install (pymongo|motor)"},
        {"kind": "shell_check", "command": "pytest -q"},
        "garbage",
    ])
    assert [f["kind"] for f in out] == ["path_glob_required_with", "command_regex_forbidden", "shell_check"]
