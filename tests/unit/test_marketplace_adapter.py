"""AGENTIC STAR Marketplace adapter boundary tests."""

from __future__ import annotations

import json
import sys
from types import ModuleType

import pytest

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import MissingSecret, NullProvider
from framework.secrets.context import bound_secrets

import cli
from src.graph.graph import JAMemberInquiryIntakeAgent


def _set_required_environment(monkeypatch, suffix: str = "one") -> dict[str, str]:
    values = {key: f"test-{suffix}-{key.lower()}" for key in cli._required_secret_keys()}
    # The startup gate requires a bare Azure resource endpoint, so
    # this value cannot be a placeholder like the other secrets.
    values["AZURE_OPENAI_ENDPOINT"] = f"https://{suffix}.services.ai.azure.example"
    values["AZURE_OPENAI_ENDPOINT"] = f"https://{suffix}.azure.example"
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def test_runner_context_is_rebuilt_and_history_never_enters_state(monkeypatch):
    _set_required_environment(monkeypatch)
    captured = {}

    def parent_invoke(self, user_input, session_id="", ctx=None, input_context=None):
        captured.update(user_input=user_input, ctx=ctx, input_context=input_context)
        return {"status": "success"}

    monkeypatch.setattr(JAMemberInquiryIntakeAgent, "invoke", parent_invoke)
    runner_context = {
        "conversation_history": [{"role": "user", "content": "member@example.jp"}],
        "intake_meta": {"caller_ref": "attacker-controlled"},
        "mode": "ingest",
        "future_runner_key": "untrusted",
    }
    result = cli.MarketplaceGraph().invoke(
        "組合員からの問い合わせ",
        ctx=InvocationContext(
            caller_id="member@example.jp",
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        ),
        input_context=runner_context,
    )

    assert result == {"status": "success"}
    assert captured["user_input"] == "組合員からの問い合わせ"
    assert set(captured["input_context"]) == {"intake_meta"}
    assert captured["input_context"]["intake_meta"]["caller_ref"] == "marketplace"
    assert "member@example.jp" not in repr(captured["input_context"])
    assert captured["input_context"] is not runner_context


def test_marketplace_disables_hitl_at_boundary(monkeypatch):
    _set_required_environment(monkeypatch)
    captured = {}

    def parent_invoke(self, user_input, session_id="", ctx=None, input_context=None):
        del self, user_input, session_id, input_context
        captured["ctx"] = ctx
        return {"status": "success"}

    monkeypatch.setattr(JAMemberInquiryIntakeAgent, "invoke", parent_invoke)
    original_ctx = InvocationContext(
        caller_id="marketplace-user",
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        hitl_allowed=True,
    )
    cli.MarketplaceGraph().invoke("問い合わせ", ctx=original_ctx)

    assert captured["ctx"] is not original_ctx
    assert captured["ctx"].caller_trust_level is TrustLevel.VERIFIED_EXTERNAL
    assert captured["ctx"].hitl_allowed is False


def test_marketplace_generated_state_completes_invoke_without_write_mode(monkeypatch):
    def fake_complete(self, messages):
        del self, messages
        return {
            "content": json.dumps(
                {
                    "intents": [
                        {
                            "intent_id": "i-01",
                            "type": "other_general",
                            "source_span": "トマトの相談",
                            "occurrence_index": 0,
                            "confidence": "high",
                            "related_intent_ids": [],
                            "extracted_facts": [],
                            "missing_information": [],
                        }
                    ]
                },
                ensure_ascii=False,
            )
        }

    monkeypatch.setattr(cli._AzureMarketplaceLLM, "complete", fake_complete)
    agent = cli.MarketplaceGraph()
    agent.compile()
    result = agent.invoke(
        "トマトの相談",
        ctx=InvocationContext(
            caller_id="member@example.jp",
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
            hitl_allowed=True,
        ),
        input_context={"conversation_history": [{"content": "untrusted"}], "mode": "ingest"},
    )

    assert result["status"] == "success"
    assert result["output"]["inquiry_id"].startswith("inq-")
    assert result["output"]["packets"][0]["provenance"] == {
        "source_channel": "unspecified",
        "caller_ref": "marketplace",
    }
    assert "mode" not in result["output"]


