"""AGENTIC STAR Marketplace entry adapter for SVC-C2-125."""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from framework.schemas.invocation_context import InvocationContext
from framework.secrets.base import SecretProvider
from framework.secrets.context import current_secrets

try:
    from framework.utils.config_loader import load_agent_config
except ImportError:  # AgentCore 1.0.0 / 1.0.1 compatibility
    from framework.utils.config_loader import load_config

    def load_agent_config(agent_dir: Path) -> dict[str, Any]:
        config_path = agent_dir / "config" / "config.yaml"
        return load_config(str(config_path)) if config_path.exists() else {}


try:
    from shared.bootstrap import run_agent_marketplace
except ModuleNotFoundError as exc:  # pragma: no cover - local SDK 1.0.0 has no Marketplace extra
    if exc.name != "shared.bootstrap":
        raise

    def run_agent_marketplace(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError("The installed AgentCore wheel does not include Marketplace support")


from shared.services.llm.base_llm import BaseLLM

from src.entry_adapter import SCOPE_KEYS, STG_DEFAULT_SCOPE_ENVS
from src.graph.graph import JAMemberInquiryIntakeAgent


_NAMESPACE = "svc"
_AGENT_NAME = "SVC-C2-125"
_PROVIDER_AGENT_NAME = "svc_c2_125"
_MARKETPLACE_SOURCE_CHANNEL = "unspecified"
_MARKETPLACE_CALLER_REF = "marketplace"

_AZURE_OPENAI_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)


def _validate_scope_declaration() -> None:
    """Fail closed when the vendored scope declaration is inconsistent."""

    if not isinstance(SCOPE_KEYS, tuple):
        raise RuntimeError("Marketplace SCOPE_KEYS must be a tuple")
    if any(not isinstance(key, str) or not key or key != key.strip() or not key.isidentifier() for key in SCOPE_KEYS):
        raise RuntimeError("Marketplace SCOPE_KEYS must contain clean identifier strings")
    if len(set(SCOPE_KEYS)) != len(SCOPE_KEYS):
        raise RuntimeError("Marketplace SCOPE_KEYS must not contain duplicates")
    if not isinstance(STG_DEFAULT_SCOPE_ENVS, Mapping) or set(STG_DEFAULT_SCOPE_ENVS) != set(SCOPE_KEYS):
        raise RuntimeError("Marketplace STG_DEFAULT_SCOPE_ENVS keys must exactly match SCOPE_KEYS")
    if any(
        not isinstance(env_name, str)
        or not env_name
        or env_name != env_name.strip()
        or any(character.isspace() for character in env_name)
        for env_name in STG_DEFAULT_SCOPE_ENVS.values()
    ):
        raise RuntimeError("Marketplace STG_DEFAULT_SCOPE_ENVS contains an invalid environment name")


def _required_secret_keys() -> tuple[str, ...]:
    """Resolve startup requirements from the current scope declaration."""

    _validate_scope_declaration()
    scope_env_keys = tuple(STG_DEFAULT_SCOPE_ENVS[key] for key in SCOPE_KEYS)
    return (*_AZURE_OPENAI_SECRET_KEYS, *scope_env_keys)


class _AllowlistedEnvironmentProvider(SecretProvider):
    """Expose only this single-agent container's declared environment values."""

    def __init__(self) -> None:
        self._namespace = _NAMESPACE
        self._agent_name = _PROVIDER_AGENT_NAME

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in frozenset(_required_secret_keys()):
            return default
        value = os.environ.get(key)
        return value if value else default


def _require_clean(provider: SecretProvider, key: str) -> str:
    """Resolve a required deployment value without repairing it."""

    value = provider.require(key)
    if not value or any(character.isspace() for character in value):
        raise ValueError(f"Marketplace environment variable {key!r} must be a clean non-empty string")
    return value


def _require_azure_endpoint(provider: SecretProvider) -> str:
    """Reject ``AZURE_OPENAI_ENDPOINT`` values that are not a bare Azure resource endpoint.

    ``AzureOpenAIClient`` appends its own ``/openai/v1/`` path, so anything
    beyond a scheme and host silently produces a broken request URL, and the
    failure surfaces inside ``complete()``. A node that swallows that exception
    degrades quietly while the invocation still reports ``status: success``,
    which is why this has to be a startup check. The value is reported here,
    never repaired.
    """

    endpoint = _require_clean(provider, "AZURE_OPENAI_ENDPOINT")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == -1
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "Marketplace environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint -- scheme and host only, with no path (such as "
            "'/openai'), query or fragment"
        )
    return endpoint


