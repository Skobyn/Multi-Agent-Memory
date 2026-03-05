"""Publication-quality benchmark suite for dual-agent memory system.

Sweeps across increasing context window sizes (50-2000 turns), runs 5
independent trials per size with deterministic seeds, and writes one
structured JSON file per run for independent verification.

Test matrix:
    Window sizes: [50, 100, 200, 500, 1000, 2000]
    Runs per size: 5  (different deterministic seeds)
    Total: 30 independent runs -> 30 JSON files

Usage:
    PYTHONPATH=src python tests/benchmark_publication.py
    PYTHONPATH=src python tests/benchmark_publication.py --base-seed 42
    PYTHONPATH=src python tests/benchmark_publication.py --window 200 --runs 1  # quick single run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dual_agent_memory.archive import MemoryArchive
from dual_agent_memory.chunker import Chunker
from dual_agent_memory.config import Config
from dual_agent_memory.embedder import Embedder
from dual_agent_memory.hooks import CompactionHook
from dual_agent_memory.integration import DualAgentMemory
from dual_agent_memory.search import HybridSearch

# ---------------------------------------------------------------------------
# Decision pool (50 unique technology decisions)
# ---------------------------------------------------------------------------

DECISION_POOL = [
    {"topic": "database", "decision": "PostgreSQL", "alt": "MongoDB"},
    {"topic": "authentication", "decision": "JWT with refresh tokens", "alt": "OAuth2"},
    {"topic": "frontend", "decision": "React with TypeScript", "alt": "Vue.js"},
    {"topic": "caching", "decision": "Redis", "alt": "Memcached"},
    {"topic": "deployment", "decision": "Kubernetes on AWS", "alt": "Docker Swarm"},
    {"topic": "API style", "decision": "REST with OpenAPI", "alt": "GraphQL"},
    {"topic": "testing", "decision": "pytest with 80% coverage target", "alt": "unittest"},
    {"topic": "logging", "decision": "structured JSON logs with ELK", "alt": "plain text logs"},
    {"topic": "message queue", "decision": "RabbitMQ", "alt": "Kafka"},
    {"topic": "search engine", "decision": "Elasticsearch", "alt": "Algolia"},
    {"topic": "file storage", "decision": "S3 with CloudFront CDN", "alt": "GCS"},
    {"topic": "monitoring", "decision": "Datadog with PagerDuty", "alt": "Prometheus/Grafana"},
    {"topic": "CI/CD", "decision": "GitHub Actions", "alt": "GitLab CI"},
    {"topic": "ORM", "decision": "SQLAlchemy", "alt": "Django ORM"},
    {"topic": "rate limiting", "decision": "token bucket at 100 req/s", "alt": "sliding window"},
    {"topic": "email service", "decision": "SendGrid", "alt": "AWS SES"},
    {"topic": "feature flags", "decision": "LaunchDarkly", "alt": "custom implementation"},
    {"topic": "DNS provider", "decision": "Cloudflare", "alt": "Route 53"},
    {"topic": "session storage", "decision": "Redis sessions", "alt": "database sessions"},
    {"topic": "image processing", "decision": "Sharp via Node", "alt": "Pillow in Python"},
    {"topic": "PDF generation", "decision": "Puppeteer", "alt": "wkhtmltopdf"},
    {"topic": "websocket", "decision": "Socket.IO", "alt": "native WebSockets"},
    {"topic": "task scheduler", "decision": "Celery with Redis broker", "alt": "APScheduler"},
    {"topic": "secrets management", "decision": "HashiCorp Vault", "alt": "AWS Secrets Manager"},
    {"topic": "container registry", "decision": "ECR", "alt": "Docker Hub"},
    {"topic": "load balancer", "decision": "ALB", "alt": "nginx reverse proxy"},
    {"topic": "error tracking", "decision": "Sentry", "alt": "Rollbar"},
    {"topic": "analytics", "decision": "Mixpanel", "alt": "Google Analytics"},
    {"topic": "A/B testing", "decision": "Optimizely", "alt": "custom split tests"},
    {"topic": "documentation", "decision": "Swagger UI", "alt": "Redoc"},
    {"topic": "code formatter", "decision": "Black", "alt": "autopep8"},
    {"topic": "linter", "decision": "ruff", "alt": "flake8"},
    {"topic": "type checker", "decision": "mypy", "alt": "pyright"},
    {"topic": "database migration", "decision": "Alembic", "alt": "Django migrations"},
    {"topic": "API versioning", "decision": "URL path versioning", "alt": "header versioning"},
    {"topic": "authentication flow", "decision": "OAuth2 PKCE", "alt": "session cookies"},
    {"topic": "state management", "decision": "Redux Toolkit", "alt": "Zustand"},
    {"topic": "CSS framework", "decision": "Tailwind CSS", "alt": "styled-components"},
    {"topic": "bundler", "decision": "Vite", "alt": "webpack"},
    {"topic": "test runner", "decision": "Vitest", "alt": "Jest"},
    {"topic": "mobile framework", "decision": "React Native", "alt": "Flutter"},
    {"topic": "graph database", "decision": "Neo4j", "alt": "Amazon Neptune"},
    {"topic": "time series DB", "decision": "InfluxDB", "alt": "TimescaleDB"},
    {"topic": "API gateway", "decision": "Kong", "alt": "AWS API Gateway"},
    {"topic": "service mesh", "decision": "Istio", "alt": "Linkerd"},
    {"topic": "infrastructure as code", "decision": "Terraform", "alt": "Pulumi"},
    {"topic": "observability", "decision": "OpenTelemetry", "alt": "Jaeger"},
    {"topic": "backup strategy", "decision": "daily S3 snapshots", "alt": "continuous replication"},
    {"topic": "CDN", "decision": "CloudFront", "alt": "Fastly"},
    {"topic": "notification service", "decision": "Firebase Cloud Messaging", "alt": "OneSignal"},
]

# Filler content for non-decision turns
FILLER_USER = [
    "Let's discuss the project timeline and milestones.",
    "We need to review the budget for this quarter.",
    "The team standup went well today.",
    "I've been looking at the competitor analysis.",
    "The product requirements document needs updating.",
    "We should schedule a design review meeting.",
    "The sprint retrospective raised some good points.",
    "Client feedback on the demo was positive.",
    "We need to finalize the API contract with the mobile team.",
    "The performance testing results look promising.",
    "Let's talk about the onboarding flow for new users.",
    "The accessibility audit found a few issues to address.",
    "We should consider internationalization early on.",
    "The data migration plan needs more detail.",
    "Let's review the security checklist before launch.",
    "Have you seen the latest design mockups?",
    "The QA team has some concerns about edge cases.",
    "We should prioritize the backlog items for next sprint.",
    "The stakeholder meeting is scheduled for Friday.",
    "Can you update the architecture diagram?",
]

FILLER_ASSISTANT = [
    "That sounds good. Let me look into the details and follow up.",
    "I agree. We should move forward with the current approach.",
    "Good point. I'll add that to our action items for the next meeting.",
    "The timeline looks feasible. Let's keep monitoring progress weekly.",
    "I've reviewed the document and it looks comprehensive.",
    "Sure, I'll coordinate with the team on the next steps.",
    "The feedback aligns with our goals. Let's iterate on it.",
    "I'll prepare a summary for the stakeholder review.",
    "We should document these findings for future reference.",
    "Let me check with the team and get back to you on that.",
]


# ---------------------------------------------------------------------------
# Conversation builder
# ---------------------------------------------------------------------------

def _select_decisions(window_size: int, rng: random.Random) -> list[dict]:
    """Select and position decisions uniformly across the window."""
    n_decisions = min(50, window_size // 20)
    if n_decisions < 1:
        n_decisions = 1
    selected = rng.sample(DECISION_POOL, k=n_decisions)

    # Distribute turns uniformly across the full window.
    # Decisions are placed at assistant turns (odd indices), so space them out.
    # Reserve first 2 turns and last 2 turns for filler.
    available_assistant_turns = list(range(3, window_size - 2, 2))
    if len(available_assistant_turns) < n_decisions:
        available_assistant_turns = list(range(1, window_size, 2))

    chosen_turns = sorted(rng.sample(available_assistant_turns, k=min(n_decisions, len(available_assistant_turns))))

    result = []
    for i, turn_idx in enumerate(chosen_turns):
        d = dict(selected[i])
        d["turn"] = turn_idx  # assistant turn index
        result.append(d)
    return result


def _build_conversation(
    window_size: int,
    decisions: list[dict],
    rng: random.Random,
) -> list[dict]:
    """Build a conversation with decisions embedded at specified turns."""
    messages = []
    decision_by_turn = {d["turn"]: d for d in decisions}

    for turn in range(window_size):
        if turn % 2 == 0:
            # User turn. If the next turn is a decision turn, ask about it.
            next_turn = turn + 1
            if next_turn in decision_by_turn:
                d = decision_by_turn[next_turn]
                content = (
                    f"What should we use for {d['topic']}? "
                    f"I've been considering {d['decision']} and {d['alt']}."
                )
            else:
                content = rng.choice(FILLER_USER)
            messages.append({"role": "user", "content": content})
        else:
            # Assistant turn
            if turn in decision_by_turn:
                d = decision_by_turn[turn]
                content = (
                    f"After evaluating the options, we should go with "
                    f"{d['decision']} for {d['topic']}. It offers better "
                    f"scalability and community support than {d['alt']}. "
                    f"This is our final decision on {d['topic']}."
                )
            else:
                content = rng.choice(FILLER_ASSISTANT)
            messages.append({"role": "assistant", "content": content})

    return messages


def _build_false_positives(decisions: list[dict], rng: random.Random) -> list[dict]:
    """Build 10 consistent restatements to measure false positive rate."""
    # Pick up to 10 decisions; restate them correctly.
    chosen = rng.sample(decisions, k=min(10, len(decisions)))
    fps = []
    for d in chosen:
        fps.append({
            "topic": d["topic"],
            "statement": (
                f"As we decided, we're using {d['decision']} for {d['topic']}."
            ),
            "is_consistent": True,
        })
    return fps


# ---------------------------------------------------------------------------
# Compaction baselines
# ---------------------------------------------------------------------------

def _simulate_compaction(messages: list[dict], keep_recent: int) -> str:
    """Simulate lossy context compaction: summary + recent turns."""
    summary = (
        "Previous conversation summary: Discussed project architecture and "
        "made several technology decisions across multiple infrastructure "
        "components. The team evaluated options and made final selections. "
        "Also discussed project timeline, budget, team processes, and "
        "client feedback."
    )
    recent = messages[-keep_recent:]
    recent_text = "\n".join(
        f"[{m['role']}]: {m['content']}" for m in recent
    )
    return f"{summary}\n\nRecent messages:\n{recent_text}"


# ---------------------------------------------------------------------------
# System metadata
# ---------------------------------------------------------------------------

def _get_git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=Path(__file__).parent.parent,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _get_system_memory_mb() -> int | None:
    """Get total system memory in MB without psutil."""
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return (pages * page_size) // (1024 * 1024)
    except (ValueError, OSError, AttributeError):
        return None


def _sqlite_vec_available() -> bool:
    try:
        conn = sqlite3.connect(":memory:")
        conn.enable_load_extension(True)
        import sqlite_vec
        sqlite_vec.load(conn)
        conn.close()
        return True
    except Exception:
        return False


def _build_metadata(seed: int, window_size: int, run_index: int, config_hash: str) -> dict:
    return {
        "git_commit": _get_git_commit(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "sqlite_version": sqlite3.sqlite_version,
        "sqlite_vec_available": _sqlite_vec_available(),
        "system_memory_mb": _get_system_memory_mb(),
        "seed": seed,
        "window_size": window_size,
        "run_index": run_index,
        "config_hash": config_hash,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


# ---------------------------------------------------------------------------
# Single run
# ---------------------------------------------------------------------------

def run_single(
    window_size: int,
    run_index: int,
    base_seed: int,
    results_dir: Path,
) -> dict:
    """Execute one benchmark run and write results to JSON."""
    seed = base_seed * 10000 + window_size * 10 + run_index
    rng = random.Random(seed)

    decisions = _select_decisions(window_size, rng)
    messages = _build_conversation(window_size, decisions, rng)
    false_positives = _build_false_positives(decisions, rng)

    # Config hash for reproducibility tracking
    config_str = json.dumps({
        "window_size": window_size,
        "seed": seed,
        "n_decisions": len(decisions),
        "n_false_positives": len(false_positives),
    }, sort_keys=True)
    config_hash = hashlib.sha256(config_str.encode()).hexdigest()[:16]

    metadata = _build_metadata(seed, window_size, run_index, config_hash)

    # --- WITH MEMORY ---
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        memory = DualAgentMemory(db_path, session_id="bench")

        # Archive full conversation
        t0 = time.perf_counter()
        archive_stats = memory.archive_messages(messages, session_id="bench")
        archive_latency = time.perf_counter() - t0

        # Search for each decision
        search_latencies = []
        per_decision_detail = []

        for d in decisions:
            query = f"{d['topic']} decision"
            t0 = time.perf_counter()
            results = memory.search(query, top_k=5)
            search_lat = time.perf_counter() - t0
            search_latencies.append(search_lat)

            recalled = results["total_results"] > 0
            accurate = False
            best_score = 0.0

            if recalled:
                for r in results["results"]:
                    if best_score < r["relevance_score"]:
                        best_score = r["relevance_score"]
                    if d["decision"].lower() in r["content"].lower():
                        accurate = True
                        break

            # Contradiction detection: propose the alternative
            t0 = time.perf_counter()
            contra_result = memory.on_assistant_message(
                f"We decided to use {d['alt']} for {d['topic']}."
            )
            on_assistant_latency = time.perf_counter() - t0

            contradiction_detected = False
            if contra_result.get("interrupt") and contra_result["interrupt"]["severity"] == "hard_interrupt":
                contradiction_detected = True
            elif contra_result.get("confidence") and "contradiction" in contra_result["confidence"].get("flags", []):
                contradiction_detected = True

            per_decision_detail.append({
                "topic": d["topic"],
                "decision": d["decision"],
                "alt": d["alt"],
                "turn": d["turn"],
                "recalled": recalled,
                "accurate": accurate,
                "contradiction_detected": contradiction_detected,
                "search_score": best_score,
                "search_latency_s": search_lat,
                "on_assistant_latency_s": on_assistant_latency,
            })

        # False positive testing
        false_positive_results = []
        for fp in false_positives:
            fp_result = memory.on_assistant_message(fp["statement"])
            flagged = False
            if fp_result.get("interrupt") and fp_result["interrupt"]["severity"] == "hard_interrupt":
                flagged = True
            elif fp_result.get("confidence") and "contradiction" in fp_result["confidence"].get("flags", []):
                flagged = True
            false_positive_results.append({
                "topic": fp["topic"],
                "statement": fp["statement"],
                "is_consistent": fp["is_consistent"],
                "flagged_as_contradiction": flagged,
            })

        # Entity recall
        all_entities = set()
        for d in decisions:
            all_entities.add(d["decision"])
            all_entities.add(d["alt"])
        entities_recalled = 0
        for entity in all_entities:
            r = memory.search(entity, top_k=1)
            if r["total_results"] > 0:
                for res in r["results"]:
                    if entity.lower() in res["content"].lower():
                        entities_recalled += 1
                        break

        # Briefing
        t0 = time.perf_counter()
        briefing = memory.start_session("bench-new")
        briefing_latency = time.perf_counter() - t0
        briefing_text = briefing["briefing"]
        decisions_in_briefing = 0
        for d in decisions:
            if d["decision"].lower() in briefing_text.lower():
                decisions_in_briefing += 1

        # Scaling info
        chunk_count = memory.status()["archive_chunks"]
        db_size_bytes = os.path.getsize(db_path)

        memory.close()

    finally:
        os.unlink(db_path)

    # --- WITHOUT MEMORY (proportional compaction) ---
    keep_proportional = max(10, window_size // 10)
    compacted_proportional = _simulate_compaction(messages, keep_recent=keep_proportional)
    recalled_without_proportional = 0
    for d in decisions:
        if d["decision"].lower() in compacted_proportional.lower():
            recalled_without_proportional += 1

    # --- WITHOUT MEMORY (fixed10 compaction) ---
    compacted_fixed10 = _simulate_compaction(messages, keep_recent=10)
    recalled_without_fixed10 = 0
    for d in decisions:
        if d["decision"].lower() in compacted_fixed10.lower():
            recalled_without_fixed10 += 1

    # Compute aggregate metrics
    n_decisions = len(decisions)
    recalled_count = sum(1 for d in per_decision_detail if d["recalled"])
    accurate_count = sum(1 for d in per_decision_detail if d["accurate"])
    contra_count = sum(1 for d in per_decision_detail if d["contradiction_detected"])
    fp_flagged = sum(1 for fp in false_positive_results if fp["flagged_as_contradiction"])

    on_assistant_latencies = [d["on_assistant_latency_s"] for d in per_decision_detail]

    result = {
        "metadata": metadata,
        "config": {
            "window_size": window_size,
            "n_decisions": n_decisions,
            "n_false_positives": len(false_positives),
            "compaction_keep_proportional": keep_proportional,
            "compaction_keep_fixed10": 10,
        },
        "retention": {
            "with_memory": {
                "decision_recall_rate": recalled_count / n_decisions if n_decisions else 0,
                "decision_accuracy_rate": accurate_count / n_decisions if n_decisions else 0,
                "entity_recall_rate": entities_recalled / len(all_entities) if all_entities else 0,
                "contradiction_detection_rate": contra_count / n_decisions if n_decisions else 0,
                "false_positive_rate": fp_flagged / len(false_positive_results) if false_positive_results else 0,
                "briefing_coverage": decisions_in_briefing / n_decisions if n_decisions else 0,
                "decisions_recalled": recalled_count,
                "decisions_accurate": accurate_count,
                "contradictions_detected": contra_count,
                "false_positives_flagged": fp_flagged,
                "entities_recalled": entities_recalled,
                "entities_total": len(all_entities),
                "decisions_in_briefing": decisions_in_briefing,
            },
            "without_memory_proportional": {
                "decisions_recalled": recalled_without_proportional,
                "recall_rate": recalled_without_proportional / n_decisions if n_decisions else 0,
                "keep_recent": keep_proportional,
            },
            "without_memory_fixed10": {
                "decisions_recalled": recalled_without_fixed10,
                "recall_rate": recalled_without_fixed10 / n_decisions if n_decisions else 0,
                "keep_recent": 10,
            },
        },
        "performance": {
            "archive_latency_s": archive_latency,
            "search_latency_s": {
                "mean": statistics.mean(search_latencies),
                "median": statistics.median(search_latencies),
                "p95": sorted(search_latencies)[int(len(search_latencies) * 0.95)] if search_latencies else 0,
                "p99": sorted(search_latencies)[min(int(len(search_latencies) * 0.99), len(search_latencies) - 1)] if search_latencies else 0,
                "std": statistics.stdev(search_latencies) if len(search_latencies) > 1 else 0,
                "samples": len(search_latencies),
            },
            "on_assistant_latency_s": {
                "mean": statistics.mean(on_assistant_latencies) if on_assistant_latencies else 0,
                "median": statistics.median(on_assistant_latencies) if on_assistant_latencies else 0,
                "p95": sorted(on_assistant_latencies)[int(len(on_assistant_latencies) * 0.95)] if on_assistant_latencies else 0,
                "p99": sorted(on_assistant_latencies)[min(int(len(on_assistant_latencies) * 0.99), len(on_assistant_latencies) - 1)] if on_assistant_latencies else 0,
                "std": statistics.stdev(on_assistant_latencies) if len(on_assistant_latencies) > 1 else 0,
                "samples": len(on_assistant_latencies),
            },
            "briefing_latency_s": briefing_latency,
        },
        "scaling": {
            "chunks_in_archive": chunk_count,
            "db_size_bytes": db_size_bytes,
        },
        "per_decision_detail": per_decision_detail,
        "false_positive_detail": false_positive_results,
    }

    # Write JSON
    timestamp_str = time.strftime("%Y%m%d_%H%M%S")
    filename = f"run_{window_size}_{run_index}_{timestamp_str}.json"
    output_path = results_dir / filename
    with open(output_path, "w") as f:
        json.dump(result, f, indent=2)

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Publication benchmark suite")
    parser.add_argument("--base-seed", type=int, default=42, help="Base random seed")
    parser.add_argument("--window", type=int, nargs="*", default=None,
                        help="Specific window sizes (default: 50 100 200 500 1000 2000)")
    parser.add_argument("--runs", type=int, default=5, help="Runs per window size")
    parser.add_argument("--results-dir", type=str, default=None,
                        help="Output directory (default: tests/results/)")
    args = parser.parse_args()

    window_sizes = args.window or [50, 100, 200, 500, 1000, 2000]
    runs_per_size = args.runs
    base_seed = args.base_seed

    results_dir = Path(args.results_dir) if args.results_dir else Path(__file__).parent / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    total_runs = len(window_sizes) * runs_per_size

    print("=" * 70)
    print("  PUBLICATION BENCHMARK SUITE")
    print("=" * 70)
    print(f"  Window sizes: {window_sizes}")
    print(f"  Runs per size: {runs_per_size}")
    print(f"  Total runs: {total_runs}")
    print(f"  Base seed: {base_seed}")
    print(f"  Output: {results_dir}/")
    print("=" * 70)
    print()

    total_start = time.perf_counter()
    run_count = 0

    for ws in window_sizes:
        for ri in range(runs_per_size):
            run_count += 1
            seed = base_seed * 10000 + ws * 10 + ri
            print(f"  [{run_count}/{total_runs}] window={ws}, run={ri}, seed={seed} ... ", end="", flush=True)

            t0 = time.perf_counter()
            result = run_single(ws, ri, base_seed, results_dir)
            elapsed = time.perf_counter() - t0

            recall = result["retention"]["with_memory"]["decision_recall_rate"]
            accuracy = result["retention"]["with_memory"]["decision_accuracy_rate"]
            contra = result["retention"]["with_memory"]["contradiction_detection_rate"]
            search_ms = result["performance"]["search_latency_s"]["mean"] * 1000

            print(
                f"done in {elapsed:.1f}s  "
                f"recall={recall:.0%} acc={accuracy:.0%} "
                f"contra={contra:.0%} search={search_ms:.1f}ms"
            )

    total_elapsed = time.perf_counter() - total_start

    print()
    print("=" * 70)
    print(f"  Completed {total_runs} runs in {total_elapsed:.1f}s")
    print(f"  Results written to: {results_dir}/")
    print("=" * 70)


if __name__ == "__main__":
    main()
