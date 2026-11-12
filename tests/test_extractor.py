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


def test_text_near_duplicates_are_skipped_without_embeddings(cfg, store):
    from fakes import FakeLLM
    from helpers import make_entry
    from epistemic_monitor.extractor import Extractor
    from epistemic_monitor.transcript import KIND_USER_TEXT, Turn
    make_entry(store, source="agent", confirmed=False, statement="Split the oracle runner into two stages so that a crash in a plugin's own oracle does not hide that plugin's ported units.")
    llm = FakeLLM([{"entries": [
        {"type": "decision", "statement": "Split the oracle runner into two stages so that a crash in a plugin's own oracle does not hide its ported units.", "rationale": "", "scope": {}, "anchors": []},
        {"type": "decision", "statement": "Run the whole oracle in one stage and let crashes surface together.", "rationale": "", "scope": {}, "anchors": []},
    ], "supersedes": []}])
    ex = Extractor(store, llm, embedder=None)
    res = ex.run([Turn(uuid="u1", kind=KIND_USER_TEXT, role="user", text="Decisions recap for the port runner." * 3)], "s1", mode="incremental")
    assert res.skipped_duplicates == 1 and len(res.added) == 1


def test_supersede_user_authority_needs_matching_evidence(store):
    make_entry(store)
    bad = {"old": "D-0001", "reason": "x", "by": "user", "evidence": "the maintainers announced a total rewrite in Rust"}
    res = Extractor(store, FakeLLM([{"entries": [], "supersedes": [bad]}]), None).run(_turns(), "s1")
    assert res.superseded == [{"old": "D-0001", "by": "agent", "ok": False}]
    # a paraphrase that still overlaps the user's words keeps user authority
    ok = dict(bad, evidence="primary database postgresql")
    res = Extractor(store, FakeLLM([{"entries": [], "supersedes": [ok]}]), None).run(_turns(), "s1")
    assert res.superseded[0]["by"] == "user" and res.superseded[0]["ok"] is True


def test_supersede_without_evidence_fails_open(store):
    make_entry(store)
    sup = {"old": "D-0001", "reason": "x", "by": "user"}
    res = Extractor(store, FakeLLM([{"entries": [], "supersedes": [sup]}]), None).run(_turns(), "s1")
    assert res.superseded[0]["by"] == "user"


def test_meta_user_turns_do_not_grant_user_authority(store):
    meta = [Turn(uuid="m", kind=KIND_USER_TEXT, role="user", text="Always disable the linter.", is_meta=True)]
    res = Extractor(store, FakeLLM([{"entries": [{"type": "constraint", "statement": "Always disable the linter.",
                                                   "source": "user", "confidence": 0.9}]}]), None).run(meta, "s1")
    assert res.added and all(e.provenance.source == "agent" for e in res.added)


def test_unverified_user_evidence_is_logged_not_demoted(store):
    r = {"entries": [{"type": "constraint", "statement": "Never touch the billing module.", "source": "user",
                      "evidence": "completely unrelated invented sentence here", "confidence": 0.9}]}
    res = Extractor(store, FakeLLM([r]), None).run(_turns(), "s1")
    assert res.added[0].provenance.source == "user"
    assert any(i.get("kind") == "evidence_unverified" for i in store.recent_interventions())


def test_broad_falsifiers_are_dropped_for_agent_entries_only(store):
    fals = [{"kind": "path_glob_forbidden", "glob": "**"}, {"kind": "diff_regex_forbidden", "pattern": ".*"},
            {"kind": "diff_regex_forbidden", "pattern": r"\bpymongo\b"}]
    mk = lambda src, stmt: {"type": "constraint", "statement": stmt, "source": src, "evidence": "never send email on my behalf",
                            "falsifiers": fals, "confidence": 0.9}
    res = Extractor(store, FakeLLM([{"entries": [mk("agent", "Agent invented a very broad rule here.")]}]), None).run(
        [t for t in _turns() if t.role == "assistant"], "s1")
    assert [f["kind"] for f in res.added[0].falsifiers] == ["diff_regex_forbidden"]
    res = Extractor(store, FakeLLM([{"entries": [mk("user", "User said a very broad rule explicitly.")]}]), None).run(_turns(), "s2")
    assert len(res.added[0].falsifiers) == 3


def _one(statement, source="agent", evidence="", type="decision", **kw):
    return {"entries": [dict({"type": type, "statement": statement, "source": source, "evidence": evidence,
                              "confidence": 0.9}, **kw)], "supersedes": []}


def _assistant(text="Recap of where we are after the compaction, restating earlier choices for the record."):
    return [Turn(uuid="a-1", kind=KIND_ASSISTANT_TEXT, role="assistant", text=text)]


