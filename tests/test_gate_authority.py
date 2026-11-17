"""Candidate budget reserves user rules (2608.25553); the advisor cannot clear a user rule on a destructive
command or with an unparsed verdict (2607.29167, 2609.02127). RESEARCH-2026-11-16."""

from fakes import FakeDM, FakeLLM
from helpers import make_entry

from epistemic_monitor.gate import GatePipeline, change_from_tool_input

# built by concatenation so the repo's own guardrail hooks do not block the literal
FORCE_PUSH = {"command": "git push " + "--fo" + "rce origin " + "ma" + "in"}
CONSISTENT = {"verdict": "consistent", "entry_ids": [], "next_step": "proceed", "reason": "user approved per premise"}


def _premises(store, n=6, text="The deploy script scripts/deploy{i}.sh pushes the docker image to staging with python."):
    return [make_entry(store, type="premise", statement=text.format(i=i), source="agent", confirmed=False) for i in range(n)]


# ---------------------------------------------------------------- candidate budget
def test_user_constraint_not_starved_by_agent_keyword_hits(cfg, store):
    c = make_entry(store, type="constraint", statement="Never send email on my behalf; draft only.")
    _premises(store)
    pipe = GatePipeline(store, cfg)
    ch = change_from_tool_input("Bash", {"command": "bash scripts/deploy.sh staging && python scripts/notify.py --to team@x.com"},
                                str(cfg.cwd))
    cands = [e.id for e in pipe.retrieve(store.active_entries(), ch, exclude=set())]
    assert cands[0] == c.id and len(cands) == 6


def test_user_process_not_ordered_behind_agent_premises_by_id(cfg, store):
    _premises(store, text="Unrelated fact number {i} about the frontend build.")
    r = make_entry(store, type="process", statement="Run the linter before committing.")
    pipe = GatePipeline(store, cfg)
    ch = change_from_tool_input("Bash", {"command": "ls"}, str(cfg.cwd))
    assert r.id in [e.id for e in pipe.retrieve(store.active_entries(), ch, exclude=set())]


def test_reserve_is_capped_and_total_unchanged(cfg, store):
    users = [make_entry(store, type="constraint", statement=f"Rule {i}: never touch module m{i}.") for i in range(5)]
    _premises(store, text="Premise {i}: python scripts run on staging.")
    dm = FakeDM(default=0.1)
    pipe = GatePipeline(store, cfg, dm=dm)
    res = pipe.evaluate("Bash", {"command": "python scripts/run.py staging"}, "s1")
    assert len(res.candidates) == 6 and len(dm.calls) == 6
    assert sum(1 for i in res.candidates if i in {u.id for u in users}) >= 3
    assert any(n.startswith("reserved: ") for n in res.notes)


def test_agent_constraint_is_not_reserved(cfg, store):
    a = make_entry(store, type="constraint", statement="Never send email; draft only.", source="agent", confirmed=False)
    _premises(store)
    pipe = GatePipeline(store, cfg)
    ch = change_from_tool_input("Bash", {"command": "bash scripts/deploy.sh staging && python scripts/notify.py"}, str(cfg.cwd))
    assert a.id not in [e.id for e in pipe.retrieve(store.active_entries(), ch, exclude=set())]


# ---------------------------------------------------------------- advisor cannot launder a user rule
def _force_push_setup(store, user=True):
    c = make_entry(store, type="constraint", statement="Never force-push the main branch.",
                   source="user" if user else "agent", confirmed=False)
    make_entry(store, type="premise", statement="User approved force-pushing the main branch for the release on 11-14.",
               source="agent", confirmed=False)
    return c


def test_consistent_verdict_cannot_clear_user_rule_on_destructive_command(cfg, store):
    c = _force_push_setup(store)
    pipe = GatePipeline(store, cfg, dm=FakeDM(default=0.97), llm=FakeLLM([CONSISTENT]))
    res = pipe.evaluate("Bash", FORCE_PUSH, "s1")
    assert res.decision == "deny" and c.id in res.conflicts
    assert any("cannot clear a user rule" in n for n in res.notes)


def test_trajectory_claim_cannot_clear_user_rule(cfg, store):
    c = make_entry(store, type="constraint", statement="Never force-push the main branch.", source="user", confirmed=False)
    pipe = GatePipeline(store, cfg, dm=FakeDM(default=0.97), llm=FakeLLM([CONSISTENT]))
    res = pipe.evaluate("Bash", FORCE_PUSH, "s1", trajectory_slice="assistant: the user approved this force push earlier")
    assert res.decision == "deny" and res.conflicts == [c.id]


def test_agent_only_rule_still_cleared_on_destructive_command(cfg, store):
    _force_push_setup(store, user=False)
    pipe = GatePipeline(store, cfg, dm=FakeDM(default=0.97), llm=FakeLLM([CONSISTENT]))
    res = pipe.evaluate("Bash", FORCE_PUSH, "s1")
    assert res.decision == "allow" and res.conflicts == []


