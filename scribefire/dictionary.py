"""Personal dictionary: fix words the transcription consistently gets wrong.

Whisper writes "pie torch" for PyTorch and "cloud" for Claude.  Fixing them
by hand defeats the point of dictating.

Rules:
*   case-insensitive match on the wrong form
*   the replacement's casing wins, not the input's
*   whole words and multi-word phrases only, never bare substrings
*   longest match first, so a two-word entry beats a one-word entry
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Sequence, Tuple

from .logs import get_logger

log = get_logger("dictionary")


class PersonalDictionary:
    """A list of ``(wrong form, correct spelling)`` replacement pairs."""

    def __init__(self, pairs: Optional[Iterable[Sequence[str]]] = None, logger=None):
        self.log = logger or log
        self.pairs: List[Tuple[str, str]] = []
        self._pattern: Optional[re.Pattern] = None
        self._lookup: dict = {}
        self.set_pairs(pairs or [])

    # -- setup ---------------------------------------------------------

    def set_pairs(self, pairs: Iterable[Sequence[str]]) -> None:
        normalised: List[Tuple[str, str]] = []
        for pair in pairs:
            try:
                wrong, right = pair[0], pair[1]
            except (TypeError, IndexError):
                continue
            wrong = str(wrong).strip()
            right = str(right).strip()
            if not wrong:
                continue
            normalised.append((wrong, right))

        # Longest wrong form first so the longest match wins.
        normalised.sort(key=lambda item: (-len(item[0]), item[0].lower()))
        self.pairs = normalised

        self._lookup = {wrong.lower(): right for wrong, right in normalised}
        self._pattern = self._build_pattern(normalised)

    @staticmethod
    def _build_pattern(pairs: Sequence[Tuple[str, str]]) -> Optional[re.Pattern]:
        if not pairs:
            return None
        alternatives = []
        for wrong, _right in pairs:
            escaped = re.escape(wrong).replace(r"\ ", r"\s+")
            # Word boundaries only - never a bare substring.
            alternatives.append(r"(?<!\w)%s(?!\w)" % escaped)
        return re.compile("|".join(alternatives), re.IGNORECASE)

    # -- matching ------------------------------------------------------

    @staticmethod
    def _apply_casing(replacement: str, matched: str) -> str:
        """Keep the replacement's casing intent, not the input's.

        A replacement with meaningful internal casing ("PyTorch") is used
        verbatim.  An all-lower or all-upper replacement adopts the shape of
        the word it replaced.
        """
        if replacement != replacement.lower() and replacement != replacement.upper():
            return replacement
        if matched.isupper() and replacement.islower():
            return replacement.upper()
        if matched[:1].isupper() and replacement.islower():
            return replacement[:1].upper() + replacement[1:]
        return replacement

    def apply(self, text: str) -> Tuple[str, List[Tuple[str, str, str]]]:
        """Apply the dictionary to *text*.

        Returns the new text and a list of ``(matched, replacement, entry)``
        tuples describing every replacement that fired.
        """
        if not text or self._pattern is None:
            return text, []

        fired: List[Tuple[str, str, str]] = []

        def _replace(match: "re.Match") -> str:
            matched = match.group(0)
            key = re.sub(r"\s+", " ", matched).strip().lower()
            replacement = self._lookup.get(key)
            if replacement is None:
                return matched
            result = self._apply_casing(replacement, matched)
            fired.append((matched, result, replacement))
            return result

        new_text = self._pattern.sub(_replace, text)
        if fired:
            for matched, result, _entry in fired:
                self.log.info("dictionary: %r -> %r", matched, result)
        return new_text, fired

    # -- convenience ---------------------------------------------------

    def entries(self) -> List[List[str]]:
        return [[wrong, right] for wrong, right in self.pairs]

    def __len__(self) -> int:
        return len(self.pairs)
