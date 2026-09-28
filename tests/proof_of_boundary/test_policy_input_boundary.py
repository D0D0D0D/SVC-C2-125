"""PB-1/PB-3: domain audit and policy-file boundary coverage."""

from __future__ import annotations

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from src.graph.graph import JAMemberInquiryIntakeAgent


def _invoke(config=None):
    agent = JAMemberInquiryIntakeAgent(config or {})
    agent.compile()
    ctx = InvocationContext(session_id="sess-abcdefgh", caller_id="staff-kojima", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
    return agent.invoke("トマトの相談", ctx=ctx, input_context={"intake_meta": {"inquiry_id": "inq-abcdefgh", "source_channel": "chat", "caller_ref": "staff-kojima", "business_date": "2026-07-30"}})


def test_policy_file_boundary_falls_back_without_external_service():
    healthy = _invoke()
    degraded = _invoke({"routing_policy_path": "not-provisioned.yaml", "seasonal_calendar_path": "not-provisioned.yaml"})
    assert healthy["status"] == "success"
    assert degraded["status"] == "success"
    assert degraded["output"]["packets"][0]["ranking_basis"]["ranking_mode"] == "policy_unavailable"
    assert "routing_policy_unavailable" in degraded["output"]["degradation_reason"]
