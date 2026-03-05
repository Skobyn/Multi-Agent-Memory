"""Token cost accounting for the dual-agent memory system.

Tracks token usage across all subsystems (embedding, search, monitoring,
archival, briefings, interrupts) and projects per-session-hour costs at
various model price points.
"""

from __future__ import annotations

import time
from typing import Any

from dual_agent_memory.config import Config


class CostTracker:
    """Tracks token usage and cost estimates for the dual-agent memory system."""

    # Per-token input costs (USD) for supported models.
    _MODEL_COSTS: dict[str, float] = {
        "haiku": 0.00000025,   # $0.25 per 1M input tokens
        "sonnet": 0.000003,    # $3.00 per 1M input tokens
        "opus": 0.000015,      # $15.00 per 1M input tokens
    }

    # Acceptable cost thresholds by use case.
    _THRESHOLDS: dict[str, dict[str, Any]] = {
        "coding": {
            "max_overhead_pct": 30,
            "recommended_model": "haiku",
            "description": "Low-latency coding sessions",
        },
        "research": {
            "max_overhead_pct": 50,
            "recommended_model": "sonnet",
            "description": "Deep research with extensive history",
        },
        "long_project": {
            "max_overhead_pct": 100,
            "recommended_model": "haiku",
            "description": "Multi-day projects where memory is critical",
        },
        "security_audit": {
            "max_overhead_pct": 40,
            "recommended_model": "sonnet",
            "description": "Security reviews requiring consistency",
        },
    }

    def __init__(self, config: Config | None = None) -> None:
        self._config = config or Config()

        # Per-category token counters.
        self.embedding_tokens: int = 0
        self.search_tokens: int = 0
        self.monitor_tokens: int = 0
        self.archive_tokens: int = 0
        self.briefing_tokens: int = 0
        self.interrupt_tokens: int = 0

        # Timing.
        self._session_start: float = time.monotonic()

        # Detailed event log.
        self._events: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(self, category: str, tokens: int, detail: str = "") -> None:
        """Record a token usage event.

        Args:
            category: One of ``"embedding"``, ``"search"``, ``"monitor"``,
                ``"archive"``, ``"briefing"``, or ``"interrupt"``.
            tokens: Number of tokens consumed.
            detail: Optional human-readable context for the event.
        """
        counter_map: dict[str, str] = {
            "embedding": "embedding_tokens",
            "search": "search_tokens",
            "monitor": "monitor_tokens",
            "archive": "archive_tokens",
            "briefing": "briefing_tokens",
            "interrupt": "interrupt_tokens",
        }

        attr = counter_map.get(category)
        if attr is not None:
            setattr(self, attr, getattr(self, attr) + tokens)

        self._events.append(
            {
                "category": category,
                "tokens": tokens,
                "detail": detail,
                "elapsed": time.monotonic() - self._session_start,
            }
        )

    @staticmethod
    def estimate_token_count(text: str) -> int:
        """Estimate the token count of *text* using a simple heuristic.

        Uses a rough chars-to-tokens ratio of 4:1.
        """
        return len(text) // 4

    def get_summary(self) -> dict[str, Any]:
        """Return a comprehensive cost summary for the current session.

        Includes per-category breakdown, extrapolated hourly rate, estimated
        USD cost at different model price points, and overhead metrics.
        """
        total_tokens = (
            self.embedding_tokens
            + self.search_tokens
            + self.monitor_tokens
            + self.archive_tokens
            + self.briefing_tokens
            + self.interrupt_tokens
        )

        elapsed = time.monotonic() - self._session_start
        # Guard against near-zero elapsed time.
        tokens_per_hour = (
            total_tokens / max(elapsed, 0.001) * 3600
        )

        estimated_cost: dict[str, float] = {
            model: round(tokens_per_hour * cost_per_token, 6)
            for model, cost_per_token in self._MODEL_COSTS.items()
        }

        # Overhead calculations.
        non_monitor = max(1, total_tokens - self.monitor_tokens)
        monitor_overhead_pct = round(
            self.monitor_tokens / non_monitor * 100, 2
        )
        archive_base = max(1, self.archive_tokens)
        total_overhead_pct = round(
            total_tokens / archive_base * 100 - 100, 2
        )

        recommendation = self._derive_recommendation(
            total_overhead_pct, tokens_per_hour
        )

        return {
            "total_tokens": total_tokens,
            "breakdown": {
                "embedding": self.embedding_tokens,
                "search": self.search_tokens,
                "monitor": self.monitor_tokens,
                "archive": self.archive_tokens,
                "briefing": self.briefing_tokens,
                "interrupt": self.interrupt_tokens,
            },
            "session_duration_seconds": round(elapsed, 3),
            "tokens_per_hour": round(tokens_per_hour, 1),
            "estimated_cost_per_hour_usd": estimated_cost,
            "overhead_vs_single_agent": {
                "description": (
                    "Estimated multiplier vs single-agent (no memory system)"
                ),
                "monitor_overhead_pct": monitor_overhead_pct,
                "total_overhead_pct": total_overhead_pct,
            },
            "recommendation": recommendation,
            "events_count": len(self._events),
        }

    @staticmethod
    def get_cost_threshold(use_case: str) -> dict[str, Any]:
        """Return acceptable cost thresholds for a given use case.

        Args:
            use_case: One of ``"coding"``, ``"research"``,
                ``"long_project"``, or ``"security_audit"``.

        Returns:
            A dict with ``max_overhead_pct``, ``recommended_model``, and
            ``description``.  Falls back to the ``"coding"`` profile for
            unknown use cases.
        """
        return CostTracker._THRESHOLDS.get(
            use_case, CostTracker._THRESHOLDS["coding"]
        )

    def reset(self) -> None:
        """Reset all counters, events, and the session timer."""
        self.embedding_tokens = 0
        self.search_tokens = 0
        self.monitor_tokens = 0
        self.archive_tokens = 0
        self.briefing_tokens = 0
        self.interrupt_tokens = 0
        self._session_start = time.monotonic()
        self._events.clear()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_recommendation(
        total_overhead_pct: float, tokens_per_hour: float
    ) -> str:
        """Generate a human-readable recommendation based on cost metrics."""
        if total_overhead_pct < 20:
            return (
                "LOW OVERHEAD: Memory system adds minimal cost. "
                "Suitable for all use cases."
            )
        if total_overhead_pct < 50:
            return (
                "MODERATE OVERHEAD: Memory system cost is within acceptable "
                "range for most use cases. Consider using Haiku for the "
                "memory agent to reduce costs."
            )
        if total_overhead_pct < 100:
            return (
                "HIGH OVERHEAD: Memory system adds significant cost. "
                "Recommended for long-running projects where continuity "
                "justifies the expense. Use Haiku for the memory agent."
            )
        return (
            "VERY HIGH OVERHEAD: Memory system cost exceeds the base "
            "conversation cost. Review configuration -- reduce "
            "search_top_k or max_observed_turns, or switch to Haiku."
        )