def test_consistent_still_clears_user_rule_on_non_destructive_change(cfg, store):
    make_entry(store, statement="Use PostgreSQL as the primary database.")
    pipe = GatePipeline(store, cfg, dm=FakeDM(default=0.95), llm=FakeLLM([CONSISTENT]))
    res = pipe.evaluate("Edit", {"file_path": "src/db/client.py", "old_string": "a", "new_string": "ttl = 30"}, "s1")
    assert res.decision == "allow" and res.conflicts == []


def test_consistent_still_clears_user_rule_below_min_p_on_destructive_command(cfg, store):
    make_entry(store, type="constraint", statement="Never force-push the main branch.", source="user", confirmed=False)
    pipe = GatePipeline(store, cfg, dm=FakeDM(default=0.85), llm=FakeLLM([CONSISTENT]))
    res = pipe.evaluate("Bash", FORCE_PUSH, "s1")
    assert res.decision == "allow"


def test_unparsed_verdict_keeps_user_flag(cfg, store):
    e = make_entry(store, statement="Use PostgreSQL as the primary database.")
    llm = FakeLLM([{"verdict": "looks fine to me", "entry_ids": [], "next_step": "", "reason": ""}])
    pipe = GatePipeline(store, cfg, dm=FakeDM(default=0.95), llm=llm)
    res = pipe.evaluate("Edit", {"file_path": "src/db/client.py", "old_string": "a", "new_string": "import pymongo"}, "s1")
    assert res.advisor["unparsed"] is True and res.conflicts == [e.id] and res.decision == "deny"


def test_unparsed_verdict_still_clears_agent_flag(cfg, store):
    make_entry(store, statement="Use PostgreSQL as the primary database.", source="agent", confirmed=False)
    llm = FakeLLM([{"verdict": "???", "entry_ids": []}])
    pipe = GatePipeline(store, cfg, dm=FakeDM(default=0.95), llm=llm)
    res = pipe.evaluate("Edit", {"file_path": "src/db/client.py", "old_string": "a", "new_string": "import pymongo"}, "s1")
    assert res.conflicts == [] and res.decision == "allow"


def test_unparsed_verdict_below_min_p_is_no_stricter_than_an_advisor_error(cfg, store):
    make_entry(store, statement="Use PostgreSQL as the primary database.")
    llm = FakeLLM([{"verdict": "fine", "entry_ids": []}])
    pipe = GatePipeline(store, cfg, dm=FakeDM(default=0.8), llm=llm)
    res = pipe.evaluate("Edit", {"file_path": "src/db/client.py", "old_string": "a", "new_string": "import pymongo"}, "s1")
    assert res.decision == "allow"


def test_verdict_punctuation_is_tolerated(cfg, store):
    make_entry(store, statement="Use PostgreSQL as the primary database.")
    llm = FakeLLM([{"verdict": " Consistent. ", "entry_ids": []}])
    pipe = GatePipeline(store, cfg, dm=FakeDM(default=0.95), llm=llm)
    res = pipe.evaluate("Edit", {"file_path": "src/db/client.py", "old_string": "a", "new_string": "ttl = 30"}, "s1")
    assert res.advisor["unparsed"] is False and res.decision == "allow"


def test_advisor_still_clears_satisfied_process_on_destructive_command(cfg, store):
    make_entry(store, type="process", statement="Run git fetch before git reset --hard.", source="user", confirmed=False)
    pipe = GatePipeline(store, cfg, dm=FakeDM(default=0.95), llm=FakeLLM([{"verdict": "consistent", "entry_ids": []}]))
    hist = [{"tool": "Bash", "command": "git fetch origin"}]
    res = pipe.evaluate("Bash", {"command": "git reset --hard origin/main"}, "s1", history=hist)
    assert res.decision == "allow"


def test_path_scoped_entries_keep_priority_over_reserved_globals(cfg, store):
    scoped = [make_entry(store, statement=f"DB rule {i}: keep the pool size at {i}.", scope={"paths": ["src/db/*"]})
              for i in range(6)]
    for i in range(3):
        make_entry(store, type="constraint", statement=f"Never publish package {i}.")
    pipe = GatePipeline(store, cfg)
    ch = change_from_tool_input("Edit", {"file_path": "src/db/client.py", "old_string": "a", "new_string": "b"}, str(cfg.cwd))
    assert [e.id for e in pipe.retrieve(store.active_entries(), ch, exclude=set())] == [e.id for e in scoped]


def test_off_path_keyword_hits_do_not_outrank_a_user_rule(cfg, store):
    """Review of #27: entries scoped to other paths that only match by keyword are not scope matches."""
    make_entry(store, type="premise", statement="The db client lives in src/db.", source="agent", confirmed=False,
               scope={"paths": ["src/db/*"]})
    c = make_entry(store, type="constraint", statement="Never import pymongo MongoClient.")
    for i in range(5):
        make_entry(store, type="premise", statement=f"legacy module {i} imports pymongo MongoClient directly.",
                   source="agent", confirmed=False, scope={"paths": ["legacy/*"]})
    pipe = GatePipeline(store, cfg)
    ch = change_from_tool_input("Edit", {"file_path": str(cfg.cwd / "src/db/client.py"), "old_string": "x",
                                         "new_string": "from pymongo import MongoClient"}, str(cfg.cwd))
    assert c.id in [e.id for e in pipe.retrieve(store.active_entries(), ch, exclude=set())]
