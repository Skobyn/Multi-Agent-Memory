"""Calibration certifies or abstains (arXiv 2610.36544): a threshold is used only when the false-deny ceiling holds
for the upper confidence bound, and each entry type is calibrated on its own."""
import json

from fakes import FakeDM
from helpers import make_entry

from epistemic_monitor.calibration import (Calibration, add_label, calibrate, false_deny_upper, fit_tau,
                                           load_calibration)
from epistemic_monitor.gate import GatePipeline


def _label(cfg, i, conflict, p, etype="decision"):
    add_label(cfg, {"tool_use_id": f"t{i}", "label": "conflict" if conflict else "ok",
                    "scores": {"X-1": p}, "types": {"X-1": etype}, "session_id": "s"})


def test_upper_bound_values():
    assert false_deny_upper(0, 0) == 1.0
    assert 0.23 < false_deny_upper(0, 11) < 0.25          # 0 of 11 still allows ~24%
    assert false_deny_upper(0, 149) <= 0.02 < false_deny_upper(0, 147)
    assert false_deny_upper(3, 3) == 1.0


def test_one_positive_eleven_negatives_abstains():
    samples = [(0.9, True)] + [(0.2, False)] * 11
    tau, stats = fit_tau(samples, 0.02)
    assert tau is None and stats["certified"] is False and "abstain" in stats["reason"]


def test_all_positive_labels_abstain():
    tau, stats = fit_tau([(0.9, True)] * 20, 0.02)
    assert tau is None and stats["n_neg"] == 0


def test_enough_clean_negatives_certify():
    tau, stats = fit_tau([(0.9, True)] * 10 + [(0.2, False)] * 150, 0.02)
    assert tau is not None and stats["certified"] is True


def test_failed_ceiling_stores_no_fallback_tau(cfg):
    for i in range(6):
        _label(cfg, i, True, 0.9)
    for i in range(6, 20):
        _label(cfg, i, False, 0.95 if i % 2 else 0.2)
    cal = calibrate(cfg, [], max_false_deny=0.02, min_labels=12)
    assert "decision" not in cal.tau_by_type and cal.details["decision"]["certified"] is False
    assert json.loads(cfg.calibration_path.read_text())["tau_by_type"] == {}
    assert not load_calibration(cfg).calibrated


def test_legacy_file_without_certificate_is_ignored(cfg):
    from epistemic_monitor.calibration import provider_key
    cfg.ensure_dirs()
    cfg.calibration_path.write_text(json.dumps({"provider_key": provider_key(cfg), "tau_by_type": {"decision": 0.3},
                                                "details": {"decision": {"tau": 0.3, "fp": 0}}}))
    assert not load_calibration(cfg).calibrated


def test_provider_mismatch_still_discards(cfg):
    for i in range(10):
        _label(cfg, i, True, 0.9)
    for i in range(10, 170):
        _label(cfg, i, False, 0.2)
    cal = calibrate(cfg, [], max_false_deny=0.02, min_labels=12)
    assert "decision" in cal.tau_by_type and load_calibration(cfg).is_calibrated("decision")
    cfg.values["decision_model"]["model"] = "other-model"
    assert load_calibration(cfg).calibrated is False


def test_calibrating_one_type_keeps_floor_for_others(cfg, store):
    """Before: any fitted type made `calibrated` true, which dropped the 0.9 floor for every type."""
    c = make_entry(store, type="constraint", statement="Use PostgreSQL as the primary database.")
    cal = Calibration(provider_key="x", tau_by_type={"process": 0.6})
    pipe = GatePipeline(store, cfg, dm=FakeDM(default=0.8), llm=None, calibration=cal)
    res = pipe.evaluate("Edit", {"file_path": "src/db/client.py", "old_string": "a",
                                 "new_string": "from pymongo import MongoClient"}, "s1")
    assert c.id in res.flagged and res.conflicts == [] and res.decision != "deny"
    assert any("uncalibrated" in n for n in res.notes)
    # the same score on a type that is certified does deny
    cal2 = Calibration(provider_key="x", tau_by_type={"constraint": 0.6})
    res2 = GatePipeline(store, cfg, dm=FakeDM(default=0.8), llm=None, calibration=cal2).evaluate(
        "Edit", {"file_path": "src/db/client.py", "old_string": "a", "new_string": "from pymongo import MongoClient"}, "s2")
    assert res2.conflicts == [c.id] and res2.decision == "deny"
