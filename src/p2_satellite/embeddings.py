"""
Node text embeddings for the P2 satellite (Workstream B).

Wraps sentence-transformers' all-MiniLM-L6-v2 (settings.embedding_model,
384-dim per settings.embedding_dim) to embed node text for semantic node
matching via pgvector on the core side.

The real SentenceTransformer model is lazily loaded behind get_encoder() and
cached as a module-level singleton, so importing this module never triggers
a HuggingFace download. Every public function also accepts an injectable
`encode_fn` so unit tests can run fully offline with a stub encoder (e.g.
`lambda texts: [[0.1] * 384 for _ in texts]`) instead of downloading the
real model — no guaranteed network access in this sandbox/CI.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import networkx as nx

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

from src.p2_satellite import schema
from src.p2_satellite.config import settings

# Signature: (list[str]) -> list[list[float]]
EncodeFn = Callable[[list[str]], list[list[float]]]

# Node types that carry meaningful text in this schema (per PATENT.md's
# node types + what tests/fixtures/sample_export.py actually populates).
# ai_system / jurisdiction / risk_tier are excluded -- they're identifiers/
# enums, not free text, in the current export shape.
DEFAULT_TEXT_NODE_TYPES: frozenset[str] = frozenset(
    {
        schema.NODE_REGULATION,
        schema.NODE_DATA_CATEGORY,
        schema.NODE_OBLIGATION,
        schema.NODE_CONTROL_TYPE,
    }
)

_encoder_singleton = None


class _SentenceTransformerEncoder:
    """Thin wrapper around a lazily-loaded SentenceTransformer model."""

    def __init__(self, model_name: str) -> None:
        self._model_name = model_name
        self._model: SentenceTransformer | None = None

    def _load(self) -> SentenceTransformer:
        if self._model is None:
            # Imported lazily so merely importing this module (or using an
            # injected encode_fn) never requires sentence-transformers to
            # touch the network / download weights.
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self._model_name)
        return self._model

    def __call__(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        vectors = model.encode(list(texts))
        return [list(map(float, v)) for v in vectors]


def get_encoder() -> EncodeFn:
    """Return a cached singleton encode function backed by the real model.

    Only actually loads (and downloads, if not cached locally) the
    SentenceTransformer model on first *call*, not on import / first
    reference to get_encoder itself.
    """
    global _encoder_singleton
    if _encoder_singleton is None:
        _encoder_singleton = _SentenceTransformerEncoder(settings.embedding_model)
    return _encoder_singleton


def _node_text(node_type: str, node_key: str, attrs: dict) -> str:
    """Best-effort text for a node: prefer rich text attrs, fall back to node_key."""
    for attr_name in ("description", "name", "label", "text"):
        val = attrs.get(attr_name)
        if isinstance(val, str) and val.strip():
            return val
    # node_key is a reasonable minimum fallback (e.g. "GDPR", "biometric").
    return node_key


def embed_node_text(text: str, encode_fn: EncodeFn | None = None) -> list[float]:
    """Embed a single piece of node text, returning a settings.embedding_dim vector."""
    encode = encode_fn if encode_fn is not None else get_encoder()
    vectors = encode([text])
    return vectors[0]


def embed_graph_nodes(
    graph: nx.DiGraph,
    node_types: set[str] | None = None,
    encode_fn: EncodeFn | None = None,
) -> dict[str, list[float]]:
    """Embed only text-bearing node types (default: DEFAULT_TEXT_NODE_TYPES).

    Returns node_id -> embedding vector. Batches all texts into a single
    encode() call for efficiency.
    """
    types_to_embed = node_types if node_types is not None else DEFAULT_TEXT_NODE_TYPES
    encode = encode_fn if encode_fn is not None else get_encoder()

    node_ids: list[str] = []
    texts: list[str] = []
    for nid, attrs in graph.nodes(data=True):
        node_type = attrs.get("node_type")
        if node_type not in types_to_embed:
            continue
        node_key = attrs.get("node_key", schema.split_node_id(nid)[1])
        node_ids.append(nid)
        texts.append(_node_text(node_type, node_key, attrs))

    if not node_ids:
        return {}

    vectors = encode(texts)
    return {nid: list(vec) for nid, vec in zip(node_ids, vectors, strict=True)}