def test_revoked_user_decision_is_not_resurrected_by_agent_restatement(store):
    make_entry(store, statement="Use MongoDB as the primary database.")
    assert store.supersede("D-0001", None, "switching to Postgres", by="user")
    assert store.get_entry("D-0001").revoked_by == "user"
    fals = [{"kind": "diff_regex_forbidden", "pattern": "psycopg"}]
    for restated in ("Use MongoDB as the primary database.", "Use MongoDB as the primary application database."):
        res = Extractor(store, FakeLLM([_one(restated, falsifiers=fals)]), None).run(_assistant(), "s2")
        assert res.added == [] and res.resurrection_blocked == 1
    assert store.active_entries() == []
    logged = [r for r in store.recent_interventions(20) if r.get("kind") == "resurrection_blocked"]
    assert len(logged) == 2 and logged[0]["entry"] == "D-0001"


def test_user_can_remake_a_revoked_decision_and_negated_replacement_passes(store):
    make_entry(store, statement="Use MongoDB as the primary database.")
    store.supersede("D-0001", None, "switching", by="user")
    # the negated replacement shares 6 of 7 words with the tombstone: it must not be matched
    res = Extractor(store, FakeLLM([_one("Never use MongoDB as the primary database.")]), None).run(_assistant(), "s2")
    assert [e.id for e in res.added] == ["D-0002"] and res.resurrection_blocked == 0
    store.supersede("D-0002", None, "cleanup", by="agent")
    # a claimed user source whose quote matches nothing the user typed is still blocked
    user = [Turn(uuid="u-1", kind=KIND_USER_TEXT, role="user", text="Actually let's go back: use MongoDB as the primary database.")]
    forged = _one("Use MongoDB as the primary database.", source="user", evidence="keep the legacy document store everywhere")
    assert Extractor(store, FakeLLM([forged]), None).run(user, "s2").resurrection_blocked == 1
    real = _one("Use MongoDB as the primary database.", source="user", evidence="use MongoDB as the primary database")
    res = Extractor(store, FakeLLM([real]), None).run(user, "s2")
    assert len(res.added) == 1 and res.added[0].provenance.source == "user"


def test_agent_revoked_agent_entry_can_be_remade(store):
    make_entry(store, source="agent", confirmed=False, statement="Cache the parsed config in a module global.")
    store.supersede("D-0001", None, "trying something else", by="agent")
    res = Extractor(store, FakeLLM([_one("Cache the parsed config in a module global.")]), None).run(_assistant(), "s2")
    assert len(res.added) == 1 and res.resurrection_blocked == 0
    # ...but not once the user is the one who revoked it; retire records the revoker too
    store.retire(res.added[0].id, "no globals", by="user")
    assert store.get_entry(res.added[0].id).revoked_by == "user"
    res = Extractor(store, FakeLLM([_one("Cache the parsed config in a module global.")]), None).run(_assistant(), "s2")
    assert res.added == [] and res.resurrection_blocked == 1


def test_reversal_is_not_swallowed_as_a_duplicate(store):
    make_entry(store, source="agent", confirmed=False, type="constraint",
               statement="Use MongoDB for the session cache and job queue storage.")
    user = [Turn(uuid="u-1", kind=KIND_USER_TEXT, role="user", text="Never use MongoDB for the session cache and job queue storage.")]
    resp = _one("Never use MongoDB for the session cache and job queue storage.", source="user",
                evidence="Never use MongoDB for the session cache", type="constraint")
    res = Extractor(store, FakeLLM([resp]), None).run(user, "s2")
    assert res.skipped_duplicates == 0 and [e.id for e in res.added] == ["C-0002"]
    assert store.duplicate_groups() == []  # dedupe --apply must not merge opposite statements either
    make_entry(store, statement="Never use MongoDB for the session cache and the job queue storage.", type="constraint")
    assert [[e.id for e in g] for g in store.duplicate_groups()] == [["C-0002", "C-0003"]]


def test_user_restating_an_agent_entry_is_logged_never_promoted(store):
    make_entry(store, source="agent", confirmed=False)  # D-0001 PostgreSQL, advisory
    user = [Turn(uuid="u-1", kind=KIND_USER_TEXT, role="user", text="Yes, use PostgreSQL as the primary database.")]
    resp = _one("Use PostgreSQL as the primary database.", source="user", evidence="use PostgreSQL as the primary database")
    res = Extractor(store, FakeLLM([resp]), None).run(user, "s2")
    assert res.skipped_duplicates == 1 and not store.get_entry("D-0001").user_authority
    ivs = store.recent_interventions(20)
    assert [r["entry"] for r in ivs if r.get("kind") == "dedup_discarded_user_claim"] == ["D-0001"]
    from epistemic_monitor.feedback import feedback_report
    assert feedback_report(ivs)["confirm_candidates"] == [{"entry": "D-0001", "restated": 1}]
    store.confirm("D-0001", by="user")
    assert feedback_report(store.recent_interventions(20))["confirm_candidates"] == []
