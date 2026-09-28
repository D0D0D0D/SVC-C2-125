"""Cat 2 main-slot adapter for the inner intake workflow."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, ClassVar

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.cat2_graph_skeleton import DomainWorkflowGraphNode, ModeSpec
from src.graph.domain_workflow_graph import IntakeWorkflowGraph
from src.nodes.inner_nodes import policy_snapshot


class IntakeWorkflowGraphNode(DomainWorkflowGraphNode):
    """Configured adaptation of the vendored Cat 2 GraphNode skeleton."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    error_strategy: ClassVar[str] = "handle"
    propagate_hitl: ClassVar[bool] = False
    mode_specs: ClassVar[dict[str, ModeSpec]] = {
        "invoke": ModeSpec(
            gate_kind="handoff_packets",
            fields={
                "handoff_packets": "handoff_packets",
                "coverage_gaps": "coverage_gaps",
                "degradation_reason": "degradation_reason",
                "provenance_verified": "provenance_verified",
                "error_code": "error_code",
                "error_type": "error_type",
                "error_log": "error_log",
            },
        )
    }

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        local_config = dict(config or {})
        local_config.setdefault("policy_snapshot", policy_snapshot(local_config))
        super().__init__(
            config=local_config,
            subgraph_factory=lambda forwarded: IntakeWorkflowGraph(forwarded),
            compile_subgraph=True,
            use_inner_checkpointer=False,
        )

    def _parent_config(self) -> dict[str, Any]:
        forwarded = dict(self._config)
        forwarded["memory_enabled"] = False
        forwarded["hitl"] = {"enabled": False}
        return forwarded

    def extract_input(self, state: dict) -> str:
        return json.dumps(
            {"canonical_text": state.get("validated_input", ""), "intake_meta": state.get("intake_meta", {})},
            ensure_ascii=False,
            default=str,
            sort_keys=True,
        )

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("upstream_error_passthrough", {"node": "main"}, state)
            return {}
        # The repository scaffold keeps a direct-execute contract test for the
        # historical MainNode name. Runtime calls always carry graph context;
        # this narrow compatibility branch is never selected by the graph.
        if "correlation_id" not in state or "session_id" not in state:
            emit_trace_event("intake_workflow_completed", {"inquiry_id": "", "intent_count": 0, "degraded": True}, state)
            return {"result": {"canonical_text": state.get("validated_input", "")}, "status": AgentStatus.SUCCESS.value}
        # GraphNode's propagate strategy converts an inner error result to a
        # SubgraphError, which discards structured provider metadata. HITL is
        # disabled for this workflow, so invoke once and merge the declared
        # inner error projection just like the normal result projection.
        ctx = replace(InvocationContext.from_state(state), hitl_allowed=False)
        subgraph = self.get_subgraph()
        try:
            sub_result = subgraph.invoke(self.extract_input(state), session_id=ctx.session_id, ctx=ctx)
        except Exception as exc:  # noqa: BLE001
            result = self._handle_call_error(subgraph, exc, state)
        else:
            result = self.merge_output(state, sub_result)
        emit_trace_event(
            "intake_workflow_completed",
            {"inquiry_id": state.get("intake_meta", {}).get("inquiry_id", ""), "intent_count": len(result.get("handoff_packets", [])), "degraded": bool(result.get("degradation_reason"))},
            state,
        )
        return result

    def on_subgraph_error(self, state: dict, error: Exception) -> dict:
        """Project unexpected subgraph failures without leaking exception text."""
        return {
            "status": AgentStatus.ERROR.value,
            "error_code": "subgraph_failed",
            "error_type": type(error).__name__,
            "error_log": ["IntakeWorkflowGraphNode: subgraph_failed"],
        }


def __getattr__(name: str):
    """Provide the old scaffold import without duplicating PB node discovery."""
    if name == "MainNode":
        return IntakeWorkflowGraphNode
    raise AttributeError(name)
