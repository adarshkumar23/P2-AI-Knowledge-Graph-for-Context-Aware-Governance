"""
Unit tests for src/p2_satellite/embeddings.py (Workstream B).

All tests inject a stub encode_fn -- never triggers a real
sentence-transformers model download. We assert shape/dimension (384) and
basic behavior (same text -> same vector, batch encoding works), not real
semantic quality.
"""

from __future__ import annotations

from src.p2_satellite import embeddings, graph_builder, schema
from src.p2_satellite.config import settings
from tests.fixtures.sample_export import (
    AI_SYSTEMS_EXPORT,
    JURISDICTIONS_EXPORT,
    REGULATIONS_CATALOG_EXPORT,
)


def stub_encode_fn(texts):
    """Deterministic stub: same text always maps to the same vector."""
    return [[0.1 + 0.001 * (hash(t) % 100)] * settings.embedding_dim for t in texts]


def constant_encode_fn(texts):
    return [[0.1] * settings.embedding_dim for _ in texts]


# --------------------------------------------------------------------------
# embed_node_text
# --------------------------------------------------------------------------


def test_embed_node_text_returns_correct_dimension():
    vec = embeddings.embed_node_text("General Data Protection Regulation", encode_fn=constant_encode_fn)
    assert len(vec) == 384
    assert len(vec) == settings.embedding_dim


def test_embed_node_text_same_text_same_vector():
    v1 = embeddings.embed_node_text("GDPR", encode_fn=stub_encode_fn)
    v2 = embeddings.embed_node_text("GDPR", encode_fn=stub_encode_fn)
    assert v1 == v2


def test_embed_node_text_different_text_can_differ():
    v1 = embeddings.embed_node_text("GDPR", encode_fn=stub_encode_fn)
    v2 = embeddings.embed_node_text("EU AI Act", encode_fn=stub_encode_fn)
    # Not asserting inequality is guaranteed for all strings (hash collisions
    # possible) but for this pair it should differ with the stub formula.
    assert v1 != v2 or hash("GDPR") % 100 == hash("EU AI Act") % 100


# --------------------------------------------------------------------------
# embed_graph_nodes
# --------------------------------------------------------------------------


def _sample_graph():
    return graph_builder.build_graph(AI_SYSTEMS_EXPORT, REGULATIONS_CATALOG_EXPORT, JURISDICTIONS_EXPORT)


def test_embed_graph_nodes_only_embeds_text_bearing_types():
    graph = _sample_graph()
    result = embeddings.embed_graph_nodes(graph, encode_fn=constant_encode_fn)

    embedded_types = {schema.split_node_id(nid)[0] for nid in result}
    assert embedded_types <= embeddings.DEFAULT_TEXT_NODE_TYPES
    # ai_system / jurisdiction / risk_tier should NOT be embedded by default.
    assert schema.NODE_AI_SYSTEM not in embedded_types
    assert schema.NODE_JURISDICTION not in embedded_types
    assert schema.NODE_RISK_TIER not in embedded_types
    # but regulation/obligation/control_type/data_category should be present.
    assert schema.NODE_REGULATION in embedded_types
    assert schema.NODE_OBLIGATION in embedded_types
    assert schema.NODE_CONTROL_TYPE in embedded_types
    assert schema.NODE_DATA_CATEGORY in embedded_types


def test_embed_graph_nodes_vectors_have_correct_dimension():
    graph = _sample_graph()
    result = embeddings.embed_graph_nodes(graph, encode_fn=constant_encode_fn)

    assert len(result) > 0
    for vec in result.values():
        assert len(vec) == settings.embedding_dim


def test_embed_graph_nodes_covers_expected_node_ids():
    graph = _sample_graph()
    result = embeddings.embed_graph_nodes(graph, encode_fn=constant_encode_fn)

    gdpr_id = schema.node_id(schema.NODE_REGULATION, "GDPR")
    assert gdpr_id in result


def test_embed_graph_nodes_respects_custom_node_types_arg():
    graph = _sample_graph()
    result = embeddings.embed_graph_nodes(graph, node_types={schema.NODE_REGULATION}, encode_fn=constant_encode_fn)
    embedded_types = {schema.split_node_id(nid)[0] for nid in result}
    assert embedded_types == {schema.NODE_REGULATION}


def test_embed_graph_nodes_batches_single_encode_call():
    graph = _sample_graph()
    call_count = {"n": 0}

    def counting_encode_fn(texts):
        call_count["n"] += 1
        return [[0.1] * settings.embedding_dim for _ in texts]

    embeddings.embed_graph_nodes(graph, encode_fn=counting_encode_fn)
    assert call_count["n"] == 1


def test_embed_graph_nodes_empty_when_no_matching_types():
    graph = _sample_graph()
    result = embeddings.embed_graph_nodes(graph, node_types=set(), encode_fn=constant_encode_fn)
    assert result == {}


# --------------------------------------------------------------------------
# get_encoder — lazy loading behavior (no real model download here)
# --------------------------------------------------------------------------


def test_get_encoder_does_not_load_model_until_called(monkeypatch):
    # Reset the module singleton so this test is independent of ordering.
    monkeypatch.setattr(embeddings, "_encoder_singleton", None)

    encoder = embeddings.get_encoder()
    # Merely obtaining the encoder must not have loaded a real model.
    assert encoder._model is None
