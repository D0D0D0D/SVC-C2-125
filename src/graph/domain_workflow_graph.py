"""Inner fixed workflow for JA member inquiry intake."""

from __future__ import annotations

from framework.graph.agent_base_graph import AgentBaseGraph

from src.nodes.inner_nodes import HandoffRankNode, IntentDecomposeExtractNode, SeasonalContextNode, policy_snapshot
from src.schemas.state import State


class IntakeWorkflowGraph(AgentBaseGraph):
    """Three-stage domain workflow: calendar, LLM draft, deterministic ranking."""

    @property
    def name(self) -> str:
        return "JAMemberInquiryIntakeInnerWorkflow"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()
        snapshot = self.config.get("policy_snapshot")
        if snapshot is None:
            snapshot = policy_snapshot(self.config)
        self._nodes["pre_process"] = SeasonalContextNode(config=self.config, snapshot=snapshot)
        self._nodes["main"] = IntentDecomposeExtractNode(config=self.config, snapshot=snapshot)
        self._nodes["post_process"] = HandoffRankNode(config=self.config, snapshot=snapshot)

    def get_output(self, state: State) -> dict:
        """Expose only the inner schema fields consumed by the GraphNode adapter."""
        keys = (
            "handoff_packets",
            "coverage_gaps",
            "degradation_reason",
            "provenance_verified",
            "status",
            "error_code",
            "error_type",
            "error_log",
        )
        return {key: state[key] for key in keys if key in state}
