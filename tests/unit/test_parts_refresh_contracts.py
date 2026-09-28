"""Target-side regression coverage for the common-parts refresh."""

from __future__ import annotations

import inspect

import pytest
from fastapi import FastAPI, HTTPException, Request
from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.api.server import _ctx
from src.entry_adapter import Operation, SCOPE_KEYS, STG_DEFAULT_SCOPE_ENVS, authenticate
from src.evidence_gate import validate_provenance
from src.nodes.inner_nodes import IntentDecomposeExtractNode


def _request_with_bearer(token: str) -> Request:
    return Request(
        {
            "type": "http",
            "app": FastAPI(),
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "state": {},
        }
    )


def _request_with_middleware_caller(caller_id) -> Request:
    return Request(
        {
            "type": "http",
            "app": FastAPI(),
            "headers": [],
            "state": {"trust_level": TrustLevel.VERIFIED_EXTERNAL, "caller_id": caller_id},
        }
    )


def test_scopeless_entry_uses_public_auth_context(monkeypatch) -> None:
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")

    ctx, caller_ref = _ctx(_request_with_bearer("mock-entry-token"), "session-1")

    assert SCOPE_KEYS == ()
    assert STG_DEFAULT_SCOPE_ENVS == {}
    assert ctx.caller_trust_level is TrustLevel.VERIFIED_EXTERNAL
    assert ctx.caller_id == "invoke-token:standalone"
    assert caller_ref == "invoke-standalone"
    assert "no KB or persistent storage" in (inspect.getdoc(_ctx) or "")
    assert "SCOPE_KEYS = ()" in (inspect.getdoc(_ctx) or "")


def test_scopeless_entry_does_not_weaken_authentication(monkeypatch) -> None:
    monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    request = _request_with_bearer("unconfigured-token")

    with pytest.raises(HTTPException) as auth_exc_info:
        authenticate(request, operation=Operation.READ_INVOKE)

    with pytest.raises(HTTPException) as exc_info:
        _ctx(request, "session-1")

    assert auth_exc_info.value.status_code == 403
    assert exc_info.value.status_code == 403


@pytest.mark.parametrize(
    "caller_id",
    ["", 123, "x" * 65, " caller-id ", "caller\nid"],
    ids=["empty", "non-string", "65-chars", "surrounding-whitespace", "newline"],
)
def test_ctx_rejects_invalid_middleware_caller_id(caller_id) -> None:
    with pytest.raises(HTTPException) as exc_info:
        _ctx(_request_with_middleware_caller(caller_id), "session-1")

    assert exc_info.value.status_code == 403


@pytest.mark.parametrize(
    "evidence_map",
    [
        [{"claim_id": "C-1", "source_ids": ""}],
        ["not-a-claim"],
        [{"claim_id": "C-1", "source_ids": [], "asserted_attrs": []}],
    ],
)
def test_evidence_gate_rejects_malformed_evidence_map(evidence_map) -> None:
    payload = {
        "gate_kind": "draft",
        "status": AgentStatus.SUCCESS.value,
        "cited_source_ids": [],
        "retrieved_records": [],
        "evidence_map": evidence_map,
    }

    with pytest.raises(SecurityViolationError):
        validate_provenance(payload)


class ContractOnlyLlm:
    """Fake intentionally implements only BaseLLM.complete(messages)."""

    def __init__(self) -> None:
        self.messages: list | None = None

    def complete(self, messages: list) -> dict:
        self.messages = messages
        return {"content": '{"intents": []}'}


class FailingLlm:
    def complete(self, messages: list) -> dict:
        raise TimeoutError("provider-specific detail must not degrade to success")


def test_llm_call_uses_basellm_complete_messages_only() -> None:
    llm = ContractOnlyLlm()
    node = IntentDecomposeExtractNode(config={"llm": llm}, snapshot=None)

    result = node.execute({"validated_input": "営農相談"})

    assert result["status"] == AgentStatus.SUCCESS.value
    assert isinstance(llm.messages, list)
    assert len(llm.messages) == 1
    assert llm.messages[0]["role"] == "user"
    assert isinstance(llm.messages[0]["content"], str)


def test_provider_failure_is_error_while_none_still_degrades() -> None:
    failed = IntentDecomposeExtractNode(config={"llm": FailingLlm()}, snapshot=None).execute(
        {"validated_input": "営農相談"}
    )
    degraded = IntentDecomposeExtractNode(config={"llm": None}, snapshot=None).execute({"validated_input": "営農相談"})

    assert failed["status"] == AgentStatus.ERROR.value
    assert failed["error_code"] == "llm_call_failed"
    assert "draft" not in failed
    assert degraded["status"] == AgentStatus.SUCCESS.value
    assert degraded["degradation_reason"] == "llm_unavailable"
