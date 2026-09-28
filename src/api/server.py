"""HTTP adapter for the non-HITL, non-persistent intake agent."""

from __future__ import annotations

import secrets
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from framework.schemas.invocation_context import InvocationContext
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from src.entry_adapter import AuthSource, Operation, authenticate
from src.graph.graph import JAMemberInquiryIntakeAgent


class InvokeContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_channel: str = "unspecified"
    business_date: str | None = None
    expected_policy_version: str | None = None


class InvokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: str
    session_id: str = ""
    context: InvokeContext = Field(default_factory=InvokeContext)


app = FastAPI(title="JAMemberInquiryIntakeAgent")
agent: JAMemberInquiryIntakeAgent | None = None


@app.on_event("startup")
def startup() -> None:
    """Build once so policy/config wiring errors surface before serving traffic."""
    global agent
    agent = JAMemberInquiryIntakeAgent()
    agent.compile()
    agent.provision_secrets(secrets_factory(namespace="svc", agent_name="JAMemberInquiryIntakeAgent"))
    app.state.agent_config = dict(agent.config)


def _agent() -> JAMemberInquiryIntakeAgent:
    if agent is None:
        startup()
    assert agent is not None
    return agent


def _ctx(request: Request, session_id: str) -> tuple[InvocationContext, str]:
    """Build trusted context for this read-only, request-scoped intake.

    This agent has no KB or persistent storage and creates no durable business
    state, so there is no customer-internal data partition to bind. Therefore
    ``SCOPE_KEYS = ()`` is intentional; dummy production-company/project scope
    keys must not be reintroduced.
    """
    auth = authenticate(request, operation=Operation.READ_INVOKE)
    caller_id = auth.principal["caller_id"]
    if not isinstance(caller_id, str) or not caller_id or len(caller_id) > 64:
        raise HTTPException(status_code=403, detail="Verified caller identity is invalid.")
    caller_ref = "invoke-standalone" if auth.auth_source is AuthSource.TOKEN else caller_id
    return (
        InvocationContext(
            session_id=session_id,
            caller_trust_level=auth.trust,
            caller_id=caller_id,
        ),
        caller_ref,
    )


def _letters(prefix: str) -> str:
    return prefix + "-" + "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(8))


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request):
    session_id = req.session_id or _letters("sess")
    ctx, caller_ref = _ctx(request, session_id)
    service = _agent()
    context = req.context
    if context.source_channel not in {"chat", "line", "phone", "counter", "unspecified"}:
        raise HTTPException(status_code=422, detail="context.source_channel is invalid")
    if context.business_date is not None:
        try:
            datetime.strptime(context.business_date, "%Y-%m-%d")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="context.business_date is invalid") from exc
        business_date = context.business_date
    else:
        business_date = datetime.now(ZoneInfo(str(service.config["business_timezone"]))).date().isoformat()
    meta = {
        "inquiry_id": _letters("inq"),
        "source_channel": context.source_channel,
        "caller_ref": caller_ref,
        "business_date": business_date,
    }
    if context.expected_policy_version is not None:
        meta["expected_policy_version"] = context.expected_policy_version
    with bound_secrets(service._secrets_provider):
        return service.invoke(req.input, ctx=ctx, input_context={"intake_meta": meta})


@app.get("/health")
def health() -> dict:
    """Deploy 検証用の liveness エンドポイント（scaffold seed 準拠）。

    `scripts/stg_invoke_evidence.py` が `GET /health` の 200 で `health_200` assertion と
    `deploy_exit_code` を判定するため、これが無いと deploy-stg は構造的に FAIL する。
    認証は掛けない（起動確認のみで、業務データを返さない）。
    """
    return {"status": "ok", "agent": "JAMemberInquiryIntakeAgent"}
