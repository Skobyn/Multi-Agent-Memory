"""Confidence scoring system for the epistemic monitor."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from dual_agent_memory.config import Config, ConfidenceSignal, MemoryChunk, SearchResult

if TYPE_CHECKING:
    from dual_agent_memory.contradiction import ContradictionDetector
    from dual_agent_memory.search import HybridSearch


class ConfidenceScorer:
    """Computes per-output confidence by comparing against the memory archive.

    The scorer uses a :class:`HybridSearch` instance for entity/topic lookup
    and the underlying archive for retrieving archived decision chunks.
    """

    # Words to ignore when extracting entities/topics from output.
    _STOP_WORDS: set[str] = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "shall", "can", "need", "must", "i", "you",
        "he", "she", "it", "we", "they", "me", "him", "her", "us", "them",
        "my", "your", "his", "its", "our", "their", "this", "that", "these",
        "those", "what", "which", "who", "whom", "whose", "when", "where",
        "why", "how", "all", "each", "every", "both", "few", "more", "most",
        "other", "some", "such", "no", "not", "only", "own", "same", "so",
        "than", "too", "very", "just", "because", "as", "until", "while",
        "of", "at", "by", "for", "with", "about", "against", "between",
        "through", "during", "before", "after", "above", "below", "to",
        "from", "up", "down", "in", "out", "on", "off", "over", "under",
        "again", "further", "then", "once", "here", "there", "and", "but",
        "or", "if", "nor", "also", "however", "although", "though",
    }

    # Pattern for capitalized words (potential named entities).
    _ENTITY_PATTERN: re.Pattern[str] = re.compile(r"\b([A-Z][a-z]{2,})\b")

    # Pattern for quoted terms.
    _QUOTED_PATTERN: re.Pattern[str] = re.compile(r'"([^"]+)"')

    # Pattern for technical terms (e.g. camelCase, snake_case, ALL_CAPS).
    _TECH_TERM_PATTERN: re.Pattern[str] = re.compile(
        r"\b([a-z]+[A-Z]\w+|[a-z]+_[a-z]+\w*|[A-Z]{2,}[A-Z_]*)\b"
    )

    def __init__(
        self,
        search: HybridSearch,
        contradiction_detector: ContradictionDetector,
        config: Config,
    ) -> None:
        self._search = search
        self._contradiction_detector = contradiction_detector
        self._config = config

    def score(
        self,
        output_text: str,
        current_context_summary: str | None = None,
    ) -> ConfidenceSignal:
        """Compute a confidence score for a main-agent output.

        Args:
            output_text: The assistant output to evaluate.
            current_context_summary: Summary of the current conversation
                context visible to the main agent, used for gap detection.

        Returns:
            A populated ConfidenceSignal.
        """
        # 1. Extract entities and topics from the output.
        entities = self._extract_entities(output_text)
        topics = self._extract_topics(output_text)

        # 2. Check each entity/topic against the archive.
        context_lower = (current_context_summary or "").lower()
        gaps: list[str] = []
        drift_topics: list[str] = []

        for entity in entities:
            # Search archive for this entity via HybridSearch.
            results = self._search.search_by_entity(entity, top_k=3)
            if results:
                # Entity exists in archive -- check if it is in current context.
                if context_lower and entity.lower() not in context_lower:
                    gaps.append(entity)

        for topic in topics:
            results = self._search.search(topic, top_k=3)
            if results:
                # Topic has archived history.
                if context_lower and topic.lower() not in context_lower:
                    drift_topics.append(topic)

        # 4. Run contradiction detection against archived decisions.
        # get_all_decisions lives on the archive, which HybridSearch wraps.
        archived_decisions = self._search._archive.get_all_decisions()
        contradictions = self._contradiction_detector.detect(
            output_text, archived_decisions
        )

        # 5. Compute the final score.
        score = 1.0
        score -= 0.1 * len(gaps)
        score -= 0.2 * len(contradictions)
        score -= 0.05 * len(drift_topics)
        score = max(0.0, min(1.0, score))

        # 6. Build flags.
        flags: list[str] = []
        if gaps:
            flags.append("gap")
        if contradictions:
            flags.append("contradiction")
        if drift_topics:
            flags.append("drift")

        # 7. Build citations from contradiction details.
        citations: list[dict] = []
        for c in contradictions:
            chunk: MemoryChunk = c["archived_chunk"]
            citations.append(
                {
                    "current_statement": c["current_statement"],
                    "archived_statement": c["archived_statement"],
                    "chunk_id": chunk.chunk_id,
                    "session_id": chunk.session_id,
                    "turn_id": chunk.turn_id,
                    "confidence": c["confidence"],
                }
            )

        # 8. Determine recommendation.
        recommendation = self._recommend(score, flags)

        return ConfidenceSignal(
            score=round(score, 3),
            flags=flags,
            citations=citations,
            recommendation=recommendation,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _recommend(self, score: float, flags: list[str]) -> str:
        """Generate a recommendation string based on score and flags."""
        if "contradiction" in flags:
            return (
                "CONTRADICTION DETECTED: Output conflicts with prior archived "
                "decisions. Review citations and reconcile before proceeding."
            )
        if score < self._config.hard_interrupt_threshold:
            return (
                "LOW CONFIDENCE: Significant gaps detected between output and "
                "archived knowledge. Recommend retrieving relevant context."
            )
        if score < self._config.soft_nudge_threshold:
            return (
                "MODERATE CONFIDENCE: Some referenced entities or topics lack "
                "context grounding. Consider verifying against archive."
            )
        return "HIGH CONFIDENCE: Output is well-grounded in available context."

    def _extract_entities(self, text: str) -> list[str]:
        """Extract named entities from text."""
        seen: set[str] = set()
        entities: list[str] = []

        # Sentence starters to skip.
        sentence_starters: set[int] = set()
        first_match = re.match(r"\s*(\w+)", text)
        if first_match:
            sentence_starters.add(first_match.start(1))
        for m in re.finditer(r"[.!?]\s+(\w)", text):
            sentence_starters.add(m.start(1))

        # Capitalized words not at sentence start.
        for match in self._ENTITY_PATTERN.finditer(text):
            word = match.group(1)
            if match.start(1) in sentence_starters:
                continue
            low = word.lower()
            if low in self._STOP_WORDS:
                continue
            if low not in seen:
                seen.add(low)
                entities.append(word)

        # Quoted terms.
        for match in self._QUOTED_PATTERN.finditer(text):
            term = match.group(1).strip()
            low = term.lower()
            if term and low not in seen:
                seen.add(low)
                entities.append(term)

        # Technical terms (camelCase, snake_case, ALL_CAPS).
        for match in self._TECH_TERM_PATTERN.finditer(text):
            term = match.group(1)
            low = term.lower()
            if low not in seen and low not in self._STOP_WORDS:
                seen.add(low)
                entities.append(term)

        return entities

    def _extract_topics(self, text: str) -> list[str]:
        """Extract topic phrases from text."""
        seen: set[str] = set()
        topics: list[str] = []

        # Phrases after prepositions.
        prep_pattern = re.compile(
            r"\b(?:about|regarding|for|on|with)\s+"
            r"([A-Z][A-Za-z0-9]*(?:\s+[A-Za-z0-9]+){0,3})"
        )
        for match in prep_pattern.finditer(text):
            phrase = match.group(1).strip()
            low = phrase.lower()
            if low not in seen:
                seen.add(low)
                topics.append(phrase)

        # Multi-word capitalized phrases.
        cap_pattern = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")
        for match in cap_pattern.finditer(text):
            phrase = match.group(1).strip()
            low = phrase.lower()
            if low not in seen:
                seen.add(low)
                topics.append(phrase)

        return topics
