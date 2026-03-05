"""Message chunking with metadata extraction for dual-agent memory."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from dual_agent_memory.config import Config, MemoryChunk


class Chunker:
    """Converts raw conversation messages into MemoryChunk instances with extracted metadata."""

    # Patterns for decision detection (case-insensitive).
    _DECISION_PATTERNS: list[re.Pattern[str]] = [
        re.compile(
            r"[^.!?]*\b("
            r"decided|will use|going with|chose|selected|"
            r"agreed|confirmed|let's use|we should"
            r")\b[^.!?]*[.!?]?",
            re.IGNORECASE,
        ),
    ]

    # Pattern for topic extraction: phrases after "about", "regarding", "for".
    # Captures a noun phrase following the preposition (2+ words or one capitalized word).
    _TOPIC_PREPOSITION_PATTERN: re.Pattern[str] = re.compile(
        r"\b(?:about|regarding|for)\s+(?:the\s+|an?\s+)?([A-Za-z][A-Za-z0-9]*(?:[\s-]+[A-Za-z][A-Za-z0-9]*){0,3})",
    )

    # Pattern for capitalized multi-word phrases (potential topics).
    _CAPITALIZED_PHRASE_PATTERN: re.Pattern[str] = re.compile(
        r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b",
    )

    # Pattern for @mentions.
    _MENTION_PATTERN: re.Pattern[str] = re.compile(r"@(\w+)")

    # Pattern for quoted terms.
    _QUOTED_PATTERN: re.Pattern[str] = re.compile(r'"([^"]+)"')

    # Pattern for standalone capitalized words (potential named entities).
    _CAPITALIZED_WORD_PATTERN: re.Pattern[str] = re.compile(r"\b([A-Z][a-z]{2,})\b")

    def __init__(self, config: Config) -> None:
        self._config = config

    def chunk_messages(
        self, messages: list[dict], session_id: str
    ) -> list[MemoryChunk]:
        """Convert a list of conversation messages into MemoryChunks.

        Args:
            messages: List of dicts with "role" and "content" keys.
            session_id: Identifier for the conversation session.

        Returns:
            Ordered list of MemoryChunk instances, one per message.
        """
        chunks: list[MemoryChunk] = []
        for turn_id, msg in enumerate(messages, start=1):
            chunk = self.chunk_single(
                role=msg["role"],
                content=msg["content"],
                session_id=session_id,
                turn_id=turn_id,
            )
            chunks.append(chunk)
        return chunks

    def chunk_single(
        self, role: str, content: str, session_id: str, turn_id: int
    ) -> MemoryChunk:
        """Process a single message into a MemoryChunk with extracted metadata.

        Args:
            role: Message role ("user", "assistant", or "system").
            content: Raw message text.
            session_id: Identifier for the conversation session.
            turn_id: Sequential position in the conversation (1-based).

        Returns:
            A MemoryChunk with populated metadata fields.
        """
        return MemoryChunk(
            session_id=session_id,
            turn_id=turn_id,
            role=role,
            content=content,
            timestamp=datetime.now(timezone.utc).isoformat(),
            topics=self._extract_topics(content),
            entities=self._extract_entities(content),
            decisions=self._extract_decisions(content),
        )

    # ------------------------------------------------------------------
    # Private extraction methods
    # ------------------------------------------------------------------

    def _extract_topics(self, text: str) -> list[str]:
        """Extract key topic phrases from text.

        Strategy:
        - Find phrases after "about", "regarding", "for" followed by capitalized words.
        - Find capitalized multi-word phrases (e.g. "Google Cloud Platform").
        """
        seen: set[str] = set()
        topics: list[str] = []

        # Phrases after prepositions like "about X", "regarding X", "for X".
        for match in self._TOPIC_PREPOSITION_PATTERN.finditer(text):
            phrase = match.group(1).strip()
            lowered = phrase.lower()
            if lowered not in seen:
                seen.add(lowered)
                topics.append(phrase)

        # Capitalized multi-word phrases (Title Case sequences).
        for match in self._CAPITALIZED_PHRASE_PATTERN.finditer(text):
            phrase = match.group(1).strip()
            lowered = phrase.lower()
            if lowered not in seen:
                seen.add(lowered)
                topics.append(phrase)

        return topics

    def _extract_entities(self, text: str) -> list[str]:
        """Extract named entities from text.

        Strategy:
        - Capitalized words that are NOT at the start of a sentence.
        - @mentions.
        - Quoted terms.
        """
        seen: set[str] = set()
        entities: list[str] = []

        # Find sentence boundaries so we can skip sentence-starting words.
        sentence_starters: set[int] = set()
        # Start of text is a sentence start.
        first_word_match = re.match(r"\s*(\w+)", text)
        if first_word_match:
            sentence_starters.add(first_word_match.start(1))
        # After sentence-ending punctuation followed by whitespace.
        for m in re.finditer(r"[.!?]\s+(\w)", text):
            sentence_starters.add(m.start(1))

        # Capitalized words not at sentence start.
        for match in self._CAPITALIZED_WORD_PATTERN.finditer(text):
            word = match.group(1)
            lowered = word.lower()
            # Skip if this word starts a sentence.
            if match.start(1) in sentence_starters:
                continue
            # Skip common English words that are often capitalized in titles.
            if lowered in {"the", "this", "that", "these", "those", "and", "but", "for"}:
                continue
            if lowered not in seen:
                seen.add(lowered)
                entities.append(word)

        # @mentions.
        for match in self._MENTION_PATTERN.finditer(text):
            mention = f"@{match.group(1)}"
            lowered = mention.lower()
            if lowered not in seen:
                seen.add(lowered)
                entities.append(mention)

        # Quoted terms.
        for match in self._QUOTED_PATTERN.finditer(text):
            term = match.group(1).strip()
            lowered = term.lower()
            if term and lowered not in seen:
                seen.add(lowered)
                entities.append(term)

        return entities

    def _extract_decisions(self, text: str) -> list[str]:
        """Extract decision statements from text.

        Looks for sentences containing decision-indicating phrases like
        "decided", "will use", "going with", "chose", "selected", etc.
        """
        seen: set[str] = set()
        decisions: list[str] = []

        for pattern in self._DECISION_PATTERNS:
            for match in pattern.finditer(text):
                sentence = match.group(0).strip()
                # Clean up: remove leading conjunctions/whitespace.
                sentence = re.sub(r"^\s*(?:and|but|so)\s+", "", sentence, flags=re.IGNORECASE)
                sentence = sentence.strip()
                if not sentence:
                    continue
                lowered = sentence.lower()
                if lowered not in seen:
                    seen.add(lowered)
                    decisions.append(sentence)

        return decisions
