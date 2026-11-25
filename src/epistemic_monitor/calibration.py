"""Calibration of decision-model thresholds (DESIGN.md §8.1).

Labels come from ``ledger label`` (a human says whether a flagged tool call
really conflicted) and from the bench. Thresholds are fit per entry type,
maximizing F1 subject to a false-deny ceiling, and a VoS-style quantile
threshold is fit from sessions marked known-good. Everything is keyed by
(provider, model) so a provider swap never reuses another's thresholds.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .schema import now_iso

GRID = [round(0.30 + 0.05 * i, 2) for i in range(14)]  # 0.30 .. 0.95
MIN_LABELS_PER_TYPE = 12
CONFIDENCE = 0.95   # one-sided confidence for the false-deny upper bound (2610.36544: certify or abstain)


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

    def is_calibrated(self, entry_type: str) -> bool:
        """Per type: fitting one type says nothing about another's threshold."""
        return entry_type in self.tau_by_type

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
    details = d.get("details") or {}
    # Only thresholds certified by the interval rule are used. A file written before that rule (or edited by hand)
    # carries no certificate, so its thresholds are dropped and the defaults apply until `calibrate` is run again.
    taus = {k: float(v) for k, v in (d.get("tau_by_type") or {}).items()
            if isinstance(details.get(k), dict) and details[k].get("certified") is True}
    return Calibration(provider_key=d.get("provider_key", ""), tau_by_type=taus,
                       quantile_threshold=d.get("quantile_threshold"), n_labels=int(d.get("n_labels", 0)),
                       n_good_sessions=int(d.get("n_good_sessions", 0)), fitted_at=d.get("fitted_at", ""),
                       details=details)


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


def _binom_cdf(k: int, n: int, p: float) -> float:
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 1.0 if k >= n else 0.0
    lp, lq = math.log(p), math.log1p(-p)
    return min(1.0, sum(math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq)
                        for i in range(0, k + 1)))


def false_deny_upper(fp: int, n_neg: int, confidence: float = CONFIDENCE) -> float:
    """One-sided Clopper-Pearson upper bound on the false-deny rate after fp false denies in n_neg consistent
    samples. With no consistent samples nothing is known, so the bound is 1."""
    if n_neg <= 0:
        return 1.0
    if fp >= n_neg:
        return 1.0
    alpha = 1.0 - confidence
    lo, hi = fp / n_neg, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if _binom_cdf(fp, n_neg, mid) > alpha:
            lo = mid
        else:
            hi = mid
    return hi


def fit_tau(samples: list[tuple[float, bool]], max_false_deny: float = 0.02,
            confidence: float = CONFIDENCE) -> tuple[float | None, dict[str, Any]]:
    """samples: (p_conflict, is_conflict). Pick τ on the grid maximizing F1 among the τ whose false-deny rate
    (consistent samples with p ≥ τ, over all consistent samples) is certified ≤ the ceiling: the one-sided
    Clopper-Pearson upper bound at `confidence` must be under it, not just the observed rate (2610.36544).
    When no τ is certified the fit abstains (None) and the gate keeps its defaults. With the 2% default ceiling
    that takes about 150 consistent samples with no false deny at the chosen τ."""
    if not samples:
        return None, {"reason": "no samples", "certified": False}
    pos = [p for p, y in samples if y]
    neg = [p for p, y in samples if not y]
    if not neg:
        return None, {"reason": "no consistent (ok) samples; the false-deny rate is unknown", "certified": False,
                      "n_pos": len(pos), "n_neg": 0}
    best, best_stats = None, None
    closest: dict[str, Any] | None = None
    for tau in GRID:
        tp = sum(1 for p in pos if p >= tau)
        fp = sum(1 for p in neg if p >= tau)
        fn = len(pos) - tp
        fd = fp / len(neg)
        upper = false_deny_upper(fp, len(neg), confidence)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        stats = {"tau": tau, "tp": tp, "fp": fp, "fn": fn, "false_deny": round(fd, 4),
                 "false_deny_upper": round(upper, 4), "precision": round(prec, 3), "recall": round(rec, 3),
                 "f1": round(f1, 3), "n_pos": len(pos), "n_neg": len(neg)}
        if closest is None or upper < closest["false_deny_upper"]:
            closest = stats
        if tp and upper <= max_false_deny and (best is None or f1 > best_stats["f1"] or (f1 == best_stats["f1"] and tau < best)):
            best, best_stats = tau, stats
    if best is not None:
        return best, {**best_stats, "certified": True, "confidence": confidence}
    need = math.ceil(math.log(1.0 - confidence) / math.log(1.0 - max_false_deny)) if 0 < max_false_deny < 1 else None
    return None, {"reason": f"abstain: no tau has a {int(confidence * 100)}% upper bound on false denies within "
                            f"{max_false_deny}" + (f" (needs at least {need} consistent samples with none denied)" if need else ""),
                  "certified": False, "closest": closest}


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
        if tau is not None and stats.get("certified") is True:
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