def _scope_from_environment(provider: SecretProvider) -> dict[str, str]:
    """Build every declared scope field from its STG default environment value."""

    _validate_scope_declaration()
    return {scope_key: _require_clean(provider, STG_DEFAULT_SCOPE_ENVS[scope_key]) for scope_key in SCOPE_KEYS}


class _AzureMarketplaceLLM(BaseLLM):  # type: ignore[misc]
    """Create an Azure client per operation and retain no connection values."""

    def __init__(self, *, tools: tuple[Any, ...] = ()) -> None:
        if tools:
            self._tools = tools

    def _new_client(self) -> BaseLLM:
        # Azure support is an optional deployment extra. Keep this provider
        # import method-local; framework/security imports stay on the main thread.
        try:
            from shared.services.llm.azure_openai_client import AzureOpenAIClient
        except ModuleNotFoundError as exc:
            if exc.name != "shared.services.llm.azure_openai_client":
                raise
            raise RuntimeError("The installed AgentCore wheel does not include Azure OpenAI support") from exc

        provider = current_secrets()
        client: BaseLLM = AzureOpenAIClient(
            {
                "api_key": _require_clean(provider, "AZURE_OPENAI_API_KEY"),
                "azure_endpoint": _require_azure_endpoint(provider),
                "azure_deployment": _require_clean(provider, "AZURE_OPENAI_DEPLOYMENT"),
            }
        )
        tools = getattr(self, "_tools", ())
        return client.bind_tools(list(tools)) if tools else client

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._new_client().complete(messages))

    def stream(self, messages: list[Any]) -> Iterator[str]:
        yield from self._new_client().stream(messages)

    def bind_tools(self, tools: list[Any]) -> "_AzureMarketplaceLLM":
        return type(self)(tools=tuple(tools))


def _letters(prefix: str) -> str:
    return prefix + "-" + "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(8))


class MarketplaceGraph(JAMemberInquiryIntakeAgent):
    """Marketplace-only read boundary, trusted metadata, and Azure LLM wiring."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        # The runner compiles before provision_secrets(). Nodes must therefore
        # capture this credential-free, per-call facade during compile().
        marketplace_config = dict(config or {})
        marketplace_config["llm"] = _AzureMarketplaceLLM()
        marketplace_config["memory_enabled"] = False
        marketplace_config["hitl"] = {"enabled": False}
        super().__init__(config=marketplace_config)

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Runner history is untrusted free text outside this AG's S-2 scan.
        # Rebuild the complete context so no current or future runner key can
        # enter State or a checkpoint without PII/injection inspection.
        del input_context
        scope = _scope_from_environment(_AllowlistedEnvironmentProvider())
        timezone = ZoneInfo(str(self.config["business_timezone"]))
        marketplace_context = {
            **scope,
            "intake_meta": {
                "inquiry_id": _letters("inq"),
                "source_channel": _MARKETPLACE_SOURCE_CHANNEL,
                # Never copy caller_id: it may itself contain member PII.
                "caller_ref": _MARKETPLACE_CALLER_REF,
                "business_date": datetime.now(timezone).date().isoformat(),
            },
        }
        marketplace_ctx = replace(ctx, hitl_allowed=False) if ctx is not None else None
        return cast(
            dict[str, Any],
            super().invoke(
                user_input,
                session_id=session_id,
                ctx=marketplace_ctx,
                input_context=marketplace_context,
            ),
        )

    def provision_secrets(self, provider: SecretProvider) -> None:
        # Ignore the runner's broader/dotenv-capable provider. This Pod reads
        # only values explicitly allowlisted by this adapter.
        del provider
        environment_provider = _AllowlistedEnvironmentProvider()
        _scope_from_environment(environment_provider)
        for key in _AZURE_OPENAI_SECRET_KEYS:
            _require_clean(environment_provider, key)
        _require_azure_endpoint(environment_provider)
        super().provision_secrets(environment_provider)


def main() -> None:
    run_agent_marketplace(
        MarketplaceGraph,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config=load_agent_config(Path(__file__).resolve().parent),
    )


if __name__ == "__main__":
    main()
