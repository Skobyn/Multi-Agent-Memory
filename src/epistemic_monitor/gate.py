"""Conformance gate (DESIGN.md §8).

Tier 0  deterministic: falsifiers, structural drift signals, first mutating write
Tier 1  retrieval: scope globs, keyword search, embedding search
Tier 2  decision model: conflict probability + severity per candidate, per-type τ,
        near-threshold resampling with max aggregation, authorization-shaped routing
Tier 3  LLM advisor: concrete change + flagged entries + trajectory slice → verdict and
        one next step

The gate judges the concrete tool input (diff, command, path), never the plan;
never the agent's own confidence; and never blocks on a slow model.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .bash_classifier import WRITES_SOURCE, Classification, classify
from .schema import Entry

MUTATING_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash")
AUTHORIZATION_TYPES = ("process", "commitment")
_DESTRUCTIVE = re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f|\bgit\s+push\s+.*--force|\bDROP\s+(TABLE|DATABASE)\b|\btruncate\b|"
                          r"\|\s*(?:ba)?sh\b|\bgit\s+reset\s+--hard|\bkubectl\s+delete\b|\bterraform\s+destroy\b", re.I)

CONFLICT_INSTRUCTIONS = "Does the planned change conflict with the decision?"
CONFLICT_CRITERIA = {"true": "The change introduces, switches to, or depends on something the decision rules out, "
                             "skips a step the decision requires, or works against the decision's intent.",
                     "false": "The change is consistent with the decision or unrelated to it."}
CONFLICT_PARAPHRASES = ["Is the planned change inconsistent with the decision?",
                        "Would carrying out the planned change violate the decision?"]
SEVERITY_INSTRUCTIONS = "How serious would it be to let this change through given the decision?"
SEVERITY_LEVELS = ["harmless or unrelated", "minor deviation worth a citation",
                   "clear contradiction that should be reviewed", "direct reversal of a user decision"]

ADVISOR_SYSTEM = """You are the advisor of an epistemic monitor that guards a software project's recorded decisions,
constraints, premises, commitments and required process steps. A cheap check flagged a planned change.
Judge from the CONCRETE change (diff, command, path), not from stated intentions. Be precise and literal:
a change conflicts only if it actually does something an entry rules out, skips a step an entry requires,
or works against an entry's stated intent. Unrelated or merely adjacent changes are consistent.
Reply with ONLY JSON:
{"verdict": "consistent" | "conflict" | "supersede_proposed", "entry_ids": [ids that are conflicted],
 "next_step": "<one concrete sentence the agent should do next>", "reason": "<one or two sentences>"}
