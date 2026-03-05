"""Contradiction detection between current output and archived decisions."""

from __future__ import annotations

import re

from dual_agent_memory.config import Config, MemoryChunk


# Pairs of antonyms / opposite action verbs used as conflict signals.
_ANTONYM_PAIRS: list[tuple[str, str]] = [
    ("add", "remove"),
    ("enable", "disable"),
    ("use", "avoid"),
    ("include", "exclude"),
    ("allow", "deny"),
    ("start", "stop"),
    ("create", "delete"),
    ("accept", "reject"),
    ("open", "close"),
    ("show", "hide"),
    ("increase", "decrease"),
    ("upgrade", "downgrade"),
    ("connect", "disconnect"),
    ("install", "uninstall"),
    ("activate", "deactivate"),
    ("encrypt", "decrypt"),
    ("require", "optional"),
    ("sync", "async"),
    ("public", "private"),
    ("mutable", "immutable"),
]

# Build a fast lookup: word -> set of its antonyms.
_ANTONYM_MAP: dict[str, set[str]] = {}
for _a, _b in _ANTONYM_PAIRS:
    _ANTONYM_MAP.setdefault(_a, set()).add(_b)
    _ANTONYM_MAP.setdefault(_b, set()).add(_a)

# Decision-indicating phrases (mirrors the chunker patterns).
_DECISION_PATTERN: re.Pattern[str] = re.compile(
    r"[^.!?\n]*\b("
    r"decided|will use|going with|chose|selected|"
    r"agreed|confirmed|let's use|we should|"
    r"switching to|moving to|opting for|prefer|"
    r"must use|need to use|plan to use|want to use"
    r")\b[^.!?\n]*[.!?\n]?",
    re.IGNORECASE,
)

# Imperative sentence pattern (e.g. "Use Redis for caching.").
_IMPERATIVE_PATTERN: re.Pattern[str] = re.compile(
    r"(?:^|\n)\s*([A-Z][a-z]+(?:\s+\S+){1,10}[.!])",
)

# Negation words.
_NEGATION_WORDS: set[str] = {
    "not", "no", "never", "don't", "doesn't", "shouldn't", "won't",
    "cannot", "can't", "isn't", "aren't", "wasn't", "weren't",
    "without", "neither", "nor", "avoid", "stop", "remove",
}


