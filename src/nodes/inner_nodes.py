"""The deterministic/LLM/deterministic stages inside the Cat 2 workflow."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.evidence_gate import ProvenanceContract
from src.llm_draft_stage import DraftStageHooks, LlmDraftStageNode
from src.services.service import load_yaml_mapping


_PRIORITY = {"critical": 0, "high": 1, "normal": 2, "low": 3}
_FIELDS = {"crop", "growth_stage", "parcel", "symptom", "deadline", "quantity", "machine", "program"}
_RESERVED_MISSING = "unspecified"
_FALLBACK_MISSING = {
    "missing_field": "unspecified",
    "ask_target": "member",
    "staff_action": "会員に用件の内訳と優先したい件を確認する",
    "why_needed": "用件の切り分けに必要です",
}
_CONTRACT = ProvenanceContract(gate_kind="handoff_packets")
_UNCERTAIN_SPAN_MARKERS = ("ではない", "ではありません", "かどうか", "不明", "未定")
_KANJI_DIGITS = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_KANJI_UNITS = {"十": 10, "百": 100, "千": 1000}
_KANJI_NUMBER = re.compile(r"[〇零一二三四五六七八九十百千]+")
_DEADLINE = re.compile(r"^(?:(?P<year>\d{4})(?:-|/|年))?(?P<month>\d{1,2})月?(?:-|/)?(?P<day>\d{1,2})日?$")
_QUANTITY = re.compile(r"^(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>L|kg|g)$", re.IGNORECASE)


def policy_snapshot(config: dict[str, Any]) -> dict[str, Any] | None:
    """Load approved policy inputs once, returning None for the declared fallback mode."""
    policy, raw_policy = load_yaml_mapping(str(config.get("routing_policy_path", "")))
    calendar, _raw_calendar = load_yaml_mapping(str(config.get("seasonal_calendar_path", "")))
    if not policy or not calendar or not raw_policy:
        return None
    intents = policy.get("intent_types")
    if not isinstance(intents, list) or not isinstance(policy.get("missing_field_reasons"), dict):
        return None
    records = [item for item in intents if isinstance(item, dict) and isinstance(item.get("id"), str)]
    if not records:
        return None
    return {
        "policy_id": str(policy.get("policy_id", "")),
        "policy_version": str(policy.get("policy_version", "")),
        "policy_digest": f"sha256:{hashlib.sha256(raw_policy).hexdigest()}",
        "seasonal_calendar_version": str(calendar.get("seasonal_calendar_version", "")),
        "intent_records": records,
        "intent_types": {str(item["id"]) for item in records} | {"other_general"},
        "mapping_rule_ids": {str(item.get("mapping_rule_id", "")) for item in records},
        "missing_reasons": dict(policy["missing_field_reasons"]),
        "calendar_windows": list(calendar.get("windows", [])),
        "escalations": list(policy.get("priority_escalation", [])),
    }


def unavailable_basis() -> dict[str, str]:
    return {
        "ranking_mode": "policy_unavailable",
        "policy_id": "unavailable",
        "policy_version": "none",
        "policy_digest": "none",
        "seasonal_calendar_version": "none",
        "mapping_rule_id": "fallback-unranked",
    }


def _normalize_fact_value(field: str, source_span: str, business_date: date) -> str | None:
    """Derive only fact values that are mechanically entailed by a source span.

    Relative dates intentionally return None: selecting a concrete date for
    "next week" would be an agent assertion rather than a normalization.
    """
    normalized = _normalize_span_text(source_span)
    if any(marker in normalized for marker in _UNCERTAIN_SPAN_MARKERS):
        return None
    if field == "deadline":
        match = _DEADLINE.fullmatch(normalized)
        if match is None:
            return None
        year = int(match.group("year") or business_date.year)
        try:
            candidate = date(year, int(match.group("month")), int(match.group("day")))
        except ValueError:
            return None
        if match.group("year") is None and candidate < business_date:
            return None
        return candidate.isoformat()
    if field == "quantity":
        match = _QUANTITY.fullmatch(normalized)
        if match is None:
            return None
        number = match.group("number")
        unit = match.group("unit").lower()
        return f"{number}{'L' if unit == 'l' else unit}"
    return None


def _normalize_span_text(source_span: str) -> str:
    normalized = unicodedata.normalize("NFKC", source_span).strip()
    normalized = _KANJI_NUMBER.sub(lambda match: str(_kanji_number_to_int(match.group())), normalized)
    for original, replacement in (("リットル", "L"), ("リッター", "L"), ("キログラム", "kg"), ("キロ", "kg"), ("グラム", "g")):
        normalized = normalized.replace(original, replacement)
    return normalized


def _kanji_number_to_int(token: str) -> int:
    if not any(char in _KANJI_UNITS for char in token):
        return int("".join(str(_KANJI_DIGITS[char]) for char in token))
    total = 0
    pending = 0
    for char in token:
        if char in _KANJI_DIGITS:
            pending = _KANJI_DIGITS[char]
        else:
            total += (pending or 1) * _KANJI_UNITS[char]
            pending = 0
    return total + pending


class SeasonalContextNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None, snapshot: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})
        self._policy_snapshot = snapshot

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("upstream_error_passthrough", {"node": "seasonal_context"}, state)
            return {"status": AgentStatus.ERROR.value}
        try:
            envelope = json.loads(str(state.get("user_input", "")))
            canonical_text = envelope["canonical_text"]
            meta = envelope["intake_meta"]
            if not isinstance(canonical_text, str) or not isinstance(meta, dict):
                raise ValueError("invalid envelope")
            business_date = date.fromisoformat(str(meta["business_date"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return {"status": AgentStatus.ERROR.value, "error_log": ["invalid inner input envelope"]}
        snapshot = self._policy_snapshot
        if snapshot is None:
            basis = unavailable_basis()
            seasonal = {"window_name": "unavailable", "critical_crops": []}
            degradation = "routing_policy_unavailable"
        else:
            window = _window_for_date(snapshot["calendar_windows"], business_date)
            seasonal = {
                "window_name": window.get("name", "outside_calendar") if window else "outside_calendar",
                "critical_crops": list(window.get("crops_in_critical_window", [])) if window else [],
            }
            basis = {
                "ranking_mode": "policy_applied",
                "policy_id": snapshot["policy_id"],
                "policy_version": snapshot["policy_version"],
                "policy_digest": snapshot["policy_digest"],
                "seasonal_calendar_version": snapshot["seasonal_calendar_version"],
            }
            degradation = ""
            expected = meta.get("expected_policy_version")
            if expected is not None and expected != snapshot["policy_version"]:
                emit_trace_event("policy_version_mismatch", {"expected": expected, "actual": snapshot["policy_version"]}, state)
                return {"status": AgentStatus.ERROR.value, "error_log": ["expected policy version does not match"]}
        emit_trace_event(
            "seasonal_context_resolved",
            {"policy_id": basis["policy_id"], "policy_version": basis["policy_version"], "window": seasonal["window_name"]},
            state,
        )
        result = {
            "validated_input": canonical_text,
            "intake_meta": meta,
            "seasonal_context": seasonal,
            "ranking_basis": basis,
            "status": AgentStatus.SUCCESS.value,
        }
        if degradation:
            result["degradation_reason"] = degradation
            emit_trace_event("degradation_applied", {"degradation_reason": degradation, "ranking_mode": basis["ranking_mode"]}, state)
        return result


class IntentDecomposeExtractNode(LlmDraftStageNode):
    """LLM stage constrained to decomposition, extraction, and staff tasks."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None, snapshot: dict[str, Any] | None = None) -> None:
        self._policy_snapshot = snapshot
        hooks = DraftStageHooks(
            build_prompt=self.build_prompt,
            build_fallback=self.build_fallback,
            build_evidence=self.build_evidence,
        )
        super().__init__(config=config, hooks=hooks)

    def build_prompt(self, state: dict) -> str:
        types = sorted(self._policy_snapshot["intent_types"]) if self._policy_snapshot else ["other_general"]
        fields = ["crop", "growth_stage", "parcel", "symptom", "deadline", "quantity", "machine", "program"]
        max_question_chars = int(self._config.get("max_question_chars", 120))
        return json.dumps(
            {
                "task": "相談を意図ごとに分解し、各意図について入力根拠付きの事実と不足情報の職員タスクをJSONで返す。事実または不足情報がある場合は空配列にせず、下記要素形を使う",
                "intent_types": types,
                "extraction_parameters": {
                    "temperature": self._config.get("llm_temperature", 0.0),
                    "max_tokens": self._config.get("llm_max_tokens", 4096),
                },
                "span_selection": {
                    "rule": "source_span は canonical_text から改変せず逐語コピーし、同じ文字列が複数ある場合は左から0始まりの occurrence_index で指定する",
                    "example": {
                        "canonical_text": "防除機の予約と防除機の点検",
                        "source_span": "防除機",
                        "occurrence_index": 1,
                        "meaning": "2回目の「防除機」を選択",
                    },
                },
                "canonical_text": state.get("validated_input", ""),
                "schema": {
                    "intents": [
                        {
                            "intent_id": "i-01",
                            "type": "<intent_types のいずれか>",
                            "source_span": "<canonical_text の literal 部分文字列>",
                            "occurrence_index": 0,
                            "confidence": "high|medium|low",
                            "related_intent_ids": [],
                            "extracted_facts": [
                                {
                                    "field": "crop|growth_stage|parcel|symptom|deadline|quantity|machine|program",
                                    "value": "<原則 source_span と完全一致。ただし field が deadline で原文に絶対日付（例 8月10日）がある場合は ISO-8601（例 2026-08-10）を入れる。相対表現（来週・今月末など）は推測せず source_span と同じ文字列にする。field が quantity の場合は数値＋単位の正規形（例 六十二リットル → 62L）を入れる>",
                                    "source_span": "<canonical_text の literal 部分文字列>",
                                    "occurrence_index": 0,
                                    "confidence": "high|medium|low",
                                }
                            ],
                            "missing_information": [
                                {
                                    "missing_field": "crop|growth_stage|parcel|symptom|deadline|quantity|machine|program|unspecified",
                                    "ask_target": "member|internal_record",
                                    "staff_action": f"<会員への読み上げ文ではない職員の確認タスク、1文、句点「。」なし、{max_question_chars}字以内>",
                                }
                            ],
                        }
                    ]
                },
                "allowlists": {
                    "field": fields,
                    "missing_field": [*fields, "unspecified"],
                    "ask_target": ["member", "internal_record"],
                    "confidence": ["high", "medium", "low"],
                },
                "prohibitions": ["capability", "rank", "seasonal_priority", "why_needed を書かない（policy が決定論的に付与する）"],
            },
            ensure_ascii=False,
        )

    def build_fallback(self, state: dict) -> dict[str, Any]:
        text = str(state.get("validated_input", ""))
        return {
            "intents": [
                {
                    "intent_id": "i-fallback",
                    "type": "other_general",
                    "source_span": text,
                    "occurrence_index": 0,
                    "confidence": "low",
                    "related_intent_ids": [],
                    "extracted_facts": [],
                    "missing_information": [dict(_FALLBACK_MISSING)],
                }
            ]
        }

    def build_evidence(self, _state: dict, draft: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        return ([{"gate_kind": _CONTRACT.gate_kind, "intent_count": len(draft.get("intents", []))}], [])

    def execute(self, state: dict) -> dict:
        result = super().execute(state)
        if result.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("intent_decomposition_completed", {"intent_count": 0, "llm_used": True, "fallback": False}, state)
            return result
        draft = result.get("draft")
        if not _valid_draft(draft):
            return {"status": AgentStatus.ERROR.value, "error_log": ["LLM draft schema is invalid"]}
        degraded = "degradation_reason" in result
        if degraded:
            result["degradation_reason"] = "llm_unavailable"
            emit_trace_event("degradation_applied", {"degradation_reason": "llm_unavailable", "ranking_mode": state.get("ranking_basis", {}).get("ranking_mode", "")}, state)
        emit_trace_event(
            "intent_decomposition_completed",
            {"intent_count": len(draft["intents"]), "llm_used": not degraded, "fallback": degraded},
            state,
        )
        return {"decomposed_intents": draft["intents"], "llm_degraded": degraded, "degradation_reason": result.get("degradation_reason", ""), "status": AgentStatus.SUCCESS.value}


class HandoffRankNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, config: dict[str, Any] | None = None, snapshot: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._config = dict(config or {})
        self._policy_snapshot = snapshot

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("upstream_error_passthrough", {"node": "handoff_rank"}, state)
            return {"status": AgentStatus.ERROR.value}
        text = str(state.get("validated_input", ""))
        basis = dict(state.get("ranking_basis") or unavailable_basis())
        snapshot = self._policy_snapshot
        gaps: list[str] = []
        packets: list[dict[str, Any]] = []
        for index, raw_intent in enumerate(state.get("decomposed_intents", []), start=1):
            packet = _make_packet(raw_intent, text, state, snapshot, basis, self._config, index, gaps)
            if packet is not None:
                packets.append(packet)
        packets.sort(key=lambda item: _rank_key(item, state))
        for rank, packet in enumerate(packets, start=1):
            packet["routing"]["rank"] = rank
        verified = all(_packet_spans_valid(packet, text, int(self._config.get("min_span_chars", 2))) for packet in packets)
        degradation = _join_degradation(state.get("degradation_reason", ""), "no_verifiable_intent" if not packets else "")
        if basis.get("ranking_mode") == "policy_unavailable":
            degradation = _join_degradation(degradation, "routing_policy_unavailable")
        if degradation:
            emit_trace_event("degradation_applied", {"degradation_reason": degradation, "ranking_mode": basis.get("ranking_mode")}, state)
        emit_trace_event("handoff_ranking_completed", {"packet_count": len(packets), "drop_count": len(gaps), "mapping_rule_ids": [packet["ranking_basis"]["mapping_rule_id"] for packet in packets]}, state)
        return {"handoff_packets": packets, "coverage_gaps": gaps, "provenance_verified": verified, "degradation_reason": degradation, "status": AgentStatus.SUCCESS.value}


def _valid_draft(draft: Any) -> bool:
    return isinstance(draft, dict) and isinstance(draft.get("intents"), list) and all(isinstance(item, dict) for item in draft["intents"])


def _window_for_date(windows: list[Any], business_date: date) -> dict[str, Any] | None:
    marker = business_date.strftime("%m-%d")
    for window in windows:
        if isinstance(window, dict) and str(window.get("from", "")) <= marker <= str(window.get("to", "")):
            return window
    return None


def _span_valid(span: str, offsets: Any, text: str, minimum: int) -> tuple[bool, str]:
    if not isinstance(offsets, dict) or not isinstance(offsets.get("start"), int) or not isinstance(offsets.get("end"), int):
        return False, "offset_mismatch"
    start, end = offsets["start"], offsets["end"]
    if len(span) < minimum or end <= start:
        return False, "too_short"
    if start < 0 or end > len(text) or text[start:end] != span or span not in text:
        return False, "offset_mismatch" if 0 <= start <= end <= len(text) else "not_found"
    return True, ""


def _literal_span_offsets(span: Any, occurrence_index: Any, text: str, minimum: int) -> tuple[dict[str, int] | None, str]:
    """Resolve a literal LLM quotation to deterministic, end-exclusive offsets."""
    if not isinstance(span, str) or len(span) < minimum:
        return None, "too_short"
    if isinstance(occurrence_index, bool) or not isinstance(occurrence_index, int) or occurrence_index < 0:
        return None, "invalid_occurrence"
    start = -1
    search_from = 0
    for _ in range(occurrence_index + 1):
        start = text.find(span, search_from)
        if start < 0:
            return None, "not_found"
        search_from = start + len(span)
    return {"start": start, "end": start + len(span)}, ""


def _make_packet(raw: Any, text: str, state: dict, snapshot: dict[str, Any] | None, basis: dict[str, Any], config: dict[str, Any], index: int, gaps: list[str]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    intent = dict(raw)
    minimum = int(config.get("min_span_chars", 2))
    intent_offsets, reason = _literal_span_offsets(intent.get("source_span"), intent.get("occurrence_index"), text, minimum)
    if intent_offsets is None:
        gaps.append(f"unverified_span:intent:{reason}")
        emit_trace_event("provenance_span_rejected", {"target": "intent", "reason": reason}, state)
        return None
    intent["span_offsets"] = intent_offsets
    raw_type = str(intent.get("type", "other_general"))
    record = next((item for item in (snapshot or {}).get("intent_records", []) if item.get("id") == raw_type), None)
    if snapshot is None:
        intent_type, capability, mapping_rule, priority = "other_general", "other_general", "fallback-unranked", "normal"
    elif record is None:
        intent_type, capability, mapping_rule, priority = "other_general", "other_general", "map-general", "low"
        gaps.append(f"unmapped_intent:{raw_type}")
    else:
        intent_type = raw_type
        capability = str(record.get("capability", "other_general"))
        mapping_rule = str(record.get("mapping_rule_id", "map-general"))
        priority = str(record.get("base_priority", "normal"))
    facts = []
    for fact in raw.get("extracted_facts", [])[: int(config.get("max_facts_per_intent", 8))]:
        if not isinstance(fact, dict) or fact.get("field") not in _FIELDS:
            continue
        fact = dict(fact)
        span = fact.get("source_span")
        fact_offsets, reason = _literal_span_offsets(span, fact.get("occurrence_index"), text, minimum)
        if fact_offsets is None:
            gaps.append(f"unverified_span:{fact.get('field', 'fact')}:{reason}")
            emit_trace_event("provenance_span_rejected", {"target": "field", "reason": reason}, state)
            continue
        fact["span_offsets"] = fact_offsets
        field = str(fact["field"])
        reported_value = str(fact.get("value", span))
        try:
            business_date = date.fromisoformat(str(state.get("intake_meta", {}).get("business_date", "")))
        except ValueError:
            normalized_value = None
        else:
            normalized_value = _normalize_fact_value(field, span, business_date)
        if reported_value != span and reported_value != normalized_value:
            gaps.append(f"unverified_value:{fact.get('field', 'fact')}")
            continue
        intent_offsets = intent["span_offsets"]
        fact_offsets = fact["span_offsets"]
        if fact_offsets["end"] < intent_offsets["start"] - 10 or fact_offsets["start"] > intent_offsets["end"] + 10:
            gaps.append("span_context_mismatch")
        facts.append({"field": field, "value": reported_value, "source_span": span, "span_offsets": fact["span_offsets"], "confidence": _confidence(fact.get("confidence"))})
    if snapshot and _should_escalate(snapshot, facts, state.get("seasonal_context", {})):
        priority = "critical"
    missing = _missing_items(raw.get("missing_information"), snapshot, config)
    if not missing:
        missing = _fallback_missing(snapshot)
    routing = {"capability": capability, "rationale": f"意図 {intent_type} を写像ルール {mapping_rule} により {capability} へ振り分け（季節ウィンドウ: {state.get('seasonal_context', {}).get('window_name', 'unavailable')}）", "seasonal_priority": priority if priority in _PRIORITY else "normal", "rank": 0}
    packet_basis = {**basis, "mapping_rule_id": mapping_rule}
    return {"packet_id": f"pkt-{_letters(index)}", "intent": {"intent_id": str(intent.get("intent_id", f"i-{index:02d}")), "type": intent_type, "source_span": intent["source_span"], "span_offsets": intent["span_offsets"], "confidence": _confidence(intent.get("confidence")), "related_intent_ids": [str(value) for value in intent.get("related_intent_ids", []) if isinstance(value, str)]}, "routing": routing, "ranking_basis": packet_basis, "extracted_facts": facts, "missing_information": missing, "provenance": {"source_channel": state.get("intake_meta", {}).get("source_channel", "unspecified"), "caller_ref": state.get("intake_meta", {}).get("caller_ref", "")}}


def _missing_items(items: Any, snapshot: dict[str, Any] | None, config: dict[str, Any]) -> list[dict[str, str]]:
    if not isinstance(items, list):
        return []
    result: list[dict[str, str]] = []
    allowed = _FIELDS | {_RESERVED_MISSING}
    reasons = (snapshot or {}).get("missing_reasons", {})
    for item in items[: int(config.get("max_questions_per_intent", 5))]:
        if not isinstance(item, dict):
            continue
        field, target, action = item.get("missing_field"), item.get("ask_target"), item.get("staff_action")
        if field not in allowed or target not in {"member", "internal_record"} or not isinstance(action, str) or not action.strip():
            continue
        if "。" in action or len(action) > int(config.get("max_question_chars", 120)):
            continue
        reason = reasons.get(field) if isinstance(reasons, dict) else None
        if not isinstance(reason, str):
            if field == _RESERVED_MISSING:
                reason = _FALLBACK_MISSING["why_needed"]
            else:
                continue
        result.append({"missing_field": field, "ask_target": target, "staff_action": action, "why_needed": reason})
    return result


def _fallback_missing(snapshot: dict[str, Any] | None) -> list[dict[str, str]]:
    if snapshot is None:
        return [dict(_FALLBACK_MISSING)]
    return [{**_FALLBACK_MISSING, "why_needed": str(snapshot["missing_reasons"].get("unspecified", _FALLBACK_MISSING["why_needed"]))}]


def _confidence(value: Any) -> str:
    return str(value) if value in {"high", "medium", "low"} else "low"


def _should_escalate(snapshot: dict[str, Any], facts: list[dict[str, Any]], seasonal: Any) -> bool:
    fields = {fact["field"] for fact in facts}
    crops = {fact["value"] for fact in facts if fact["field"] == "crop"}
    critical_crops = set((seasonal or {}).get("critical_crops", []))
    return any(rule.get("when_intent_has_field") in fields and rule.get("and_crop_in_critical_window") and crops & critical_crops for rule in snapshot["escalations"] if isinstance(rule, dict))


def _rank_key(packet: dict[str, Any], state: dict) -> tuple[int, int, int]:
    deadline = next((fact["value"] for fact in packet["extracted_facts"] if fact["field"] == "deadline" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", fact["value"])), None)
    try:
        distance = (date.fromisoformat(deadline) - date.fromisoformat(state["intake_meta"]["business_date"])).days if deadline else 10**9
    except ValueError:
        distance = 10**9
    return (_PRIORITY.get(packet["routing"]["seasonal_priority"], 2), distance, packet["intent"]["span_offsets"]["start"])


def _packet_spans_valid(packet: dict[str, Any], text: str, minimum: int) -> bool:
    intent = packet["intent"]
    if not _span_valid(intent["source_span"], intent["span_offsets"], text, minimum)[0]:
        return False
    return all(_span_valid(fact["source_span"], fact["span_offsets"], text, minimum)[0] for fact in packet["extracted_facts"])


def _join_degradation(*values: str) -> str:
    return "+".join(dict.fromkeys(value for value in values if value))


def _letters(index: int) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyz"
    return "".join(alphabet[(index + offset) % len(alphabet)] for offset in range(8))
