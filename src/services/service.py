"""AgentCore Platform v1.0"""

# Service layer: domain queries, external API wrappers, data aggregation.
# Must NOT contain business logic, routing, or credentials.
# Nodes call this; this calls shared/services/ for external integrations.

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG: dict[str, Any] = {
    "max_retry": 3,
    "timeout_s": 45,
    "memory_enabled": False,
    "hitl": {"enabled": False},
    "max_inquiry_chars": 4000,
    "business_timezone": "Asia/Tokyo",
    "max_intents": 8,
    "max_facts_per_intent": 8,
    "max_questions_per_intent": 5,
    "min_span_chars": 2,
    "max_span_chars": 200,
    "max_question_chars": 120,
    "max_output_bytes": 65536,
    "routing_policy_path": "config/routing_policy.sample.yaml",
    "seasonal_calendar_path": "config/seasonal_calendar.sample.yaml",
    "llm_temperature": 0.0,
    "llm_max_tokens": 4096,
}


def load_runtime_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load the local runtime configuration without adding deployment dependencies."""
    config = dict(DEFAULT_CONFIG)
    path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            config.update(loaded)
    if overrides:
        config.update(overrides)
    return config


def load_yaml_mapping(path_value: str) -> tuple[dict[str, Any] | None, bytes | None]:
    """Read a customer-supplied policy input at the explicit file boundary."""
    path = Path(path_value)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    try:
        raw = path.read_bytes()
        loaded = yaml.safe_load(raw) or {}
    except (OSError, yaml.YAMLError):
        return None, None
    return (loaded if isinstance(loaded, dict) else None), raw