Use "supersede_proposed" when the change is a deliberate, reasonable replacement of a recorded decision that
should be made explicit rather than silently done. For process entries ("run X before Y"), check
recent_tool_calls_this_session: if the required step already ran, the change is consistent. Keep "reason" short."""


# ---------------------------------------------------------------- data
@dataclass
class Change:
    tool: str
    path: str = ""
    text: str = ""
    command: str = ""
    is_new_file: bool = False

    @property
    def is_mutating(self) -> bool:
        if self.tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            return True
        return self.tool == "Bash" and classify(self.command).verdict == WRITES_SOURCE

    def fingerprint(self) -> str:
        return hashlib.sha1(f"{self.tool}|{self.path}|{self.text[:4000]}".encode()).hexdigest()[:12]


@dataclass
class Trip:
    entry_id: str
    kind: str
    detail: str

    def to_dict(self) -> dict:
        return {"entry_id": self.entry_id, "kind": self.kind, "detail": self.detail}


@dataclass
class GateResult:
    change: Change
    trips: list[Trip] = field(default_factory=list)
    classification: Classification | None = None
    scoped_entries: list[str] = field(default_factory=list)
    cleared: list[str] = field(default_factory=list)   # entries whose deterministic process check passed; never model-scored
    tier_reached: int = 0
    decision: str = "allow"  # allow | deny | advisory
    reason: str = ""
    structural: list[dict[str, Any]] = field(default_factory=list)
    first_mutating_write: bool = False
    candidates: list[str] = field(default_factory=list)
    scores: dict[str, dict[str, Any]] = field(default_factory=dict)
    flagged: list[str] = field(default_factory=list)
    advisor: dict[str, Any] | None = None
    conflicts: list[str] = field(default_factory=list)
    latency_ms: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def max_p_conflict(self) -> float | None:
        ps = [s.get("p_conflict") for s in self.scores.values() if isinstance(s.get("p_conflict"), (int, float))]
        return max(ps) if ps else None

    def to_dict(self) -> dict:
        return {
            "tool": self.change.tool, "path": self.change.path, "is_new_file": self.change.is_new_file,
            "trips": [t.to_dict() for t in self.trips],
            "bash": self.classification.to_dict() if self.classification else None,
            "scoped_entries": self.scoped_entries, "cleared": self.cleared, "tier_reached": self.tier_reached,
            "decision": self.decision, "reason": self.reason[:2000], "structural": self.structural,
            "first_mutating_write": self.first_mutating_write, "candidates": self.candidates,
            "scores": self.scores, "flagged": self.flagged, "advisor": self.advisor, "conflicts": self.conflicts,
            "max_p_conflict": self.max_p_conflict, "latency_ms": self.latency_ms, "notes": self.notes,
        }


# ---------------------------------------------------------------- tier 0
def _rel(path: str, cwd: str) -> str:
    if not path:
        return ""
    try:
        return os.path.relpath(path, cwd) if os.path.isabs(path) else path
    except ValueError:
        return path


def change_from_tool_input(tool: str, tool_input: dict[str, Any], cwd: str) -> Change:
    ti = tool_input or {}
    if tool == "Bash":
        cmd = str(ti.get("command", ""))
        return Change(tool=tool, text=cmd, command=cmd)
    path = _rel(str(ti.get("file_path") or ti.get("notebook_path") or ""), cwd)
    if tool == "Write":
        abs_path = Path(cwd) / path if path and not os.path.isabs(path) else Path(path or "")
        return Change(tool=tool, path=path, text=str(ti.get("content", "")), is_new_file=bool(path) and not abs_path.exists())
    if tool == "Edit":
        return Change(tool=tool, path=path, text=str(ti.get("new_string", "")))
    if tool == "MultiEdit":
        edits = ti.get("edits") or []
        return Change(tool=tool, path=path, text="\n".join(str(e.get("new_string", "")) for e in edits if isinstance(e, dict)))
    if tool == "NotebookEdit":
        return Change(tool=tool, path=path, text=str(ti.get("new_source", "")))
    return Change(tool=tool, path=path, text="")


def _match_glob(path: str, glob: str) -> bool:
    return fnmatch.fnmatch(path, glob) or fnmatch.fnmatch("./" + path, glob)


def entry_in_scope(entry: Entry, change: Change) -> bool:
    if entry.scope.tools and change.tool not in entry.scope.tools:
        return False
    if entry.scope.paths:
        targets = [change.path] if change.path else []
        if change.tool == "Bash":
            targets = classify(change.command).paths
        if not targets:
            return False
        return any(_match_glob(t, g) for t in targets for g in entry.scope.paths)
    return True


def evaluate_falsifiers(entry: Entry, change: Change, cwd: str, history: list[dict[str, Any]] | None = None) -> list[Trip]:
    trips: list[Trip] = []
    history = history or []
    for f in entry.falsifiers:
        kind = f.get("kind")
        try:
            if kind == "diff_regex_forbidden" and change.text and re.search(f["pattern"], change.text, re.MULTILINE):
                trips.append(Trip(entry.id, kind, f"pattern {f['pattern']!r} found in change"))
            elif kind == "diff_regex_required" and change.tool == "Write" and change.is_new_file \
                    and not re.search(f["pattern"], change.text, re.MULTILINE):
                trips.append(Trip(entry.id, kind, f"required pattern {f['pattern']!r} missing from new file"))
            elif kind == "path_glob_forbidden":
                paths = [change.path] if change.path else (classify(change.command).paths if change.tool == "Bash" else [])
                for p in paths:
                    if _match_glob(p, f["glob"]):
                        trips.append(Trip(entry.id, kind, f"path {p} matches forbidden glob {f['glob']}"))
            elif kind == "path_glob_required_with" and change.tool == "Write" and change.is_new_file and change.path \
                    and _match_glob(change.path, f["glob"]):
                companion = f["requires"].replace("{stem}", Path(change.path).stem)
                if not (Path(cwd) / companion).exists():
                    trips.append(Trip(entry.id, kind, f"new file {change.path} requires {companion}, which does not exist"))
            elif kind == "command_regex_forbidden" and change.tool == "Bash" and re.search(f["pattern"], change.command):
                trips.append(Trip(entry.id, kind, f"command matches forbidden pattern {f['pattern']!r}"))
            elif kind == "command_requires_prior" and change.tool == "Bash" and re.search(f["pattern"], change.command):
                window = int(f.get("window", 20))
                recent = [h for h in history[-window:] if h.get("tool") == "Bash"]
                if not any(re.search(f["prior_pattern"], str(h.get("command", ""))) for h in recent):
                    trips.append(Trip(entry.id, kind, f"command matches {f['pattern']!r} but nothing matching "
                                                      f"{f['prior_pattern']!r} ran in the last {window} tool calls"))
            # shell_check is Tier 3 only (slow); never evaluated here.
        except (re.error, KeyError) as e:
            trips.append(Trip(entry.id, "falsifier_error", f"{kind}: {e}"))
    return trips


def satisfied_process_falsifier(entry: Entry, change: Change, history: list[dict[str, Any]] | None = None) -> bool:
    """True when a `command_requires_prior` falsifier applies to this command and its prior step is in the history.
    That is the entry's own machine-checkable definition of compliance, so the decision model is not asked
    (it sees only the command, not the history, and would score `git commit` against "run tests before every
    commit" as a conflict even though the tests just ran)."""
    history = history or []
    for f in entry.falsifiers:
        if f.get("kind") != "command_requires_prior" or change.tool != "Bash":
            continue
        try:
            if not re.search(f["pattern"], change.command):
                continue
            window = int(f.get("window", 20))
            recent = [h for h in history[-window:] if h.get("tool") == "Bash"]
            if any(re.search(f["prior_pattern"], str(h.get("command", ""))) for h in recent):
                return True
        except (re.error, KeyError):
            continue
    return False


def structural_signals(change: Change, history: list[dict[str, Any]], cycle_window: int = 10,
                       stagnation_run: int = 7) -> list[dict[str, Any]]:
    """LivePlan-style drift signals from the session's own tool history. Advisory only."""
    out: list[dict[str, Any]] = []
    fp = change.fingerprint()
    recent = history[-cycle_window:]
    repeats = sum(1 for h in recent if h.get("fingerprint") == fp)
    if repeats:
        out.append({"kind": "repeat", "detail": f"identical {change.tool} call seen {repeats}x in the last {len(recent)} tool calls"})
    if change.path:
        run = 0
        for h in reversed(history):
            if h.get("tool") == change.tool and h.get("path") == change.path:
                run += 1
            else:
                break
        if run + 1 > stagnation_run:
            out.append({"kind": "stagnation", "detail": f"{run + 1} consecutive {change.tool} calls on {change.path}"})
    return out


def is_first_mutating_write(change: Change, history: list[dict[str, Any]]) -> bool:
    return change.is_mutating and not any(h.get("mutating") for h in history)


def run_tier0(entries: list[Entry], tool: str, tool_input: dict[str, Any], cwd: str,
              history: list[dict[str, Any]] | None = None, cycle_window: int = 10, stagnation_run: int = 7) -> GateResult:
    history = history or []
    change = change_from_tool_input(tool, tool_input, cwd)
    result = GateResult(change=change)
    if tool == "Bash":
        result.classification = classify(change.command)
    for e in entries:
        if e.status != "active" or not entry_in_scope(e, change):
            continue
        result.scoped_entries.append(e.id)
        trips = evaluate_falsifiers(e, change, cwd, history)
        result.trips.extend(trips)
        if not trips and satisfied_process_falsifier(e, change, history):
            result.cleared.append(e.id)
    result.structural = structural_signals(change, history, cycle_window, stagnation_run)
    result.first_mutating_write = is_first_mutating_write(change, history)
    result.tier_reached = 0
    return result


def format_citation(entries_by_id: dict[str, Entry], trips: list[Trip], next_step: str = "") -> str:
    lines = ["Epistemic monitor: this change conflicts with recorded decisions."]
    for t in trips:
        e = entries_by_id.get(t.entry_id)
        if e is None:
            continue
        prov = f"{e.provenance.source} decision" + (", confirmed by user" if e.provenance.confirmed_by_user else "")
        lines.append(f"- {e.id} ({prov}): \"{e.statement}\" — {t.detail}")
    if next_step:
        lines.append(f"Suggested next step: {next_step}")
    lines.append("Either revise the change, or supersede explicitly by replying with "
                 "`SUPERSEDE <id>: <reason>`.")
    return "\n".join(lines)


# ---------------------------------------------------------------- pipeline
class GatePipeline:
    def __init__(self, store, cfg, dm=None, llm=None, embedder=None, calibration=None) -> None:
        self.store, self.cfg, self.dm, self.llm, self.embedder, self.calibration = store, cfg, dm, llm, embedder, calibration
        self.g = cfg.section("gate")

    # ---- helpers
    def _tau(self, entry: Entry) -> float:
        default = float(self.g.get("tau_by_type", {}).get(entry.type, self.g.get("tau_default", 0.75)))
        return self.calibration.tau(entry.type, default) if self.calibration is not None else default

    def _state_for(self, entry: Entry, change: Change) -> dict[str, Any]:
        max_chars = int(self.g.get("max_diff_chars", 120000))
        planned: dict[str, Any] = {"tool": change.tool}
        if change.path:
            planned["file"] = change.path
        if change.tool == "Bash":
            planned["command"] = change.command[:max_chars]
        else:
            planned["diff"] = change.text[:max_chars]
            if change.is_new_file:
                planned["new_file"] = True
        return {"decision": {"id": entry.id, "type": entry.type, "statement": entry.statement,
                             "rationale": entry.rationale, "provenance": entry.provenance.source},
                "planned_change": planned}

    # ---- tier 1
    def retrieve(self, entries: list[Entry], change: Change, exclude: set[str]) -> list[Entry]:
        by_id = {e.id: e for e in entries}
        ordered: list[str] = [e.id for e in entries if entry_in_scope(e, change) and e.scope.paths and e.id not in exclude]
        text = (change.text or change.command)[:2000]
        if text.strip():
            try:
                for e, _score in self.store.search_keyword(text, top_k=int(self.g.get("candidates_max", 6))):
                    if e.id in by_id and e.id not in exclude and e.id not in ordered:
                        ordered.append(e.id)
            except Exception:  # noqa: BLE001
                pass
        # Embedding retrieval costs a network round trip; use it only when scope + keywords found little.
        if self.embedder is not None and text.strip() and len(ordered) < 2:
            try:
                vec = self.embedder.embed([text[:4000]])[0]
                if vec:
                    for e, score in self.store.search_vector(vec, top_k=int(self.g.get("candidates_max", 6))):
                        if score >= 0.25 and e.id in by_id and e.id not in exclude and e.id not in ordered:
                            ordered.append(e.id)
            except Exception:  # noqa: BLE001
                pass
        # global-scope entries (no paths) in scope by tool are candidates too, lowest priority
        for e in entries:
            if e.id not in exclude and e.id not in ordered and not e.scope.paths and entry_in_scope(e, change):
                ordered.append(e.id)
        return [by_id[i] for i in ordered[: int(self.g.get("candidates_max", 6))]]

    # ---- tier 2
    def _score_one(self, entry: Entry, change: Change) -> dict[str, Any]:
        state = self._state_for(entry, change)
        t0 = time.monotonic()
        severity: float | None = None
        if hasattr(self.dm, "noul_and_score"):
            r, sv = self.dm.noul_and_score(CONFLICT_INSTRUCTIONS, CONFLICT_CRITERIA, SEVERITY_INSTRUCTIONS, SEVERITY_LEVELS, state)
            severity = round(sv.expected, 2)
        else:
            r = self.dm.noul(CONFLICT_INSTRUCTIONS, state, CONFLICT_CRITERIA)
        out: dict[str, Any] = {"p_conflict": round(r.p_true, 3), "samples": [round(r.p_true, 3)], "tau": self._tau(entry)}
        tau = out["tau"]
        if abs(r.p_true - tau) <= float(self.g.get("resample_band", 0.1)):
            for q in CONFLICT_PARAPHRASES[: int(self.g.get("resample_n", 2))]:
                try:
                    out["samples"].append(round(self.dm.noul(q, state, CONFLICT_CRITERIA).p_true, 3))
                except Exception:  # noqa: BLE001
                    break
            out["p_conflict"] = max(out["samples"])  # max aggregation (2605.26047)
            out["resampled"] = True
        if severity is None and out["p_conflict"] >= tau:
            # severity only matters for flagged candidates; one extra call there
            try:
                severity = round(self.dm.score(SEVERITY_INSTRUCTIONS, state, SEVERITY_LEVELS).expected, 2)
            except Exception:  # noqa: BLE001
                severity = None
        out["severity"] = severity
        out["ms"] = int((time.monotonic() - t0) * 1000)
        return out

    def score(self, candidates: list[Entry], change: Change) -> dict[str, dict[str, Any]]:
        scores: dict[str, dict[str, Any]] = {}
        if not candidates or self.dm is None:
            return scores
        budget = float(self.g.get("tier2_time_budget_s", 1.5))
        pool = ThreadPoolExecutor(max_workers=min(8, len(candidates)))
        futures = {pool.submit(self._score_one, e, change): e for e in candidates}
        deadline = time.monotonic() + budget
        try:
            for f in as_completed(futures, timeout=max(0.1, deadline - time.monotonic())):
                e = futures[f]
                try:
                    scores[e.id] = f.result()
                except Exception as ex:  # noqa: BLE001
                    scores[e.id] = {"error": str(ex)[:200]}
        except Exception:  # noqa: BLE001 - timeout: remaining candidates are unscored
            for f, e in futures.items():
                if e.id not in scores:
                    scores[e.id] = {"timeout": True}
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return scores

    # ---- tier 3
    def advise(self, entries: list[Entry], change: Change, trajectory_slice: str, last_advice: str, mode: str,
               history: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
        if self.llm is None or not entries:
            return None
        recent_calls = [{"tool": h.get("tool"), "path": h.get("path") or None, "command": (h.get("command") or None)}
                        for h in (history or [])[-12:]]
        payload = {
            "mode": mode,
            "entries": [{"id": e.id, "type": e.type, "statement": e.statement, "rationale": e.rationale,
                         "provenance": e.provenance.source, "confirmed_by_user": e.provenance.confirmed_by_user} for e in entries],
            "planned_change": self._state_for(entries[0], change)["planned_change"],
            "recent_tool_calls_this_session": [{k: v for k, v in c.items() if v} for c in recent_calls],
            "trajectory_since_last_advice": trajectory_slice[-8000:],
            "last_advice": last_advice[-1000:],
        }
        t0 = time.monotonic()
        try:
            data, comp = self.llm.complete_json(ADVISOR_SYSTEM, json.dumps(payload, ensure_ascii=False), max_tokens=1000)
        except Exception as ex:  # noqa: BLE001
            return {"error": str(ex)[:200], "ms": int((time.monotonic() - t0) * 1000)}
        verdict = str(data.get("verdict", "")).lower()
        if verdict not in ("consistent", "conflict", "supersede_proposed"):
            verdict = "consistent"
        ids = [i for i in (data.get("entry_ids") or []) if isinstance(i, str) and any(i == e.id for e in entries)]
        return {"verdict": verdict, "entry_ids": ids, "next_step": str(data.get("next_step", ""))[:400],
                "reason": str(data.get("reason", ""))[:600], "model": comp.model, "ms": comp.latency_ms}

    # ---- full pipeline
    def evaluate(self, tool: str, tool_input: dict[str, Any], session_id: str, history: list[dict[str, Any]] | None = None,
                 trajectory_slice: str = "", last_advice: str = "") -> GateResult:
        history = history or []
        entries = self.store.active_entries()
        t_start = time.monotonic()
        res = run_tier0(entries, tool, tool_input, str(self.cfg.cwd), history,
                        int(self.g.get("cycle_window", 10)), int(self.g.get("stagnation_run", 7)))
        res.latency_ms["tier0"] = int((time.monotonic() - t_start) * 1000)
        by_id = {e.id: e for e in entries}
        tripped = {t.entry_id for t in res.trips if t.kind != "falsifier_error"}
        conflicts: set[str] = set(tripped)
        if not entries:
            return self._decide(res, by_id, conflicts, session_id, history)

        # Tier 1
        t1 = time.monotonic()
        candidates = self.retrieve(entries, res.change, exclude=tripped | set(res.cleared))
        res.candidates = [e.id for e in candidates]
        res.latency_ms["tier1"] = int((time.monotonic() - t1) * 1000)
        res.tier_reached = 1
        auth_shaped = [e for e in candidates if (e.type in AUTHORIZATION_TYPES or _DESTRUCTIVE.search(res.change.command or ""))
                       and self.g.get("authorization_shaped_to_advisor", True)]

        # Tier 2
        if candidates and self.dm is not None:
            t2 = time.monotonic()
            res.scores = self.score(candidates, res.change)
            res.latency_ms["tier2"] = int((time.monotonic() - t2) * 1000)
            res.tier_reached = 2
            for e in candidates:
                s = res.scores.get(e.id, {})
                p = s.get("p_conflict")
                if isinstance(p, (int, float)) and p >= s.get("tau", self._tau(e)):
                    res.flagged.append(e.id)

        # Tier 3
        to_advise: list[Entry] = []
        cooldown = int(self.g.get("cooldown_steps", 5))
        last_calls = {k: v for k, v in (self.store.get_state(session_id, "advisor_last_call", {}) or {}).items()}
        step = len(history)
        for e in candidates:
            if e.id in res.flagged or e in auth_shaped:
                if step - int(last_calls.get(e.id, -10**6)) >= cooldown:
                    to_advise.append(e)
                else:
                    res.notes.append(f"{e.id}: advisor on cooldown")
        mode = "flagged"
        if not to_advise and res.first_mutating_write and self.g.get("first_write_advisor", True) and self.llm is not None:
            to_advise = entries[: int(self.cfg.get("pinning", "max_entries", 25))]
            mode = "first_mutating_write"
        if to_advise and self.llm is not None:
            t3 = time.monotonic()
            res.advisor = self.advise(to_advise, res.change, trajectory_slice, last_advice, mode, history)
            res.latency_ms["tier3"] = int((time.monotonic() - t3) * 1000)
            res.tier_reached = 3
            if mode == "flagged":   # the first-write briefing is not a conflict review and must not put entries on cooldown
                for e in to_advise:
                    last_calls[e.id] = step
                self.store.set_state(session_id, "advisor_last_call", last_calls)

        # Model-based conflicts
        min_p = float(self.g.get("enforce_uncalibrated_min_p", 0.9))
        calibrated = self.calibration is not None and self.calibration.calibrated
        if res.advisor and "verdict" in res.advisor:
            if res.advisor["verdict"] in ("conflict", "supersede_proposed"):
                conflicts |= set(res.advisor["entry_ids"]) or set(res.flagged)
            # a consistent verdict clears model flags (Tier 0 trips remain)
        else:
            for eid in res.flagged:
                p = res.scores[eid]["p_conflict"]
                if calibrated or p >= min_p:
                    conflicts.add(eid)
                else:
                    res.notes.append(f"{eid}: flagged at p={p} but uncalibrated and below {min_p}; advisory only")
        return self._decide(res, by_id, conflicts, session_id, history)

    def _decide(self, res: GateResult, by_id: dict[str, Entry], conflicts: set[str], session_id: str,
                history: list[dict[str, Any]]) -> GateResult:
        res.conflicts = sorted(conflicts)
        if not conflicts:
            res.decision = "allow"
            if res.advisor and res.advisor.get("verdict") == "consistent" and res.first_mutating_write and res.advisor.get("next_step"):
                res.notes.append("first-write advisor: " + res.advisor["next_step"])
            return res
        trips_for_citation = [t for t in res.trips if t.entry_id in conflicts] + [
            Trip(eid, "model", (res.advisor or {}).get("reason") or f"conflict probability {res.scores.get(eid, {}).get('p_conflict')}")
            for eid in conflicts if not any(t.entry_id == eid for t in res.trips)]
        citation = format_citation(by_id, trips_for_citation, (res.advisor or {}).get("next_step", ""))
        res.reason = citation
        enforce = bool(self.g.get("enforce", True))
        user_backed = any(by_id[e].provenance.source == "user" for e in conflicts if e in by_id)
        tripped = {t.entry_id for t in res.trips}
        hard = user_backed or bool(tripped & conflicts)
        denies = int(self.store.get_state(session_id, "denies", 0) or 0)
        budget = int(self.g.get("deny_budget", 5))
        per_entry = dict(self.store.get_state(session_id, "entry_denies", {}) or {})
        cap = int(self.g.get("max_consecutive_denies", 5))
        capped = [e for e in conflicts if int(per_entry.get(e, 0)) >= cap]
        if enforce and hard and denies < budget and not capped:
            res.decision = "deny"
            self.store.set_state(session_id, "denies", denies + 1)
            for e in conflicts:
                per_entry[e] = int(per_entry.get(e, 0)) + 1
            self.store.set_state(session_id, "entry_denies", per_entry)
        else:
            res.decision = "advisory"
            if capped:
                res.notes.append(f"cap_reached for {capped}; allowing with citation")
            if denies >= budget:
                res.notes.append("deny budget exhausted for this session; advisory only")
            if not enforce:
                res.notes.append("enforcement off")
            if not hard:
                res.notes.append("agent/summary provenance only; advisory")
            for e in conflicts:
                per_entry[e] = 0
            self.store.set_state(session_id, "entry_denies", per_entry)
        return res
