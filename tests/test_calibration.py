from epistemic_monitor.calibration import (Calibration, add_label, calibrate, fit_quantile, fit_tau, load_calibration,
                                           mark_good_session)


def test_fit_tau_respects_false_deny_ceiling():
    samples = [(0.9, True), (0.85, True), (0.8, True), (0.6, True), (0.55, False), (0.4, False), (0.3, False), (0.75, False)]
    tau, stats = fit_tau(samples, max_false_deny=0.0)
    assert tau == 0.8 and stats["fp"] == 0 and stats["recall"] == 0.75
    tau, stats = fit_tau(samples, max_false_deny=0.3)
    assert tau == 0.6 and stats["recall"] == 1.0
    assert fit_tau([], 0.02)[0] is None


def test_fit_quantile():
    assert fit_quantile([0.1, 0.2, 0.3], 0.05) is None
    assert fit_quantile([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95], 0.1) == 0.9


def test_calibrate_end_to_end_and_provider_key(cfg):
    for i in range(14):
        add_label(cfg, {"tool_use_id": f"t{i}", "label": "conflict" if i % 2 == 0 else "ok",
                        "scores": {"D-0001": 0.9 if i % 2 == 0 else 0.3}, "types": {"D-0001": "decision"}, "session_id": "s"})
    for s in ("g1", "g2", "g3", "g4", "g5"):
        mark_good_session(cfg, s)
    gates = [{"kind": "gate", "session_id": s, "max_p_conflict": p} for s, p in
             [("g1", 0.2), ("g2", 0.3), ("g3", 0.5), ("g4", 0.4), ("g5", 0.35), ("bad", 0.99)]]
    cal = calibrate(cfg, gates, max_false_deny=0.02, alpha=0.2, min_labels=12)
    assert cal.tau_by_type["decision"] == 0.35 and cal.n_labels == 14 and cal.n_good_sessions == 5
    assert cal.quantile_threshold == 0.4  # (1-0.2) quantile of 5 known-good maxima
    loaded = load_calibration(cfg)
    assert loaded.calibrated and loaded.tau("decision", 0.75) == 0.35 and loaded.tau("process", 0.6) == 0.6
    # thresholds are keyed by decision-model provider: another provider does not inherit them
    cfg.values["decision_model"]["model"] = "other-model"
    assert load_calibration(cfg).calibrated is False


def test_calibration_object_defaults():
    c = Calibration()
    assert not c.calibrated and c.tau("decision", 0.75) == 0.75
