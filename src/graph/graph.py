"""Outer AgentBaseGraph for JAMemberInquiryIntakeAgent."""

from __future__ import annotations

from typing import Any

from framework.graph.agent_base_graph import AgentBaseGraph

from src.nodes.main_node import IntakeWorkflowGraphNode
from src.nodes.post_process_node import IntakePacketEnvelopeNode
from src.nodes.pre_process_node import InquiryIntakeValidateNode
from src.schemas.state import State
from src.services.service import load_runtime_config


class JAMemberInquiryIntakeAgent(AgentBaseGraph):
    """Fixed outer pipeline around the three-stage JA inquiry workflow."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(load_runtime_config(config))

    @property
    def name(self) -> str:
        return "JAMemberInquiryIntakeAgent"

    @property
    def state_schema(self) -> type:
        return State

    def _extra_initial_state(self) -> dict:
        return {"mode": "invoke"}

    def register_nodes(self) -> None:
        super().register_nodes()
        self._nodes["pre_process"] = InquiryIntakeValidateNode(config=self.config)
        self._nodes["main"] = IntakeWorkflowGraphNode(config=self.config)
        self._nodes["post_process"] = IntakePacketEnvelopeNode(config=self.config)

    def get_output(self, state: State) -> dict:
        """Expose stable structured failure metadata at the public boundary."""
        output = super().get_output(state)
        output.update(
            error_code=state.get("error_code"),
            error_type=state.get("error_type"),
            error_log=list(dict.fromkeys(state.get("error_log", []))),
        )
        return output


# Legacy scaffold name retained for local callers generated before MR-B.
Graph = JAMemberInquiryIntakeAgent
