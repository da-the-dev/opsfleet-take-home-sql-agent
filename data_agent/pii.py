"""Content-based PII masking — defense layers 2 and 3 (docs/ARCHITECTURE.md §4.2).

Detection operates on *values*, never on column names: pattern-with-validation
for structured PII (emails, phones) and NER (Presidio + spaCy) for person
names. Masked values become typed placeholders (``<EMAIL_1>``, ``<PERSON_2>``)
that stay consistent within one result set, so the model can still count and
compare distinct entities it never sees.

Person-name NER is *provenance-gated*: it is enforced when the executed query
touched the ``users`` table (the only source of customer names — sqlguard
reports this) and skipped otherwise, so a brand ranking ("Calvin Klein") is not
false-positive-masked in a pure product query. Emails and phones are masked
unconditionally — their validated patterns have near-zero false positives.

If Presidio/spaCy cannot load, the module degrades to regex-only detection
(emails/phones still fully covered) rather than failing the agent — resilience
over completeness, loudly logged.
"""

import logging
import os
import re
from functools import lru_cache
from typing import Any, Optional

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# Requires phone-like structure (separators / +CC), so bare numeric IDs don't match.
_PHONE_RE = re.compile(
    r"(?<!\w)(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?)?\d{2,4}[\s.-]\d{2,4}[\s.-]\d{2,6}(?!\w)"
)

_PRESIDIO_ENTITIES = ["EMAIL_ADDRESS", "PHONE_NUMBER", "PERSON"]
# Presidio's phone recognizer caps at 0.4 for pattern-only matches; stay under it.
_SCORE_THRESHOLD = 0.35


@lru_cache(maxsize=1)
def _analyzer():
    """Presidio AnalyzerEngine, or None if unavailable (regex fallback then applies)."""
    try:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider

        provider = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [
                    {
                        "lang_code": "en",
                        "model_name": os.getenv("SPACY_MODEL", "en_core_web_sm"),
                    }
                ],
            }
        )
        engine = AnalyzerEngine(nlp_engine=provider.create_engine())
        logger.info("Presidio analyzer ready (spaCy en_core_web_sm)")
        return engine
    except Exception:  # noqa: BLE001 - degrade, don't die (design §4.5)
        logger.exception("Presidio unavailable; falling back to regex-only PII detection")
        return None


def _detect(text: str, include_person: bool) -> list[tuple[int, int, str]]:
    """Return (start, end, entity_type) spans found in `text`."""
    engine = _analyzer()
    if engine is not None:
        entities = _PRESIDIO_ENTITIES if include_person else _PRESIDIO_ENTITIES[:2]
        results = engine.analyze(text=text, language="en", entities=entities)
        return [
            (r.start, r.end, "PERSON" if r.entity_type == "PERSON" else r.entity_type.split("_")[0])
            for r in results
            if r.score >= _SCORE_THRESHOLD
        ]
    spans = [(m.start(), m.end(), "EMAIL") for m in _EMAIL_RE.finditer(text)]
    spans += [(m.start(), m.end(), "PHONE") for m in _PHONE_RE.finditer(text)]
    return spans


class Masker:
    """Masks PII across one result set (or one output text) with consistent placeholders."""

    def __init__(self, include_person: bool) -> None:
        self.include_person = include_person
        self._placeholders: dict[str, str] = {}
        self._counters: dict[str, int] = {}
        self.hits = 0

    def _placeholder(self, value: str, entity: str) -> str:
        if value not in self._placeholders:
            self._counters[entity] = self._counters.get(entity, 0) + 1
            self._placeholders[value] = f"<{entity}_{self._counters[entity]}>"
        return self._placeholders[value]

    def mask_text(self, text: str) -> str:
        spans = _detect(text, self.include_person)
        if not spans:
            return text
        # Replace right-to-left so earlier offsets stay valid; drop overlaps.
        spans.sort(key=lambda s: (s[0], -s[1]))
        merged: list[tuple[int, int, str]] = []
        for span in spans:
            if merged and span[0] < merged[-1][1]:
                continue
            merged.append(span)
        self.hits += len(merged)
        for start, end, entity in reversed(merged):
            text = text[:start] + self._placeholder(text[start:end], entity) + text[end:]
        return text

    def mask_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {k: self.mask_text(v) if isinstance(v, str) else v for k, v in row.items()}
            for row in rows
        ]


def mask_result_rows(
    rows: list[dict[str, Any]], touches_pii_table: bool
) -> tuple[list[dict[str, Any]], int]:
    """Layer 2: mask a result set before it enters LLM context."""
    masker = Masker(include_person=touches_pii_table)
    masked = masker.mask_rows(rows)
    if masker.hits:
        logger.warning("PII mask (layer 2) redacted %d value(s) from query results", masker.hits)
    return masked, masker.hits


def scan_output(text: str, strict_person: bool = False) -> tuple[str, int]:
    """Layer 3: final sweep over the rendered answer before it reaches the user."""
    masker = Masker(include_person=strict_person)
    cleaned = masker.mask_text(text)
    if masker.hits:
        logger.warning("PII output scan (layer 3) redacted %d value(s)", masker.hits)
    return cleaned, masker.hits


def warm_up() -> Optional[bool]:
    """Pre-load the NER engine at startup so first query latency isn't hit."""
    return _analyzer() is not None
