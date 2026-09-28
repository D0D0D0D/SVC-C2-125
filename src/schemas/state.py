"""AgentCore Platform v1.0"""

# ADR-005: State must be a flat TypedDict (see ADR-005 for the prohibited
# alternatives). LangGraph checkpoints use msgpack serialization, so only
# plain serializable fields are allowed. Do NOT add credentials or secrets.

from typing import NotRequired

from framework.schemas.agent_state import AgentState


class State(AgentState, total=False):
    """Agent state.

    Add only fields specific to this agent below.
    All shared fields (user_input, status, session_id, node_history,
    error_log, hitl_*, etc.) are inherited from AgentState.
    """

    intake_meta: NotRequired[dict]
    mode: NotRequired[str]
    gate_kind: NotRequired[str]
    seasonal_context: NotRequired[dict]
    ranking_basis: NotRequired[dict]
    decomposed_intents: NotRequired[list[dict]]
    handoff_packets: NotRequired[list[dict]]
    coverage_gaps: NotRequired[list[str]]
    degradation_reason: NotRequired[str]
    provenance_verified: NotRequired[bool]
    error_code: NotRequired[str]
    error_type: NotRequired[str]
