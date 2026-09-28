"""PB-1 / TC-05: prove domain events, not framework lifecycle events, fire."""

from __future__ import annotations

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from src.graph.graph import JAMemberInquiryIntakeAgent
import src.nodes.inner_nodes as inner_module
import src.nodes.main_node as main_module
import src.nodes.post_process_node as post_module
import src.nodes.pre_process_node as pre_module


def test_each_domain_node_module_emits_its_own_event_binding(monkeypatch):
    seen: list[tuple[str, str]] = []

    def capture(module_name):
        return lambda event_name, _payload, _state: seen.append((module_name, event_name))

    monkeypatch.setattr(pre_module, "emit_trace_event", capture("pre"))
    monkeypatch.setattr(main_module, "emit_trace_event", capture("main"))
    monkeypatch.setattr(inner_module, "emit_trace_event", capture("inner"))
    monkeypatch.setattr(post_module, "emit_trace_event", capture("post"))
    agent = JAMemberInquiryIntakeAgent()
    agent.compile()
    ctx = InvocationContext(session_id="sess-abcdefgh", caller_id="staff-kojima", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
    result = agent.invoke("トマトについて相談したい", ctx=ctx, input_context={"intake_meta": {"inquiry_id": "inq-abcdefgh", "source_channel": "chat", "caller_ref": "staff-kojima", "business_date": "2026-07-30"}})
    assert result["status"] == "success"
    assert ("pre", "inquiry_intake_validated") in seen
    assert ("main", "intake_workflow_completed") in seen
    assert ("inner", "seasonal_context_resolved") in seen
    assert ("inner", "intent_decomposition_completed") in seen
    assert ("inner", "handoff_ranking_completed") in seen
    assert ("post", "handoff_packets_emitted") in seen