class ContradictionDetector:
    """Detects contradictions between current output and archived decisions."""

    def __init__(self, config: Config) -> None:
        self._config = config

    def detect(
        self,
        current_text: str,
        archived_decisions: list[MemoryChunk],
    ) -> list[dict]:
        """Detect contradictions between current text and archived decisions.

        Args:
            current_text: The current assistant output text.
            archived_decisions: List of MemoryChunks that contain prior decisions.

        Returns:
            List of contradiction dicts, each with keys:
            ``current_statement``, ``archived_statement``, ``archived_chunk``,
            and ``confidence``.
        """
        current_statements = self._extract_statements(current_text)
        if not current_statements:
            return []

        contradictions: list[dict] = []

        for stmt in current_statements:
            for chunk in archived_decisions:
                for archived_decision in chunk.decisions:
                    is_conflict, confidence = self._statements_conflict(
                        stmt, archived_decision
                    )
                    if is_conflict:
                        contradictions.append(
                            {
                                "current_statement": stmt,
                                "archived_statement": archived_decision,
                                "archived_chunk": chunk,
                                "confidence": confidence,
                            }
                        )

        return contradictions

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _extract_statements(self, text: str) -> list[str]:
        """Extract actionable / decision-like statements from text.

        Combines decision-pattern matches with imperative sentences.
        """
        seen: set[str] = set()
        statements: list[str] = []

        # Decision-pattern matches.
        for match in _DECISION_PATTERN.finditer(text):
            stmt = match.group(0).strip()
            stmt = re.sub(
                r"^\s*(?:and|but|so)\s+", "", stmt, flags=re.IGNORECASE
            ).strip()
            if stmt and stmt.lower() not in seen:
                seen.add(stmt.lower())
                statements.append(stmt)

        # Imperative sentences.
        for match in _IMPERATIVE_PATTERN.finditer(text):
            stmt = match.group(1).strip()
            if stmt and stmt.lower() not in seen:
                seen.add(stmt.lower())
                statements.append(stmt)

        return statements

    def _statements_conflict(
        self, stmt1: str, stmt2: str
    ) -> tuple[bool, float]:
        """Determine whether two statements conflict.

        Returns:
            (is_conflict, confidence) where confidence is 0.0-1.0.
        """
        words1 = self._tokenize(stmt1)
        words2 = self._tokenize(stmt2)

        if not words1 or not words2:
            return False, 0.0

        # Compute keyword overlap to determine if statements are about the
        # same topic.  We use Jaccard-like overlap.
        set1 = set(words1)
        set2 = set(words2)
        overlap = set1 & set2
        union = set1 | set2

        if not union:
            return False, 0.0

        overlap_ratio = len(overlap) / len(union)

        # Statements need some topical overlap to be comparable.
        if overlap_ratio < 0.1:
            return False, 0.0

        # --- Check for negation asymmetry ---
        neg1 = self._has_negation(words1)
        neg2 = self._has_negation(words2)
        negation_conflict = neg1 != neg2

        # --- Check for antonym pairs ---
        antonym_score = self._antonym_score(words1, words2)

        # --- Check for conflicting choices ---
        # e.g. "use A" vs "use B" -- same verb, different object.
        choice_conflict_score = self._choice_conflict_score(stmt1, stmt2)

        # Combine conflict signals.
        conflict_signal = 0.0
        if negation_conflict:
            conflict_signal += 0.5
        conflict_signal += antonym_score * 0.4
        conflict_signal += choice_conflict_score * 0.4

        # Scale by overlap -- higher overlap means more confidence.
        confidence = min(1.0, conflict_signal * (0.5 + overlap_ratio))

        is_conflict = confidence >= 0.3
        return is_conflict, round(confidence, 3)

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """Lowercase tokenize, stripping punctuation."""
        return re.findall(r"[a-z0-9]+", text.lower())

    @staticmethod
    def _has_negation(words: list[str]) -> bool:
        """Check whether word list contains a negation word."""
        return bool(set(words) & _NEGATION_WORDS)

    @staticmethod
    def _antonym_score(words1: list[str], words2: list[str]) -> float:
        """Return a score 0.0-1.0 indicating how many antonym pairs are found."""
        found = 0
        for w in words1:
            antonyms = _ANTONYM_MAP.get(w)
            if antonyms and antonyms & set(words2):
                found += 1
        # Normalize: one pair is a strong signal, cap at 1.0.
        return min(1.0, found / 1.0)

    @staticmethod
    def _choice_conflict_score(stmt1: str, stmt2: str) -> float:
        """Detect conflicting choices like 'use X' vs 'use Y'.

        Looks for shared action verbs followed by different objects.
        """
        pattern = re.compile(
            r"\b(use|chose|selected|going with|switch(?:ing)?\s+to|"
            r"prefer|moving\s+to|opting\s+for)\s+(\S+)",
            re.IGNORECASE,
        )
        matches1 = pattern.findall(stmt1)
        matches2 = pattern.findall(stmt2)

        if not matches1 or not matches2:
            return 0.0

        for verb1, obj1 in matches1:
            for verb2, obj2 in matches2:
                # Same verb category, different object => conflict.
                v1 = re.sub(r"\s+", " ", verb1.lower().strip())
                v2 = re.sub(r"\s+", " ", verb2.lower().strip())
                o1 = obj1.lower().strip().rstrip(".,!?")
                o2 = obj2.lower().strip().rstrip(".,!?")
                # Verbs should be in the same family.
                if v1 == v2 or {v1, v2} <= {"use", "chose", "selected", "prefer"}:
                    if o1 != o2:
                        return 1.0
        return 0.0
