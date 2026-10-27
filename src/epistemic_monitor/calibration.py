"""Calibration of decision-model thresholds (DESIGN.md §8.1).

Labels come from ``ledger label`` (a human says whether a flagged tool call
really conflicted) and from the bench. Thresholds are fit per entry type,
maximizing F1 subject to a false-deny ceiling, and a VoS-style quantile
threshold is fit from sessions marked known-good. Everything is keyed by
(provider, model) so a provider swap never reuses another's thresholds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .schema import now_iso

GRID = [round(0.30 + 0.05 * i, 2) for i in range(14)]  # 0.30 .. 0.95
MIN_LABELS_PER_TYPE = 12


@dataclass
class Calibration:
    provider_key: str = ""
    tau_by_type: dict[str, float] = field(default_factory=dict)
    quantile_threshold: float | None = None
    n_labels: int = 0
    n_good_sessions: int = 0
    fitted_at: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def tau(self, entry_type: str, default: float) -> float:
        return float(self.tau_by_type.get(entry_type, default))

    @property
    def calibrated(self) -> bool:
        return bool(self.tau_by_type)

    def to_dict(self) -> dict[str, Any]:
        return {"provider_key": self.provider_key, "tau_by_type": self.tau_by_type,
                "quantile_threshold": self.quantile_threshold, "n_labels": self.n_labels,
                "n_good_sessions": self.n_good_sessions, "fitted_at": self.fitted_at, "details": self.details}


def provider_key(cfg: Config) -> str:
    return f"{cfg.get('decision_model', 'provider')}:{cfg.get('decision_model', 'model')}"


def labels_path(cfg: Config) -> Path:
    return cfg.data_dir / "labels.jsonl"


def good_sessions_path(cfg: Config) -> Path:
    return cfg.data_dir / "good_sessions.json"


def load_calibration(cfg: Config) -> Calibration:
    p = cfg.calibration_path
    if not p.exists():
        return Calibration(provider_key=provider_key(cfg))
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return Calibration(provider_key=provider_key(cfg))
    if d.get("provider_key") != provider_key(cfg):
        return Calibration(provider_key=provider_key(cfg))  # another provider's thresholds are not reused
    return Calibration(provider_key=d.get("provider_key", ""), tau_by_type={k: float(v) for k, v in (d.get("tau_by_type") or {}).items()},
                       quantile_threshold=d.get("quantile_threshold"), n_labels=int(d.get("n_labels", 0)),
                       n_good_sessions=int(d.get("n_good_sessions", 0)), fitted_at=d.get("fitted_at", ""),
                       details=d.get("details") or {})


def add_label(cfg: Config, record: dict[str, Any]) -> None:
    cfg.ensure_dirs()
    with open(labels_path(cfg), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": now_iso(), **record}, ensure_ascii=False) + "\n")


def read_labels(cfg: Config) -> list[dict[str, Any]]:
    p = labels_path(cfg)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def mark_good_session(cfg: Config, session_id: str) -> None:
    p = good_sessions_path(cfg)
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    except ValueError:
        data = []
    if session_id not in data:
        data.append(session_id)
    p.write_text(json.dumps(data), encoding="utf-8")


def good_sessions(cfg: Config) -> list[str]:
    p = good_sessions_path(cfg)
    try:
        return list(json.loads(p.read_text(encoding="utf-8"))) if p.exists() else []
    except ValueError:
        return []


def fit_tau(samples: list[tuple[float, bool]], max_false_deny: float = 0.02) -> tuple[float | None, dict[str, Any]]:
    """samples: (p_conflict, is_conflict). Pick τ on the grid maximizing F1 with
    false-deny rate (consistent samples with p ≥ τ, over all consistent samples) ≤ ceiling.
    Falls back to the τ with the lowest false-deny rate if none satisfies the ceiling."""
    if not samples:
        return None, {"reason": "no samples"}
    pos = [p for p, y in samples if y]
    neg = [p for p, y in samples if not y]
    best, best_stats = None, None
    fallback, fallback_fd = None, 1.0
    for tau in GRID:
        tp = sum(1 for p in pos if p >= tau)
        fp = sum(1 for p in neg if p >= tau)
        fn = len(pos) - tp
        fd = fp / len(neg) if neg else 0.0
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        stats = {"tau": tau, "tp": tp, "fp": fp, "fn": fn, "false_deny": round(fd, 4), "precision": round(prec, 3),
                 "recall": round(rec, 3), "f1": round(f1, 3)}
        if fd <= max_false_deny and (best is None or f1 > best_stats["f1"] or (f1 == best_stats["f1"] and tau < best)):
            best, best_stats = tau, stats
        if fd < fallback_fd or (fd == fallback_fd and fallback is None):
            fallback, fallback_fd = tau, fd
    if best is not None:
        return best, best_stats
    return fallback, {"reason": "no tau met the false-deny ceiling; using the lowest-false-deny tau", "tau": fallback,
                      "false_deny": round(fallback_fd, 4)}


def fit_quantile(session_max_scores: list[float], alpha: float = 0.05) -> float | None:
    """(1-α) quantile of the per-session maximum conflict score on known-good sessions."""
    if len(session_max_scores) < 5:
        return None
    xs = sorted(session_max_scores)
    k = min(len(xs) - 1, max(0, int(round((1 - alpha) * (len(xs) - 1)))))
    return round(xs[k], 3)


def calibrate(cfg: Config, gate_records: list[dict[str, Any]], max_false_deny: float = 0.02,
              alpha: float = 0.05, min_labels: int = MIN_LABELS_PER_TYPE) -> Calibration:
    """Fit from labels (each: {tool_use_id, label: conflict|ok, scores: {entry_id: p}, types: {entry_id: type}})
    and from gate records of known-good sessions (shadow scores)."""
    labels = read_labels(cfg)
    by_type: dict[str, list[tuple[float, bool]]] = {}
    for lab in labels:
        is_conflict = lab.get("label") == "conflict"
        scores = lab.get("scores") or {}
        types = lab.get("types") or {}
        for eid, p in scores.items():
            try:
                by_type.setdefault(types.get(eid, "decision"), []).append((float(p), is_conflict))
            except (TypeError, ValueError):
                continue
    cal = Calibration(provider_key=provider_key(cfg), fitted_at=now_iso(), n_labels=len(labels))
    for etype, samples in by_type.items():
        if len(samples) < min_labels:
            cal.details[etype] = {"reason": f"only {len(samples)} samples (< {min_labels}); default kept"}
            continue
        tau, stats = fit_tau(samples, max_false_deny)
        if tau is not None:
            cal.tau_by_type[etype] = tau
        cal.details[etype] = stats
    good = set(good_sessions(cfg))
    per_session: dict[str, float] = {}
    for r in gate_records:
        sid = r.get("session_id")
        if sid in good:
            p = r.get("max_p_conflict")
            if isinstance(p, (int, float)):
                per_session[sid] = max(per_session.get(sid, 0.0), float(p))
    cal.n_good_sessions = len(per_session)
    cal.quantile_threshold = fit_quantile(list(per_session.values()), alpha)
    cfg.ensure_dirs()
    cfg.calibration_path.write_text(json.dumps(cal.to_dict(), indent=2), encoding="utf-8")
    return cal