@pytest.mark.parametrize("invalid", [None, "", "   ", " padded", "padded ", "has space", "line\nbreak"])
def test_scope_fails_closed_during_startup(monkeypatch, invalid):
    monkeypatch.setattr(cli, "SCOPE_KEYS", ("member_scope",))
    monkeypatch.setattr(cli, "STG_DEFAULT_SCOPE_ENVS", {"member_scope": "STG_DEFAULT_MEMBER_SCOPE"})
    _set_required_environment(monkeypatch)
    if invalid is None:
        monkeypatch.delenv("STG_DEFAULT_MEMBER_SCOPE")
    else:
        monkeypatch.setenv("STG_DEFAULT_MEMBER_SCOPE", invalid)

    agent = cli.MarketplaceGraph()
    agent.compile()
    with pytest.raises((MissingSecret, ValueError), match="STG_DEFAULT_MEMBER_SCOPE"):
        agent.provision_secrets(NullProvider())


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_during_startup(monkeypatch, endpoint):
    """AzureOpenAIClient appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces inside complete(); a node that swallows it
    degrades silently while the invocation still reports status: success, so the
    adapter has to reject the shape at startup instead.
    """

    monkeypatch.setattr(cli, "SCOPE_KEYS", ("member_scope",))
    monkeypatch.setattr(cli, "STG_DEFAULT_SCOPE_ENVS", {"member_scope": "STG_DEFAULT_MEMBER_SCOPE"})
    _set_required_environment(monkeypatch)
    agent = cli.MarketplaceGraph()
    agent.compile()
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)

    with pytest.raises(ValueError, match="bare"):
        agent.provision_secrets(NullProvider())


@pytest.mark.parametrize("missing_key", cli._AZURE_OPENAI_SECRET_KEYS)
def test_missing_azure_value_fails_during_startup(monkeypatch, missing_key):
    _set_required_environment(monkeypatch)
    monkeypatch.delenv(missing_key)

    agent = cli.MarketplaceGraph()
    agent.compile()
    with pytest.raises(MissingSecret, match=missing_key):
        agent.provision_secrets(NullProvider())


def test_azure_llm_resolves_fresh_credentials_without_caching_client(monkeypatch):
    configs = []

    class FakeAzureOpenAIClient:
        def __init__(self, config):
            configs.append(dict(config))

        def complete(self, messages):
            return {"content": "ok", "messages": messages}

        def stream(self, messages):
            del messages
            yield "ok"

        def bind_tools(self, tools):
            del tools
            return self

    azure_module = ModuleType("shared.services.llm.azure_openai_client")
    azure_module.AzureOpenAIClient = FakeAzureOpenAIClient
    monkeypatch.setitem(sys.modules, azure_module.__name__, azure_module)
    llm = cli._AzureMarketplaceLLM()
    provider = cli._AllowlistedEnvironmentProvider()
    assert vars(llm) == {}

    with bound_secrets(provider):
        first = _set_required_environment(monkeypatch, "first")
        llm.complete([{"role": "user", "content": "first"}])
        assert vars(llm) == {}
        second = _set_required_environment(monkeypatch, "second")
        llm.complete([{"role": "user", "content": "second"}])
        assert vars(llm) == {}

    assert configs == [
        {
            "api_key": first["AZURE_OPENAI_API_KEY"],
            "azure_endpoint": first["AZURE_OPENAI_ENDPOINT"],
            "azure_deployment": first["AZURE_OPENAI_DEPLOYMENT"],
        },
        {
            "api_key": second["AZURE_OPENAI_API_KEY"],
            "azure_endpoint": second["AZURE_OPENAI_ENDPOINT"],
            "azure_deployment": second["AZURE_OPENAI_DEPLOYMENT"],
        },
    ]


def test_graph_injects_azure_before_compile():
    supplied_llm = object()
    agent = cli.MarketplaceGraph({"llm": supplied_llm, "memory_enabled": True, "hitl": {"enabled": True}})

    assert isinstance(agent.config["llm"], cli._AzureMarketplaceLLM)
    assert agent.config["llm"] is not supplied_llm
    assert agent.config["memory_enabled"] is False
    assert agent.config["hitl"] == {"enabled": False}
    agent.compile()
    assert agent._nodes["main"]._config["llm"] is agent.config["llm"]


def test_allowlisted_provider_hides_unrelated_environment(monkeypatch):
    values = _set_required_environment(monkeypatch)
    monkeypatch.setenv("UNRELATED_PLATFORM_SECRET", "must-not-be-visible")
    agent = cli.MarketplaceGraph()

    agent.provision_secrets(NullProvider())

    provider = agent._secrets_provider
    assert {key: provider.require(key) for key in cli._required_secret_keys()} == values
    assert provider.get("UNRELATED_PLATFORM_SECRET") is None


def test_main_uses_marketplace_graph_and_repository_config(monkeypatch):
    called = {}

    def fake_runner(agent_cls, **kwargs):
        called["agent_cls"] = agent_cls
        called.update(kwargs)

    monkeypatch.setattr(cli, "run_agent_marketplace", fake_runner)
    cli.main()

    assert called["agent_cls"] is cli.MarketplaceGraph
    assert called["agent_name"] == "SVC-C2-125"
    assert called["namespace"] == "svc"
    assert called["config"]["max_retry"] == 3
