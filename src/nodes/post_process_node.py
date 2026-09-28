"""Output envelope, deterministic truncation, and the S-3 closed gate."""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.security import detect_pii
from shared.utils.audit_logger import emit_trace_event
from src.nodes.inner_nodes import _FIELDS, _FALLBACK_MISSING, _normalize_fact_value, _packet_spans_valid, _rank_key, policy_snapshot, unavailable_basis
from src.output_envelope import OutputEnvelopeNode


_CAPABILITIES = {"pest_disease_consultation", "machinery_service", "procurement", "sales_shipment", "subsidy_program", "other_general"}
_CAPABILITY_TOKEN = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
_CALLER_REF = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_INQUIRY_ID = re.compile(r"^inq-[a-z]{8}$")
_PACKET_ID = re.compile(r"^pkt-[a-z]{8}$")
_FREE_TEXT_PATHS = (
    ("packets", "*", "missing_information", "*", "staff_action"),
    ("packets", "*", "missing_information", "*", "why_needed"),
    ("packets", "*", "extracted_facts", "*", "value"),
    ("packets", "*", "intent", "source_span"),
    ("packets", "*", "routing", "rationale"),
    ("packets", "*", "provenance", "caller_ref"),
)


class IntakePacketEnvelopeNode(OutputEnvelopeNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    allowed_modes: ClassVar[tuple[str, ...]] = ("invoke",)
    gate_kind_by_mode: ClassVar[dict[str, str]] = {"invoke": "handoff_packets"}
    required_fields_by_mode: ClassVar[dict[str, tuple[str, ...]]] = {
        "invoke": ("schema_version", "inquiry_id", "text_basis", "packets", "provenance_verified")
    }

    def __init__(self, *, config: dict[str, Any] | None = None, snapshot: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})
        self._policy_snapshot = snapshot if snapshot is not None else policy_snapshot(self._config)

    def format_invoke(self, state: dict) -> dict[str, Any]:
        packets, gaps = self._truncate_packets(list(state.get("handoff_packets", [])), list(state.get("coverage_gaps", [])), state)
        return {
            "schema_version": "1.0.0",
            "inquiry_id": state.get("intake_meta", {}).get("inquiry_id", ""),
            "text_basis": "canonical_text",
            "packets": packets,
            "coverage_gaps": gaps,
            "provenance_verified": bool(state.get("provenance_verified", False)),
            "degradation_reason": state.get("degradation_reason", ""),
        }

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("upstream_error_passthrough", {"node": "post_process"}, state)
            return super().execute(state)
        if state.get("mode") != "invoke":
            return {"status": AgentStatus.ERROR.value, "error_log": ["post_process requires invoke mode"]}
        if not self._ranks_match_policy(state):
            emit_trace_event("output_gate_rejected", {"path": "routing.rank", "reason": "sort_mismatch"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["packet rank does not match deterministic policy sort"]}
        result = super().execute(state)
        result.pop("_output_envelope_hook_state", None)
        result["_verification_basis"] = str(state.get("validated_input", ""))
        result["_normalization_business_date"] = str(state.get("intake_meta", {}).get("business_date", ""))
        formatted = result.get("formatted_output", {})
        emit_trace_event(
            "handoff_packets_emitted",
            {"packet_count": len(formatted.get("packets", [])), "provenance_verified": formatted.get("provenance_verified", False), "coverage_gap_count": len(formatted.get("coverage_gaps", []))},
            state,
        )
        return result

    def _ranks_match_policy(self, state: dict) -> bool:
        packets = state.get("handoff_packets", [])
        if not isinstance(packets, list):
            return False
        ordered = sorted((packet for packet in packets if isinstance(packet, dict)), key=lambda packet: _rank_key(packet, state))
        return len(ordered) == len(packets) and all(packet.get("routing", {}).get("rank") == index for index, packet in enumerate(ordered, start=1))

    def _truncate_packets(self, packets: list[Any], gaps: list[str], state: dict) -> tuple[list[dict], list[str]]:
        valid_packets = [packet for packet in packets if isinstance(packet, dict)]
        valid_packets.sort(key=lambda packet: packet.get("routing", {}).get("rank", 10**9))
        maximum = int(self._config.get("max_intents", 8))
        if len(valid_packets) > maximum:
            dropped = len(valid_packets) - maximum
            valid_packets = valid_packets[:maximum]
            gaps.append(f"truncated:packets:{dropped}")
            emit_trace_event("output_truncated", {"field": "packets", "dropped_count": dropped}, state)
        span_limit = int(self._config.get("max_span_chars", 200))
        question_limit = int(self._config.get("max_question_chars", 120))
        pruned: list[dict] = []
        for packet in valid_packets:
            if len(str(packet.get("intent", {}).get("source_span", ""))) > span_limit:
                gaps.append("truncated:intent_span:1")
                continue
            packet = dict(packet)
            packet["extracted_facts"] = [fact for fact in packet.get("extracted_facts", []) if len(str(fact.get("source_span", ""))) <= span_limit]
            packet["missing_information"] = [item for item in packet.get("missing_information", []) if len(str(item.get("staff_action", ""))) <= question_limit and len(str(item.get("why_needed", ""))) <= question_limit]
            pruned.append(packet)
        output = self._envelope_for_size(pruned, gaps, state)
        while pruned and len(json.dumps(output, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > int(self._config.get("max_output_bytes", 65536)):
            pruned.pop()
            gaps.append("truncated:output_bytes:1")
            output = self._envelope_for_size(pruned, gaps, state)
            emit_trace_event("output_truncated", {"field": "output_bytes", "dropped_count": 1}, state)
        return pruned, gaps

    def _envelope_for_size(self, packets: list[dict], gaps: list[str], state: dict) -> dict:
        return {"schema_version": "1.0.0", "inquiry_id": state.get("intake_meta", {}).get("inquiry_id", ""), "text_basis": "canonical_text", "packets": packets, "coverage_gaps": gaps, "provenance_verified": bool(state.get("provenance_verified", False)), "degradation_reason": state.get("degradation_reason", "")}

    def _extra_security_gate_output(self, result: dict) -> dict:
        # Verification must precede allowlist checking, because the private basis
        # is intentionally not a client-visible output field.
        if result.get("status") == AgentStatus.ERROR.value:
            return result
        basis = result.pop("_verification_basis", None)
        business_date_raw = result.pop("_normalization_business_date", None)
        if not isinstance(basis, str):
            self._reject("verification_basis", "missing")
        formatted = result.get("formatted_output")
        if not isinstance(formatted, dict):
            self._reject("formatted_output", "missing")
        try:
            business_date = date.fromisoformat(business_date_raw) if business_date_raw else None
        except ValueError:
            self._reject("normalization_business_date", "invalid")
        if formatted.get("provenance_verified") is not True:
            self._reject("provenance_verified", "must_be_true")
        self._verify_spans(formatted, basis)
        self._validate_structure(formatted)
        self._validate_policy_and_values(formatted, business_date)
        self._validate_free_text(formatted)
        # The generic envelope adds routing metadata for its own contract. It is
        # removed only after all validation so the public envelope remains §4-2.
        formatted.pop("mode", None)
        formatted.pop("gate_kind", None)
        result["formatted_output"] = formatted
        return result

    def _verify_spans(self, formatted: dict, basis: str) -> None:
        minimum = int(self._config.get("min_span_chars", 2))
        for packet in formatted.get("packets", []):
            if not isinstance(packet, dict) or not _packet_spans_valid(packet, basis, minimum):
                self._reject("packets", "provenance")

    def _validate_structure(self, formatted: dict) -> None:
        allowed = {"schema_version", "inquiry_id", "text_basis", "packets", "coverage_gaps", "provenance_verified", "degradation_reason", "mode", "gate_kind"}
        if set(formatted) - allowed or not {"schema_version", "inquiry_id", "text_basis", "packets", "provenance_verified"} <= set(formatted):
            self._reject("formatted_output", "allowlist")
        if formatted.get("schema_version") != "1.0.0" or formatted.get("text_basis") != "canonical_text" or not isinstance(formatted.get("packets"), list):
            self._reject("formatted_output", "schema")
        if not isinstance(formatted.get("inquiry_id"), str) or not _INQUIRY_ID.fullmatch(formatted["inquiry_id"]):
            self._reject("inquiry_id", "invalid")
        packet_keys = {"packet_id", "intent", "routing", "ranking_basis", "extracted_facts", "missing_information", "provenance"}
        for packet in formatted["packets"]:
            if not isinstance(packet, dict) or set(packet) != packet_keys:
                self._reject("packets", "allowlist")
            self._exact_keys(packet.get("intent"), {"intent_id", "type", "source_span", "span_offsets", "confidence", "related_intent_ids"}, "intent")
            self._exact_keys(packet.get("routing"), {"capability", "rationale", "seasonal_priority", "rank"}, "routing")
            self._exact_keys(packet.get("ranking_basis"), {"ranking_mode", "policy_id", "policy_version", "policy_digest", "seasonal_calendar_version", "mapping_rule_id"}, "ranking_basis")
            self._exact_keys(packet.get("provenance"), {"source_channel", "caller_ref"}, "provenance")
            for fact in packet.get("extracted_facts", []):
                self._exact_keys(fact, {"field", "value", "source_span", "span_offsets", "confidence"}, "extracted_facts")
            for missing in packet.get("missing_information", []):
                self._exact_keys(missing, {"missing_field", "ask_target", "staff_action", "why_needed"}, "missing_information")

    def _exact_keys(self, value: Any, expected: set[str], path: str) -> None:
        if not isinstance(value, dict) or set(value) != expected:
            self._reject(path, "allowlist")

    def _validate_policy_and_values(self, formatted: dict, business_date: date | None) -> None:
        packets = formatted["packets"]
        encoded = json.dumps(formatted, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > int(self._config.get("max_output_bytes", 65536)):
            self._reject("formatted_output", "volume")
        ranks = [packet["routing"].get("rank") for packet in packets]
        if ranks != list(range(1, len(packets) + 1)):
            self._reject("routing.rank", "sequence")
        for packet in packets:
            intent, routing, basis = packet["intent"], packet["routing"], packet["ranking_basis"]
            if not isinstance(packet.get("packet_id"), str) or not _PACKET_ID.fullmatch(packet["packet_id"]):
                self._reject("packet_id", "invalid")
            if intent.get("confidence") not in {"high", "medium", "low"} or routing.get("seasonal_priority") not in {"critical", "high", "normal", "low"}:
                self._reject("enum", "invalid")
            capability = routing.get("capability")
            if capability not in _CAPABILITIES and (not isinstance(capability, str) or not _CAPABILITY_TOKEN.fullmatch(capability)):
                self._reject("routing.capability", "invalid")
            if self._policy_snapshot is None:
                if basis != unavailable_basis() or intent.get("type") != "other_general" or capability != "other_general":
                    self._reject("ranking_basis", "unavailable_mode")
            else:
                expected = self._policy_snapshot
                if basis.get("ranking_mode") != "policy_applied" or any(basis.get(key) != expected[key] for key in ("policy_id", "policy_version", "policy_digest", "seasonal_calendar_version")) or basis.get("mapping_rule_id") not in expected["mapping_rule_ids"]:
                    self._reject("ranking_basis", "snapshot_mismatch")
                if intent.get("type") not in expected["intent_types"]:
                    self._reject("intent.type", "invalid")
            if len(packet["extracted_facts"]) > int(self._config.get("max_facts_per_intent", 8)) or len(packet["missing_information"]) > int(self._config.get("max_questions_per_intent", 5)):
                self._reject("packets", "volume")
            for fact in packet["extracted_facts"]:
                field = fact.get("field")
                source_span = str(fact.get("source_span", ""))
                derived = _normalize_fact_value(field, source_span, business_date) if isinstance(field, str) and business_date else None
                if field not in _FIELDS or fact.get("confidence") not in {"high", "medium", "low"} or fact.get("value") not in {source_span, derived} or len(source_span) > int(self._config.get("max_span_chars", 200)):
                    self._reject("extracted_facts", "invalid")
            for missing in packet["missing_information"]:
                if missing.get("missing_field") not in _FIELDS | {"unspecified"} or missing.get("ask_target") not in {"member", "internal_record"}:
                    self._reject("missing_information", "enum")
                action = missing.get("staff_action")
                why_needed = missing.get("why_needed")
                if not isinstance(action, str) or not action or "。" in action or len(action) > int(self._config.get("max_question_chars", 120)) or not isinstance(why_needed, str) or len(why_needed) > int(self._config.get("max_question_chars", 120)):
                    self._reject("missing_information.staff_action", "invalid")
                if self._policy_snapshot is None and missing != _FALLBACK_MISSING:
                    self._reject("missing_information", "fallback_contract")
            if not isinstance(packet["provenance"].get("caller_ref"), str) or not _CALLER_REF.fullmatch(packet["provenance"]["caller_ref"]):
                self._reject("provenance.caller_ref", "invalid")
        if len(packets) > int(self._config.get("max_intents", 8)):
            self._reject("packets", "volume")

    def _validate_free_text(self, formatted: dict) -> None:
        for path in _FREE_TEXT_PATHS:
            for value in _values_at_path(formatted, path):
                if not isinstance(value, str) or detect_pii(value):
                    self._reject(".".join(path), "pii")

    def _reject(self, path: str, reason: str) -> None:
        emit_trace_event("output_gate_rejected", {"path": path, "reason": reason}, {})
        raise SecurityViolationError(f"S-3 output gate rejected {path}: {reason}")


def _values_at_path(node: Any, path: tuple[str, ...]):
    if not path:
        yield node
        return
    head, *tail = path
    if head == "*" and isinstance(node, list):
        for item in node:
            yield from _values_at_path(item, tuple(tail))
    elif isinstance(node, dict) and head in node:
        yield from _values_at_path(node[head], tuple(tail))
