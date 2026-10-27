from fakes import FakeDM, FakeLLM
from helpers import make_entry

from epistemic_monitor.calibration import Calibration
from epistemic_monitor.gate import GatePipeline, change_from_tool_input, run_tier0, structural_signals


def _pg(store, **kw):
    return make_entry(store, statement="Use PostgreSQL as the primary database.", **kw)


def _edit(text, path="src/db/client.py"):
    return {"file_path": path, "old_string": "a", "new_string": text}


def test_tier1_retrieval_by_scope_keyword_and_embedding(cfg, store):
    scoped = _pg(store, scope={"paths": ["src/db/*"]})
    kw = make_entry(store, statement="Use Redis for caching.")
    other = make_entry(store, statement="Front-end tests use Vitest.")
    store.set_embedding(other.id, [0.0, 1.0])
    store.set_embedding(kw.id, [1.0, 0.0])

    class Emb:
        name = "fake"

        def embed(self, texts):
            return [[0.0, 1.0]]

    pipe = GatePipeline(store, cfg, dm=None, llm=None, embedder=Emb())
    change = change_from_tool_input("Edit", _edit("cache = redis.Redis()"), str(cfg.cwd))
    cands = [e.id for e in pipe.retrieve(store.active_entries(), change, exclude=set())]
    assert cands[0] == scoped.id            # scoped first
    assert kw.id in cands and other.id in cands  # keyword + embedding


def test_tier2_flags_and_resamples_near_threshold(cfg, store):
    e = _pg(store)
    dm = FakeDM(default=0.78)  # within ±0.1 of tau 0.75 -> resample
    pipe = GatePipeline(store, cfg, dm=dm, llm=None)
    res = pipe.evaluate("Edit", _edit("from pymongo import MongoClient"), "s1")
    sc = res.scores[e.id]
    assert sc["resampled"] is True and len(sc["samples"]) == 3 and sc["p_conflict"] == 0.78
    assert res.flagged == [e.id] and res.tier_reached == 2
    # uncalibrated and below enforce_uncalibrated_min_p -> no conflict, allow, with a note
    assert res.decision == "allow" and res.conflicts == [] and any("uncalibrated" in n for n in res.notes)


def test_uncalibrated_high_p_denies_without_advisor(cfg, store):
    e = _pg(store)
    pipe = GatePipeline(store, cfg, dm=FakeDM(default=0.97), llm=None)
    res = pipe.evaluate("Edit", _edit("from pymongo import MongoClient"), "s1")
    assert res.conflicts == [e.id] and res.decision == "deny" and "D-0001" in res.reason


def test_calibrated_tau_denies_at_lower_p(cfg, store):
    e = _pg(store)
    cal = Calibration(provider_key="x", tau_by_type={"decision": 0.6})
    pipe = GatePipeline(store, cfg, dm=FakeDM(default=0.7), llm=None, calibration=cal)
    res = pipe.evaluate("Edit", _edit("from pymongo import MongoClient"), "s1")
    assert res.scores[e.id]["tau"] == 0.6 and res.conflicts == [e.id] and res.decision == "deny"


def test_advisor_consistent_clears_model_flag(cfg, store):
    _pg(store)
    llm = FakeLLM([{"verdict": "consistent", "entry_ids": [], "next_step": "proceed", "reason": "just a pool size"}])
    pipe = GatePipeline(store, cfg, dm=FakeDM(default=0.95), llm=llm)
    res = pipe.evaluate("Edit", _edit("engine = create_engine(URL, pool_size=10)"), "s1")
    assert res.tier_reached == 3 and res.advisor["verdict"] == "consistent"
    assert res.conflicts == [] and res.decision == "allow"


def test_authorization_shaped_goes_to_advisor_regardless_of_score(cfg, store):
    make_entry(store, type="process", statement="Run pytest before every commit.")
    llm = FakeLLM([{"verdict": "conflict", "entry_ids": ["R-0001"], "next_step": "run pytest first", "reason": "commit without tests"}])
    pipe = GatePipeline(store, cfg, dm=FakeDM(default=0.2), llm=llm)
    res = pipe.evaluate("Bash", {"command": "git commit -am 'wip'"}, "s1")
    assert res.flagged == [] and res.tier_reached == 3
    assert res.conflicts == ["R-0001"] and res.decision == "deny" and "run pytest first" in res.reason


