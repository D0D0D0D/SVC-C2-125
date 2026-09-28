"""Deterministic deadline/quantity normalization and ranking regression tests."""

from __future__ import annotations

import pytest

import src.nodes.inner_nodes as inner_nodes
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.nodes.inner_nodes import HandoffRankNode, policy_snapshot
from src.nodes.post_process_node import IntakePacketEnvelopeNode
from src.services.service import load_runtime_config


def _config_and_snapshot():
    config = load_runtime_config()
    snapshot = policy_snapshot(config)
    assert snapshot is not None
    return config, snapshot


def _basis(snapshot):
    return {
        "ranking_mode": "policy_applied",
        "policy_id": snapshot["policy_id"],
        "policy_version": snapshot["policy_version"],
        "policy_digest": snapshot["policy_digest"],
        "seasonal_calendar_version": snapshot["seasonal_calendar_version"],
    }


def _intent(text, intent_id, span, field, fact_span, value):
    return {
        "intent_id": intent_id,
        "type": "machinery_booking",
        "source_span": span,
        "occurrence_index": 0,
        "confidence": "high",
        "related_intent_ids": [],
        "extracted_facts": [
            {
                "field": field,
                "value": value,
                "source_span": fact_span,
                "occurrence_index": 0,
                "confidence": "high",
            }
        ],
        "missing_information": [],
    }


def _rank(text, intents, business_date="2026-07-30"):
    config, snapshot = _config_and_snapshot()
    state = {
        "validated_input": text,
        "intake_meta": {"inquiry_id": "inq-abcdefgh", "source_channel": "chat", "caller_ref": "staff-kojima", "business_date": business_date},
        "seasonal_context": {"window_name": "outside_calendar", "critical_crops": []},
        "ranking_basis": _basis(snapshot),
        "decomposed_intents": intents,
        "status": AgentStatus.SUCCESS.value,
    }
    result = HandoffRankNode(config=config, snapshot=snapshot).execute(state)
    return config, snapshot, state, result


def _assert_earliest_deadline_first(result):
    packets = result["handoff_packets"]
    assert all(packet["extracted_facts"] for packet in packets)
    assert [packet["extracted_facts"][0]["value"] for packet in packets] == ["2026-08-10", "2026-08-20"]
    assert [packet["routing"]["rank"] for packet in packets] == [1, 2]


def test_absolute_deadline_normalization_is_accepted_and_changes_rank_then_passes_s3():
    text = "防除機を8月20日に予約したい。防除機を8月10日に予約したい。"
    intents = [
        _intent(text, "i-late", "防除機を8月20日に予約", "deadline", "8月20日", "2026-08-20"),
        _intent(text, "i-early", "防除機を8月10日に予約", "deadline", "8月10日", "2026-08-10"),
    ]
    config, snapshot, state, ranked = _rank(text, intents)
    _assert_earliest_deadline_first(ranked)

    post_state = {**state, **ranked, "mode": "invoke", "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value}
    formatted = IntakePacketEnvelopeNode(config=config, snapshot=snapshot)(post_state)
    assert formatted["status"] == AgentStatus.SUCCESS.value
    assert formatted["formatted_output"]["packets"][0]["extracted_facts"][0]["value"] == "2026-08-10"


@pytest.mark.parametrize(
    ("source_span", "value", "field", "expected_facts"),
    [
        ("来週", "2026-08-06", "deadline", []),
        ("来週", "来週", "deadline", ["来週"]),
        ("8月10日", "2030-01-01", "deadline", []),
        ("べと病ではない", "べと病", "symptom", []),
        ("六十二リットル", "62L", "quantity", ["62L"]),
        ("1月5日", "2026-01-05", "deadline", []),
        ("1月5日", "1月5日", "deadline", ["1月5日"]),
    ],
)
def test_fact_value_is_accepted_only_when_literal_or_span_derived(source_span, value, field, expected_facts):
    text = f"防除機の相談は{source_span}です"
    intent = _intent(text, "i-01", text, field, source_span, value)
    _config, _snapshot, _state, ranked = _rank(text, [intent])
    values = [fact["value"] for fact in ranked["handoff_packets"][0]["extracted_facts"]]
    assert values == expected_facts
    if not expected_facts:
        assert f"unverified_value:{field}" in ranked["coverage_gaps"]


def test_deadline_ranking_test_detects_normalizer_always_none_mutation(monkeypatch):
    text = "防除機を8月20日に予約したい。防除機を8月10日に予約したい。"
    intents = [
        _intent(text, "i-late", "防除機を8月20日に予約", "deadline", "8月20日", "2026-08-20"),
        _intent(text, "i-early", "防除機を8月10日に予約", "deadline", "8月10日", "2026-08-10"),
    ]
    monkeypatch.setattr(inner_nodes, "_normalize_fact_value", lambda _field, _span, _date: None)
    _config, _snapshot, _state, ranked = _rank(text, intents)

    with pytest.raises(AssertionError):
        _assert_earliest_deadline_first(ranked)
