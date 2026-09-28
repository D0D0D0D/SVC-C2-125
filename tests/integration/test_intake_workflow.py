"""Value-level acceptance coverage for the JA member inquiry workflow."""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from framework.errors import SecurityViolationError
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.security import detect_pii
from src.api.server import app
from src.graph.graph import JAMemberInquiryIntakeAgent
from src.nodes.post_process_node import IntakePacketEnvelopeNode
from src.nodes.pre_process_node import InquiryIntakeValidateNode


TEXT = "トマトの葉が枯れている。防除機の予約をしたい。補助金の書類を確認したい。"


class FakeLlm:
    def complete(self, _messages):
        def intent(intent_id, kind, span, facts, missing):
            return {
                "intent_id": intent_id,
                "type": kind,
                "source_span": span,
                "occurrence_index": 0,
                "confidence": "high",
                "related_intent_ids": [],
                "extracted_facts": facts,
                "missing_information": missing,
            }

        def fact(field, span):
            return {"field": field, "value": span, "source_span": span, "occurrence_index": 0, "confidence": "high"}

        payload = {
            "intents": [
                intent("i-01", "pest_symptom_report", "トマトの葉が枯れている", [fact("crop", "トマト"), fact("symptom", "葉が枯れている")], [{"missing_field": "parcel", "ask_target": "internal_record", "staff_action": "圃場台帳で対象区画を照合する"}]),
                intent("i-02", "machinery_booking", "防除機の予約", [fact("machine", "防除機")], [{"missing_field": "deadline", "ask_target": "member", "staff_action": "会員に使用希望日を確認する"}]),
                intent("i-03", "subsidy_inquiry", "補助金の書類", [fact("program", "補助金")], [{"missing_field": "program", "ask_target": "member", "staff_action": "会員に対象制度名を確認する"}]),
            ]
        }
        import json

        return {"content": json.dumps(payload, ensure_ascii=False)}


class StaticLlm:
    def __init__(self, payload):
        self.payload = payload

    def complete(self, _messages):
        import json

        return {"content": json.dumps(self.payload, ensure_ascii=False)}


class TimeoutLlm:
    def complete(self, _messages):
        raise TimeoutError("provider-specific detail")


def _meta(**changes):
    base = {"inquiry_id": "inq-abcdefgh", "source_channel": "chat", "caller_ref": "staff-kojima", "business_date": "2026-07-30"}
    base.update(changes)
    return base


