"""Regression contract for the real LLM's decomposition/extraction prompt."""

from __future__ import annotations

import json

import pytest

from src.nodes.inner_nodes import IntentDecomposeExtractNode


FIELDS = {"crop", "growth_stage", "parcel", "symptom", "deadline", "quantity", "machine", "program"}


def _assert_prompt_contract(prompt: str, max_question_chars: int) -> None:
    payload = json.loads(prompt)
    intent = payload["schema"]["intents"][0]
    fact = intent["extracted_facts"][0]
    missing = intent["missing_information"][0]
    assert {"field", "value", "source_span", "occurrence_index", "confidence"} <= set(fact)
    assert "occurrence_index" in intent
    assert "span_offsets" not in intent
    assert "span_offsets" not in fact
    assert {"missing_field", "ask_target", "staff_action"} <= set(missing)
    assert set(payload["allowlists"]["field"]) == FIELDS
    assert set(payload["allowlists"]["missing_field"]) == FIELDS | {"unspecified"}
    assert set(payload["allowlists"]["ask_target"]) == {"member", "internal_record"}
    assert set(payload["allowlists"]["confidence"]) == {"high", "medium", "low"}
    assert str(max_question_chars) in missing["staff_action"]
    assert any("why_needed を書かない" in item for item in payload["prohibitions"])
    # 🔴 deadline / quantity の正規形を「許可」ではなく「指示」として書く。
    #    許可形（"…を許可"）だと LLM は原則側（literal）に倒れ、deadline ランキングが実運用で
    #    発動しない（実 LLM 1 回転で実測。2026-07-31）。
    value_spec = fact["value"]
    assert "ISO-8601" in value_spec, "deadline の ISO 正規形の指示がプロンプトに無い"
    assert "相対表現" in value_spec and "推測せず" in value_spec, "相対表現を推測しない指示が無い"
    assert "62L" in value_spec or "数値＋単位" in value_spec, "quantity の正規形の指示が無い"


def test_prompt_contract_includes_fact_and_staff_task_schema_with_configured_limit():
    node = IntentDecomposeExtractNode(config={"max_question_chars": 73})
    prompt = node.build_prompt({"validated_input": "トマトの葉が枯れている"})

    _assert_prompt_contract(prompt, 73)
    for key in ("field", "value", "source_span", "occurrence_index", "missing_field", "ask_target", "staff_action"):
        assert f'"{key}"' in prompt
    assert "左から0始まり" in prompt
    assert "2回目の「防除機」" in prompt


def test_prompt_contract_detects_mutation_that_removes_fact_element_shape():
    node = IntentDecomposeExtractNode(config={"max_question_chars": 73})
    payload = json.loads(node.build_prompt({"validated_input": "トマトの葉が枯れている"}))
    del payload["schema"]["intents"][0]["extracted_facts"][0]["field"]

    with pytest.raises(AssertionError):
        _assert_prompt_contract(json.dumps(payload, ensure_ascii=False), 73)