def test_command_requires_prior_falsifier(cfg, store):
    make_entry(store, type="process", statement="Run pytest before every commit.",
               falsifiers=[{"kind": "command_requires_prior", "pattern": r"git\s+(commit|push)", "prior_pattern": r"pytest", "window": 20}])
    pipe = GatePipeline(store, cfg, dm=None, llm=None)
    res = pipe.evaluate("Bash", {"command": "git commit -m x"}, "s1", history=[{"tool": "Bash", "command": "ls"}])
    assert [t.kind for t in res.trips] == ["command_requires_prior"] and res.decision == "deny"
    res = pipe.evaluate("Bash", {"command": "git commit -m x"}, "s2",
                        history=[{"tool": "Bash", "command": "pytest -q"}, {"tool": "Bash", "command": "ls"}])
    assert res.trips == [] and res.decision == "allow"


def test_provenance_force_and_budget_and_cap(cfg, store):
    agent_entry = make_entry(store, source="agent", confirmed=False, scope={"paths": ["src/x/*"]},
                             falsifiers=[{"kind": "diff_regex_forbidden", "pattern": "forbidden"}])
    user_entry = make_entry(store, statement="Never use MongoDB.", scope={"paths": ["src/y/*"]},
                            falsifiers=[{"kind": "diff_regex_forbidden", "pattern": "pymongo"}])
    pipe = GatePipeline(store, cfg, dm=None, llm=None)
    # a tripped falsifier is "hard" even with agent provenance
    res = pipe.evaluate("Edit", _edit("forbidden thing", "src/x/a.py"), "s1")
    assert res.decision == "deny"
    # consecutive-deny cap: after 5 denies on the same entry the gate lets it through with a citation
    for _ in range(5):
        res = pipe.evaluate("Edit", _edit("import pymongo", "src/y/a.py"), "s2")
        assert res.decision == "deny"
    res = pipe.evaluate("Edit", _edit("import pymongo", "src/y/a.py"), "s2")
    assert res.decision == "advisory" and any("cap_reached" in n for n in res.notes)
    # session deny budget (5) exhausted -> advisory
    for i in range(5):
        pipe.evaluate("Edit", _edit("import pymongo", f"src/y/{i}.py"), "s3")
    res = pipe.evaluate("Edit", _edit("import pymongo", "src/y/z.py"), "s3")
    assert res.decision == "advisory" and any("budget" in n for n in res.notes)


def test_structural_signals_and_first_write(cfg, store):
    change = change_from_tool_input("Edit", _edit("x = 1"), str(cfg.cwd))
    history = [{"tool": "Edit", "path": "src/db/client.py", "fingerprint": change.fingerprint(), "mutating": True}] * 8
    sig = structural_signals(change, history)
    kinds = {s["kind"] for s in sig}
    assert kinds == {"repeat", "stagnation"}
    res = run_tier0([], "Edit", _edit("x = 1"), str(cfg.cwd), history=[])
    assert res.first_mutating_write is True
    res = run_tier0([], "Bash", {"command": "ls"}, str(cfg.cwd), history=[])
    assert res.first_mutating_write is False


def test_first_write_advisor_reviews_pinned_ledger(cfg, store):
    _pg(store)
    llm = FakeLLM([{"verdict": "consistent", "entry_ids": [], "next_step": "keep using the Postgres engine", "reason": "fine"}])
    pipe = GatePipeline(store, cfg, dm=FakeDM(default=0.1), llm=llm)
    res = pipe.evaluate("Write", {"file_path": "src/new_module.py", "content": "x = 1"}, "s1", history=[])
    assert res.first_mutating_write and res.advisor["verdict"] == "consistent" and res.decision == "allow"
    assert any("first-write advisor" in n for n in res.notes)
    assert "first_mutating_write" in llm.calls[0][1]


def test_advisor_cooldown(cfg, store):
    e = _pg(store)
    llm = FakeLLM([{"verdict": "conflict", "entry_ids": [e.id], "next_step": "n", "reason": "r"}])
    pipe = GatePipeline(store, cfg, dm=FakeDM(default=0.95), llm=llm)
    pipe.evaluate("Edit", _edit("import pymongo"), "s1", history=[])
    assert len(llm.calls) == 1
    prior = {"tool": "Edit", "path": "src/db/client.py", "mutating": True}
    pipe.evaluate("Edit", _edit("import pymongo"), "s1", history=[prior])  # 1 step later: on cooldown
    assert len(llm.calls) == 1
    pipe.evaluate("Edit", _edit("import pymongo"), "s1", history=[prior] * 6)
    assert len(llm.calls) == 2
