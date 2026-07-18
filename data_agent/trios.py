"""Golden Bucket retrieval — Hybrid Intelligence (docs/ARCHITECTURE.md §4.1).

Prototype-scale stand-in for the production GCS → pgvector pipeline: a seed
JSON of human-curated trios (Question → SQL → Analyst Report), embedded once
per process and searched in memory by cosine similarity. Same mechanism,
smaller box.

Degrades, doesn't die (§4.5): if the embedding API is unavailable, retrieval
falls back to keyword overlap — weaker ranking, but the agent keeps answering.
"""

import json
import logging
import math
import re
from dataclasses import dataclass
from typing import Optional

from . import config

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "a an and are by for from how in is of on our the to what which who why".split()
)


@dataclass
class Trio:
    id: str
    question: str
    sql: str
    report: str


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS}


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


class GoldenBucket:
    def __init__(self, path=config.TRIOS_FILE) -> None:
        raw = json.loads(path.read_text())
        self.trios = [Trio(**t) for t in raw]
        self._vectors: Optional[list[list[float]]] = None
        self._embedder = None

    def _embed(self, texts: list[str]) -> Optional[list[list[float]]]:
        try:
            if self._embedder is None:
                from .llm import build_embeddings

                self._embedder = build_embeddings()
            if self._embedder is None:
                return None
            return self._embedder.embed_documents(texts)
        except Exception:  # noqa: BLE001
            logger.warning("Embedding unavailable; golden-trio retrieval falls back to keywords")
            return None

    def retrieve(self, question: str, k: int = 3) -> list[Trio]:
        """Top-k most similar trios; keyword-overlap fallback if embeddings fail."""
        if self._vectors is None:
            self._vectors = self._embed([t.question for t in self.trios]) or []
        if self._vectors:
            query_vec = self._embed([question])
            if query_vec:
                scored = [
                    (_cosine(query_vec[0], vec), trio)
                    for vec, trio in zip(self._vectors, self.trios)
                ]
                scored.sort(key=lambda s: s[0], reverse=True)
                return [trio for _, trio in scored[:k]]
        query_tokens = _tokens(question)
        scored_kw = [
            (len(query_tokens & _tokens(trio.question)), trio) for trio in self.trios
        ]
        scored_kw.sort(key=lambda s: s[0], reverse=True)
        return [trio for score, trio in scored_kw[:k] if score > 0]


def format_trios(trios: list[Trio]) -> str:
    """Render retrieved trios as few-shot context for the SQL/analysis prompt."""
    if not trios:
        return ""
    blocks = []
    for i, trio in enumerate(trios, 1):
        blocks.append(
            f"### Example {i}\n"
            f"Question: {trio.question}\n"
            f"SQL:\n```sql\n{trio.sql}\n```\n"
            f"Analyst notes: {trio.report}"
        )
    return (
        "Relevant examples of how our analysts answered similar questions "
        "(mirror their conventions, joins, and framing):\n\n" + "\n\n".join(blocks)
    )