def _invoke(config=None, text=TEXT, meta=None):
    agent = JAMemberInquiryIntakeAgent({"llm": FakeLlm(), **(config or {})})
    agent.compile()
    ctx = InvocationContext(session_id="sess-abcdefgh", caller_id="staff-kojima", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
    return agent.invoke(text, ctx=ctx, input_context={"intake_meta": meta or _meta()})


def test_three_mixed_inquiries_produce_distinct_ranked_packets():
    output = _invoke()["output"]
    assert len(output["packets"]) == 3
    assert [packet["routing"]["rank"] for packet in output["packets"]] == [1, 2, 3]
    assert output["packets"][0]["intent"]["type"] == "pest_symptom_report"
    for packet in output["packets"]:
        assert packet["intent"]["source_span"]
        assert packet["extracted_facts"]
        assert packet["missing_information"]
        assert packet["routing"]["capability"]
        assert packet["ranking_basis"]["policy_version"] == "2026-07"


def test_duplicate_literal_occurrence_is_resolved_to_deterministic_offsets():
    text = "防除機の予約をしたい。防除機の点検も相談したい。"
    payload = {
        "intents": [
            {
                "intent_id": f"i-0{index + 1}",
                "type": "machinery_booking",
                "source_span": "防除機",
                "occurrence_index": index,
                "confidence": "high",
                "related_intent_ids": [],
                "extracted_facts": [],
                "missing_information": [],
            }
            for index in range(2)
        ]
    }

    output = _invoke({"llm": StaticLlm(payload)}, text=text)["output"]

    starts = [packet["intent"]["span_offsets"]["start"] for packet in output["packets"]]
    assert starts == [text.find("防除機"), text.rfind("防除機")]
    assert all(packet["intent"]["span_offsets"]["end"] == start + len("防除機") for packet, start in zip(output["packets"], starts, strict=True))


def test_non_literal_llm_span_is_dropped_fail_closed():
    payload = {
        "intents": [
            {
                "intent_id": "i-01",
                "type": "machinery_booking",
                "source_span": "本文には存在しない防除機予約",
                "occurrence_index": 0,
                "confidence": "high",
                "related_intent_ids": [],
                "extracted_facts": [],
                "missing_information": [],
            }
        ]
    }

    output = _invoke({"llm": StaticLlm(payload)}, text="肥料について相談したい")["output"]

    assert output["packets"] == []
    assert "unverified_span:intent:not_found" in output["coverage_gaps"]
    assert "no_verifiable_intent" in output["degradation_reason"]


def test_provider_timeout_error_metadata_reaches_public_response():
    response = _invoke({"llm": TimeoutLlm()})

    assert response["output"] is None
    assert response["status"] == "error"
    assert response["error_code"] == "llm_call_failed"
    assert response["error_type"] == "TimeoutError"
    assert response["error_log"] == ["LlmDraftStageNode: llm_call_failed (TimeoutError)"]


def test_fullwidth_phone_is_masked_once_in_canonical_text():
    node = InquiryIntakeValidateNode()
    state = {"user_input": "０９０－１２３４－５６７８", "intake_meta": _meta(), "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value}
    result = node(state)
    assert result["status"] == "success"
    assert detect_pii(result["validated_input"]) == []
    assert "[MASKED]" in result["validated_input"]


def test_known_limitation_pii_not_detected_when_adjacent_to_japanese():
    """Framework detect_pii has a word-boundary limitation; do not add a template sanitizer."""
    node = InquiryIntakeValidateNode()
    state = {"user_input": "連絡先は０９０－１２３４－５６７８です。", "intake_meta": _meta(), "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value}
    canonical = node(state)["validated_input"]
    assert "090-1234-5678" in canonical
    assert detect_pii(canonical) == []


def test_policy_unavailable_uses_closed_fallback_contract():
    output = _invoke({"routing_policy_path": "missing-policy.yaml", "seasonal_calendar_path": "missing-calendar.yaml"})["output"]
    packet = output["packets"][0]
    assert output["degradation_reason"] == "routing_policy_unavailable"
    assert packet["ranking_basis"]["ranking_mode"] == "policy_unavailable"
    assert packet["intent"]["type"] == "other_general"
    assert packet["routing"]["capability"] == "other_general"
    assert packet["missing_information"][0]["missing_field"] == "unspecified"


def test_expected_policy_version_mismatch_fails_closed_against_working_control():
    assert _invoke()["status"] == "success"
    failed = _invoke(meta=_meta(expected_policy_version="2099-01"))
    assert failed["status"] == "error"


def test_s3_recalculates_spans_and_does_not_trust_boolean_claim():
    output = _invoke()["output"]
    node = IntakePacketEnvelopeNode(config=JAMemberInquiryIntakeAgent().config)
    baseline = {"formatted_output": copy.deepcopy(output), "_verification_basis": TEXT, "status": "success"}
    assert node._extra_security_gate_output(baseline)["formatted_output"]["provenance_verified"] is True
    false_claim = {"formatted_output": copy.deepcopy(output), "_verification_basis": TEXT, "status": "success"}
    false_claim["formatted_output"]["provenance_verified"] = False
    with pytest.raises(SecurityViolationError, match="provenance_verified"):
        node._extra_security_gate_output(false_claim)
    bad = {"formatted_output": copy.deepcopy(output), "_verification_basis": TEXT, "status": "success"}
    bad["formatted_output"]["packets"][0]["intent"]["span_offsets"]["start"] += 1
    with pytest.raises(SecurityViolationError, match="provenance"):
        node._extra_security_gate_output(bad)


@pytest.mark.parametrize("key", ["diagnosis", "eligibility", "booking_confirmed", "assigned_to"])
def test_s3_closed_allowlist_rejects_forbidden_fields(key):
    output = _invoke()["output"]
    output["packets"][0][key] = True
    with pytest.raises(SecurityViolationError, match="allowlist"):
        IntakePacketEnvelopeNode(config=JAMemberInquiryIntakeAgent().config)._extra_security_gate_output({"formatted_output": output, "_verification_basis": TEXT, "status": "success"})


def test_config_caps_change_end_to_end_result():
    assert len(_invoke()["output"]["packets"]) == 3
    assert len(_invoke({"max_intents": 1})["output"]["packets"]) == 1
    short = _invoke({"min_span_chars": 50})["output"]
    assert short["packets"] == []
    assert "no_verifiable_intent" in short["degradation_reason"]


def test_server_startup_and_context_validation(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-jwt-for-testing")
    with TestClient(app, raise_server_exceptions=False) as client:
        valid = client.post("/invoke", json={"input": "トマトについて相談したい"}, headers={"authorization": "Bearer mock-jwt-for-testing"})
        unknown = client.post("/invoke", json={"input": "x", "context": {"operator_id": "untrusted"}}, headers={"authorization": "Bearer mock-jwt-for-testing"})
    assert valid.status_code == 200
    assert valid.json()["status"] == "success"
    assert unknown.status_code == 422
    assert app.state.agent_config["business_timezone"] == "Asia/Tokyo"


def test_health_endpoint_returns_ok_for_deploy_verification():
    """`scripts/stg_invoke_evidence.py` が health_200 / deploy_exit_code を判定する経路。

    このエンドポイントが無いと deploy-stg が構造的に FAIL する（scaffold seed には存在するが
    本テンプレの server.py 実装時に落ちていた・2026-07-31 に STG 発火前の実 HTTP 確認で検出）。
    """
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
