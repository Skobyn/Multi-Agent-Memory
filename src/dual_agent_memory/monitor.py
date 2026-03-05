"""The MemoryAgent -- epistemic monitor that oversees the main agent."""

from __future__ import annotations

import re
import uuid
from collections import Counter
from typing import Any

from dual_agent_memory.chunker import Chunker
from dual_agent_memory.confidence import ConfidenceScorer
from dual_agent_memory.config import Config, ConfidenceSignal, Interrupt, MemoryChunk
from dual_agent_memory.contradiction import ContradictionDetector
from dual_agent_memory.cost import CostTracker
from dual_agent_memory.search import HybridSearch


class MemoryAgent:
    """Epistemic monitor that observes conversation, evaluates outputs, and
    raises interrupts when the main agent drifts from archived knowledge."""

    def __init__(
        self,
        archive: Any,
        embedder: Any,
        config: Config | None = None,
    ) -> None:
        """Initialize the memory agent.

        Args:
            archive: A MemoryArchive (or compatible object) providing
                ``get_all_decisions``, ``get_all_chunks``, and chunk
                storage methods.
            embedder: An Embedder (or compatible object) with an ``embed``
                method for generating text embeddings.
            config: System configuration.  Defaults are used when *None*.
        """
        self._config = config or Config()
        self._archive = archive
        self._embedder = embedder
        self._chunker = Chunker(self._config)
        self._contradiction_detector = ContradictionDetector(self._config)

        # Create a HybridSearch that wraps the archive for entity/topic lookup.
        self._search = HybridSearch(
            archive=archive,
            embedder=embedder,
            config=self._config,
        )

        self._scorer = ConfidenceScorer(
            search=self._search,
            contradiction_detector=self._contradiction_detector,
            config=self._config,
        )

        # Cost tracking for the dual-agent overhead.
        self._cost_tracker = CostTracker(self._config)

        # Full observed conversation (both user and assistant turns).
        self._observed_turns: list[dict] = []

        # Rolling summary of compacted older turns.
        self._rolling_summary: str = ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def cost_tracker(self) -> CostTracker:
        """The :class:`CostTracker` recording token usage for this agent."""
        return self._cost_tracker

    def observe(self, role: str, content: str) -> None:
        """Record a conversation turn.

        Called for every message in the conversation (user, assistant, or
        system).

        Args:
            role: The message role (``"user"``, ``"assistant"``, ``"system"``).
            content: The raw message text.
        """
        self._observed_turns.append({"role": role, "content": content})
        self._cost_tracker.record(
            "monitor",
            CostTracker.estimate_token_count(content),
            f"observe:{role}",
        )
        self._compact_observations()

    def evaluate(self, assistant_output: str) -> ConfidenceSignal:
        """Evaluate the latest assistant output against archived knowledge.

        Builds a context summary from the most recent observed turns and
        delegates to :class:`ConfidenceScorer`.

        Args:
            assistant_output: The assistant message to evaluate.

        Returns:
            A :class:`ConfidenceSignal` with score, flags, citations, and
            recommendation.
        """
        context_summary = self._build_context_summary()
        self._cost_tracker.record(
            "search",
            CostTracker.estimate_token_count(assistant_output + context_summary),
            "evaluate:score",
        )
        return self._scorer.score(assistant_output, context_summary)

    def check_interrupt(self, signal: ConfidenceSignal) -> Interrupt | None:
        """Decide whether to raise an interrupt based on a confidence signal.

        Rules:
        - ``"contradiction"`` in flags -> hard interrupt (regardless of score).
        - score < ``hard_interrupt_threshold`` -> hard interrupt with citations.
        - score < ``soft_nudge_threshold`` -> soft nudge.
        - Otherwise -> no interrupt.

        Args:
            signal: The evaluated confidence signal.

        Returns:
            An :class:`Interrupt` if warranted, or *None*.
        """
        contradicting_chunks: list[MemoryChunk] = []
        for citation in signal.citations:
            # Attempt to find the original chunk from the archive.
            chunk_id = citation.get("chunk_id")
            if chunk_id:
                chunk = self._archive.get_chunk(chunk_id)
                if chunk:
                    contradicting_chunks.append(chunk)

        # Rule: contradiction flag always triggers hard interrupt.
        if "contradiction" in signal.flags:
            message_parts = ["CONTRADICTION: Output conflicts with archived decisions."]
            for citation in signal.citations:
                message_parts.append(
                    f"  - Current: \"{citation.get('current_statement', '')}\""
                    f" vs Archived: \"{citation.get('archived_statement', '')}\""
                    f" (Session {citation.get('session_id', '?')},"
                    f" Turn {citation.get('turn_id', '?')})"
                )
            interrupt_msg = "\n".join(message_parts)
            self._cost_tracker.record(
                "interrupt",
                CostTracker.estimate_token_count(interrupt_msg),
                "check_interrupt:contradiction",
            )
            return Interrupt(
                severity="hard_interrupt",
                message=interrupt_msg,
                contradicting_chunks=contradicting_chunks,
                suggested_query=self._suggest_query(signal),
            )

        # Rule: very low score triggers hard interrupt.
        if signal.score < self._config.hard_interrupt_threshold:
            interrupt_msg = (
                f"LOW CONFIDENCE ({signal.score:.2f}): Significant knowledge "
                f"gaps detected. Flags: {', '.join(signal.flags)}. "
                f"{signal.recommendation}"
            )
            self._cost_tracker.record(
                "interrupt",
                CostTracker.estimate_token_count(interrupt_msg),
                "check_interrupt:low_confidence",
            )
            return Interrupt(
                severity="hard_interrupt",
                message=interrupt_msg,
                contradicting_chunks=contradicting_chunks,
                suggested_query=self._suggest_query(signal),
            )

        # Rule: moderate score triggers soft nudge.
        if signal.score < self._config.soft_nudge_threshold:
            interrupt_msg = (
                f"MODERATE CONFIDENCE ({signal.score:.2f}): "
                f"Flags: {', '.join(signal.flags)}. "
                f"{signal.recommendation}"
            )
            self._cost_tracker.record(
                "interrupt",
                CostTracker.estimate_token_count(interrupt_msg),
                "check_interrupt:soft_nudge",
            )
            return Interrupt(
                severity="soft_nudge",
                message=interrupt_msg,
                contradicting_chunks=contradicting_chunks,
                suggested_query=self._suggest_query(signal),
            )

        return None

    def generate_briefing(self, session_id: str) -> str:
        """Generate a session-boundary briefing from the archive.

        Queries the archive for recent decisions, open topics, and entity
        state, then formats a structured briefing string.

        Args:
            session_id: The current session identifier (for labelling).

        Returns:
            A formatted briefing string.
        """
        # Fetch recent decisions (last 20 chunks with decisions).
        all_decision_chunks = self._archive.get_all_decisions()
        recent_decision_chunks = all_decision_chunks[-20:]

        # Collect unique topics and entities across all archived chunks.
        all_topics: dict[str, str] = {}  # topic -> last session_id
        all_entities: dict[str, str] = {}  # entity -> last content snippet

        for chunk in all_decision_chunks:
            for topic in chunk.topics:
                all_topics[topic] = chunk.session_id
            for entity in chunk.entities:
                # Store the most recent content mentioning this entity.
                all_entities[entity] = (
                    f"Last seen in session {chunk.session_id}, "
                    f"turn {chunk.turn_id}"
                )

        # Build the briefing.
        lines: list[str] = []
        lines.append("=== Session Briefing ===")

        # Prior decisions.
        lines.append("Prior Decisions:")
        if recent_decision_chunks:
            for chunk in recent_decision_chunks:
                for decision in chunk.decisions:
                    lines.append(
                        f"- {decision} "
                        f"(Session {chunk.session_id}, Turn {chunk.turn_id})"
                    )
        else:
            lines.append("- (none)")

        # Active topics.
        lines.append(f"Active Topics: {list(all_topics.keys()) if all_topics else '(none)'}")

        # Key entities.
        lines.append("Key Entities:")
        if all_entities:
            for entity, state in all_entities.items():
                lines.append(f"- {entity}: {state}")
        else:
            lines.append("- (none)")

        lines.append("===")

        briefing = "\n".join(lines)
        self._cost_tracker.record(
            "briefing",
            CostTracker.estimate_token_count(briefing),
            f"generate_briefing:{session_id}",
        )
        return briefing

    def process_turn(
        self, role: str, content: str
    ) -> tuple[ConfidenceSignal | None, Interrupt | None]:
        """Convenience method: observe + evaluate (if assistant) + check_interrupt.

        For user turns, just observes and returns ``(None, None)``.
        For assistant turns, observes, evaluates, and checks for interrupts.

        Args:
            role: The message role.
            content: The raw message text.

        Returns:
            A tuple of ``(ConfidenceSignal | None, Interrupt | None)``.
        """
        self.observe(role, content)

        if role != "assistant":
            return None, None

        signal = self.evaluate(content)
        interrupt = self.check_interrupt(signal)
        return signal, interrupt

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_context_summary(self) -> str:
        """Build a context summary from the most recent observed turns.

        Takes the last 10 turns and concatenates them into a single string
        for use in gap/drift detection.  When a rolling summary exists from
        prior compaction rounds it is prepended so the scorer retains
        awareness of earlier conversation context.
        """
        recent = self._observed_turns[-10:]
        parts: list[str] = []
        if self._rolling_summary:
            parts.append(f"[compacted history]\n{self._rolling_summary}")
        for turn in recent:
            parts.append(f"[{turn['role']}]: {turn['content']}")
        return "\n".join(parts)

    def _compact_observations(self) -> None:
        if len(self._observed_turns) <= self._config.max_observed_turns:
            return

        # Take the oldest half for compaction.
        midpoint = len(self._observed_turns) // 2
        old_turns = self._observed_turns[:midpoint]

        # --- Build a structured summary of the compacted turns ---
        role_counts: Counter[str] = Counter()
        all_decisions: list[str] = []
        all_entities: set[str] = set()
        all_topics: set[str] = set()

        for turn in old_turns:
            role_counts[turn["role"]] += 1
            content = turn["content"]

            # Extract decisions from assistant turns using the Chunker's
            # decision patterns (same regex the rest of the system uses).
            if turn["role"] == "assistant":
                for pattern in Chunker._DECISION_PATTERNS:
                    for match in pattern.finditer(content):
                        sentence = match.group(0).strip()
                        sentence = re.sub(
                            r"^\s*(?:and|but|so)\s+", "", sentence, flags=re.IGNORECASE
                        ).strip()
                        if sentence and sentence not in all_decisions:
                            all_decisions.append(sentence)

            # Extract entities and topics via the chunker's helpers.
            chunk = self._chunker.chunk_single(
                role=turn["role"],
                content=content,
                session_id="compaction",
                turn_id=0,
            )
            all_entities.update(chunk.entities)
            all_topics.update(chunk.topics)

        # Assemble the summary block.
        summary_lines: list[str] = []
        role_parts = ", ".join(f"{r}: {c}" for r, c in sorted(role_counts.items()))
        summary_lines.append(f"Compacted {len(old_turns)} turns ({role_parts}).")

        if all_decisions:
            summary_lines.append("Decisions: " + "; ".join(all_decisions))
        if all_entities:
            summary_lines.append("Entities: " + ", ".join(sorted(all_entities)))
        if all_topics:
            summary_lines.append("Topics: " + ", ".join(sorted(all_topics)))

        summary_block = "\n".join(summary_lines)

        # Append to the rolling summary (accumulates across compaction rounds).
        if self._rolling_summary:
            self._rolling_summary += "\n---\n" + summary_block
        else:
            self._rolling_summary = summary_block

        # --- Archive the compacted turns so they remain searchable ---
        session_id = f"compaction-{uuid.uuid4().hex[:8]}"
        chunks = self._chunker.chunk_messages(old_turns, session_id)
        for chunk in chunks:
            embedding = self._embedder.embed(chunk.content)
            chunk.embedding = embedding
        self._archive.store_chunks(chunks)

        # Drop the compacted turns from the live observation list.
        self._observed_turns = self._observed_turns[midpoint:]

    @staticmethod
    def _suggest_query(signal: ConfidenceSignal) -> str:
        """Suggest a retrieval query based on the confidence signal's issues."""
        suggestions: list[str] = []

        if signal.citations:
            # Use the first contradiction's topic as a search query.
            first = signal.citations[0]
            stmt = first.get("current_statement", "")
            if stmt:
                # Extract key words from the statement.
                words = [
                    w for w in stmt.split()
                    if len(w) > 3 and w.lower() not in {
                        "will", "have", "that", "this", "with", "from",
                        "should", "would", "could",
                    }
                ]
                suggestions.extend(words[:5])

        if "gap" in signal.flags:
            suggestions.append("context gaps")

        if "drift" in signal.flags:
            suggestions.append("topic history")

        return " ".join(suggestions) if suggestions else "recent decisions"
