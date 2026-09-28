"""Input validation and canonical-text construction for the intake workflow."""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.security import detect_pii
from shared.utils.audit_logger import emit_trace_event


_CHANNELS = {"chat", "line", "phone", "counter", "unspecified"}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class InquiryIntakeValidateNode(FunctionNode):
    """Validate adapter-populated metadata and establish immutable canonical text."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})

    def _extra_security_gate_input(self, state: dict) -> dict:
        text = state.get("user_input")
        meta = state.get("intake_meta") or state.get("input_context", {}).get("intake_meta")
        errors: list[str] = []
        if not isinstance(text, str) or not text.strip():
            errors.append("input must be a non-empty string")
        elif len(text) > int(self._config.get("max_inquiry_chars", 4000)):
            errors.append("input exceeds max_inquiry_chars")
        elif not _readable_text(text):
            errors.append("input text quality gate rejected the text")
        if not isinstance(meta, dict):
            errors.append("intake_meta is missing")
        else:
            for key in ("inquiry_id", "source_channel", "caller_ref", "business_date"):
                if not meta.get(key):
                    errors.append(f"intake_meta.{key} is required")
            if meta.get("source_channel") not in _CHANNELS:
                errors.append("intake_meta.source_channel is invalid")
            if not isinstance(meta.get("business_date"), str) or not _DATE_RE.fullmatch(meta["business_date"]):
                errors.append("intake_meta.business_date is invalid")
            else:
                try:
                    date.fromisoformat(meta["business_date"])
                except ValueError:
                    errors.append("intake_meta.business_date is invalid")
            expected = meta.get("expected_policy_version")
            if expected is not None and (not isinstance(expected, str) or not expected.strip()):
                errors.append("intake_meta.expected_policy_version is invalid")
        if errors:
            # Keep S-2 structurally non-bypassing while letting execute return
            # the domain error through the normal S-3/audit sequence.
            return {**state, "_pre_validation_errors": errors}
        return state

    def execute(self, state: dict) -> dict:
        if state.get("_pre_validation_errors"):
            emit_trace_event("inquiry_intake_validation_rejected", {"reason_count": len(state["_pre_validation_errors"])}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": list(state["_pre_validation_errors"])}
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("upstream_error_passthrough", {"node": "pre_process"}, state)
            return {"status": AgentStatus.ERROR.value}
        normalized = unicodedata.normalize("NFKC", str(state.get("user_input", "")).strip())
        # The second scan is deliberately after NFKC.  Replace backwards so the
        # detector offsets remain valid while canonical_text is being built.
        for finding in reversed(detect_pii(normalized)):
            normalized = normalized[: finding["start"]] + "[MASKED]" + normalized[finding["end"] :]
        meta = dict(state.get("intake_meta") or state.get("input_context", {}).get("intake_meta", {}))
        emit_trace_event(
            "inquiry_intake_validated",
            {
                "inquiry_id": meta["inquiry_id"],
                "source_channel": meta["source_channel"],
                "text_length": len(normalized),
                "quality_gate": "passed",
            },
            state,
        )
        return {"validated_input": normalized, "intake_meta": meta, "status": AgentStatus.SUCCESS.value}


def _readable_text(text: str) -> bool:
    if not text:
        return False
    controls = sum(1 for char in text if unicodedata.category(char) == "Cc")
    replacements = text.count("\ufffd")
    readable = sum(
        1
        for char in text
        if char.isspace() or char.isalnum() or "\u3040" <= char <= "\u30ff" or "\u3400" <= char <= "\u9fff" or char in "。、！？・ー（）()、,.!?-:/"
    )
    return controls / len(text) <= 0.02 and replacements / len(text) <= 0.01 and readable / len(text) >= 0.7

