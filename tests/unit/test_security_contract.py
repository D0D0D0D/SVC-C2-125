"""TC-01/02/03/04/05/08 value-level security contract tests."""

from __future__ import annotations

import inspect

from framework.schemas.trust_level import TrustLevel
from src.nodes.inner_nodes import HandoffRankNode, IntentDecomposeExtractNode, SeasonalContextNode
from src.nodes.main_node import IntakeWorkflowGraphNode
from src.nodes.post_process_node import IntakePacketEnvelopeNode
from src.nodes.pre_process_node import InquiryIntakeValidateNode
from src.schemas.state import State


def test_state_is_flat_and_does_not_hold_context_or_credentials():
    annotations = State.__annotations__
    assert "intake_meta" in annotations
    assert not {"token", "api_key", "credential"} & set(annotations)
    assert "InvocationContext" not in str(annotations)


def test_every_privileged_node_is_denied_before_execute_at_anonymous_trust():
    nodes = [
        InquiryIntakeValidateNode(),
        IntakeWorkflowGraphNode(),
        IntakePacketEnvelopeNode(),
        SeasonalContextNode(),
        IntentDecomposeExtractNode(),
        HandoffRankNode(),
    ]
    state = {"caller_trust_level": TrustLevel.ANONYMOUS.value, "correlation_id": "tc08"}
    for node in nodes:
        result = node(state)
        assert result["status"] == "error"
        assert node.required_trust_level is TrustLevel.VERIFIED_EXTERNAL


def test_nodes_only_define_execute_and_extra_security_hooks_not_final_gates():
    for node in [InquiryIntakeValidateNode, IntakePacketEnvelopeNode, SeasonalContextNode, IntentDecomposeExtractNode, HandoffRankNode]:
        assert "_security_gate_input" not in node.__dict__
        assert "_security_gate_output" not in node.__dict__
        assert list(inspect.signature(node.execute).parameters)[:2] == ["self", "state"]
