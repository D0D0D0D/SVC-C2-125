# SVC-C2-125 — JAMemberInquiryIntakeAgent

> **Category**: Cat 2 (domain workflow)
> **Industry**: SVC

## Overview

JAMemberInquiryIntakeAgent turns a free-text enquiry from an agricultural co-operative's farming
advisory desk into structured handoff packets — one per intent. A member's message rarely contains
one request: a pest symptom, a machinery booking and a subsidy question arrive in the same
paragraph, and the desk has to split them before anything can be routed. The agent does that split,
attaches the evidence span each intent was read from, names the destination capability, and lists
what a staff member still has to ask.

Two properties define the design:

- **Routing is derived, never invented.** The model reads intents and facts out of the text; the
  destination capability and the relative ranking come from an **approved, version-pinned policy
  file** and a seasonal calendar. The model is not asked which enquiry is more urgent, because
  ranking is the part that has to be auditable — the same text, the same policy version, the same
  order. The packet carries the policy version it was derived under.
- **Every extracted item points back at the text.** Facts and intents carry the source span they
  came from, verified against the canonical text; a span the text does not contain is not
  published. `provenance_verified` reports whether that verification held.

The agent does not diagnose pests or disease, does not decide control measures, does not determine
subsidy eligibility, does not make or change a booking, and does not assign a specific member of
staff. It prepares the intake; a person does the rest.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

Provide:

- an AgentCore runtime;
- an approved routing policy and seasonal calendar. Sample files ship as
  `config/routing_policy.sample.yaml` and `config/seasonal_calendar.sample.yaml` and are the
  defaults — **replace them with your own approved, versioned files before deploying.** The policy
  defines intent types, the capability each routes to, the fixed reason wording, and base
  priorities; the calendar defines per-crop critical windows;
- optionally a `BaseLLM` implementation through `config["llm"]`. Without it — or when a call fails
  — the agent returns a deterministic fallback and names the shortfall in `degradation_reason`;
- caller authentication in front of the entry adapter.

The agent holds **no knowledge base, no database and no durable business state**: it is a
request-scoped read. That is why `entry_adapter.SCOPE_KEYS` is an explicit empty tuple — there is
no customer-internal partition to bind, and placeholder scope keys must not be reintroduced to
imply one. It also declares `requires.secrets: []` on purpose: declaring an LLM key there would
make start-up fail in an environment without one, and take the deterministic fallback path — the
very behaviour the template guarantees — down with it.

Runtime parameters read from `config/config.yaml` (defaults in parentheses):

| Key | Meaning |
|---|---|
| `routing_policy_path`, `seasonal_calendar_path` | The approved policy and calendar. Change these first |
| `business_timezone` (`Asia/Tokyo`) | Basis for the business date when the caller omits it |
| `max_inquiry_chars` (`4000`) | Longest enquiry accepted |
| `max_intents` (`8`) | Packets returned per enquiry |
| `max_facts_per_intent` (`8`), `max_questions_per_intent` (`5`) | Per-packet ceilings |
| `min_span_chars` (`2`), `max_span_chars` (`200`) | Bounds on an evidence span |
| `max_question_chars` (`120`) | Longest staff action. Combined with a single-sentence rule, this stops a fact and a judgement being packed into one field |
| `max_output_bytes` (`65536`) | Response ceiling; packets are truncated rather than the response failing |
| `llm_temperature` (`0.0`), `llm_max_tokens` (`4096`) | Passed to the injected model |
| `max_retry` (`3`), `timeout_s` (`45`), `memory_enabled` (`false`), `hitl.enabled` (`false`) | Framework parameters |

Input is text only. There is no file upload and no parser — an attack surface the template
deliberately does not open.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent resolves its secrets through the platform and relies on it for caller identity. Without it, only the token-authenticated path is available (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The agent is built once at application start-up, so a broken policy or configuration surfaces
before any traffic is served rather than on the first request. Without a platform it still starts,
and `/health` answers `200`.

`POST /invoke` is a read-only operation, so — unlike templates that persist business state — it
*is* reachable with a bearer `INVOKE_AUTH_TOKEN`: a matching token promotes the caller to
`VERIFIED_EXTERNAL` and the request runs, recorded under a fixed standalone caller reference rather
than a real identity. What is missing without a platform is a model, so the run takes the
deterministic path and says so in `degradation_reason`. The test suite runs without a platform
connection, using injected fakes.

## Input

`POST /invoke` takes `input` — the enquiry text — plus an optional `session_id` and an optional
`context` object. Both request models reject unknown fields outright rather than ignoring them.

`context` accepts exactly three keys:

| Key | Meaning |
|---|---|
| `source_channel` | One of `chat`, `line`, `phone`, `counter`, `unspecified`. Anything else is **422** |
| `business_date` | `YYYY-MM-DD`. Malformed is **422**. Omitted, it is resolved at the entry point from `business_timezone` |
| `expected_policy_version` | The policy version the caller believes is in force, so a version skew is visible rather than silent |

Callers must be authenticated: trust comes from verified upstream middleware, or from a matching
`Authorization: Bearer` header when `INVOKE_AUTH_TOKEN` is set. A wrong token is **401**; a caller
with no verified trust is **403**, as is one whose caller identity is empty or over-long.

```bash
curl -X POST http://127.0.0.1:8000/invoke \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -d '{
    "input": "先週から葉に斑点が出ています。あと来月の田植え機の予約もお願いしたいです。",
    "session_id": "example-session",
    "context": {"source_channel": "phone"}
  }'
```

The caller supplies neither the inquiry id nor the caller reference — the adapter mints them, so a
request body cannot forge either.

## Output

The standard AgentCore envelope is preserved. `output` carries the intake result:

```json
{
  "schema_version": "1.0.0",
  "inquiry_id": "inq-abcdefgh",
  "text_basis": "canonical_text",
  "packets": [
    {
      "packet_id": "pkt-a",
      "intent": {
        "intent_id": "i-01",
        "type": "pest_symptom_report",
        "source_span": "葉に斑点が出ています",
        "span_offsets": [7, 18],
        "confidence": 0.8,
        "related_intent_ids": []
      },
      "routing": {"capability": "pest_disease_consultation", "priority": "high", "reason": "..."},
      "ranking_basis": {"policy_version": "2026-07"},
      "extracted_facts": [],
      "missing_information": [
        {"missing_field": "field_location", "ask_target": "member", "staff_action": "圃場の所在を確認する"}
      ],
      "provenance": {"source_channel": "phone", "caller_ref": "invoke-standalone"}
    }
  ],
  "coverage_gaps": [],
  "provenance_verified": true,
  "degradation_reason": ""
}
```

`text_basis` names what the spans are offsets into. `coverage_gaps` records enquiry content that
produced no packet — omitted intents are reported, not dropped. Values above are illustrative.

## Security and Limitations

Every node requires verified-external trust, so an unauthenticated caller is stopped before any
node body runs.

Output safety is structural rather than lexical. Fields that would express a determination —
a diagnosis, an eligibility verdict, a confirmed booking — do not exist in the response schema, and
the output gate enforces a closed allow-list of fields. A denylist of assertive vocabulary was
considered and rejected: paraphrase defeats it, whereas a field that does not exist cannot be
filled. The single-sentence, length-bounded `staff_action` serves the same end, preventing a fact
and a judgement from being merged into one line.

Personal data is handled by the framework's `detect_pii`, applied across the free-text fields
rather than the main one only. **A template-local Japanese name or contact detector is deliberately
not implemented**, and there is a known, documented limit: the framework detector misses Japanese
phone numbers and email addresses when they sit directly against Japanese characters with no
delimiter (`連絡先は090-1234-5678です。` is not detected). This is reported upstream rather than
worked around locally, because a hand-rolled second detector drifts from the one the framework
actually runs. Treat the agent as one layer of defence: the caller and the operator remain
responsible for what they send and store.

Known limits:

- **Routing quality is the policy's quality.** The shipped policy and calendar are *samples*. An
  unreplaced sample routes real enquiries by example data.
- **Span verification proves the text said it, not that reading it that way was right.**
- **No accumulation.** The agent does not learn from past enquiries or match against history —
  that is out of scope by design, not pending.
- **Extraction is model-driven.** With no model injected the deterministic fallback is coarser;
  `degradation_reason` says so, and a packet count is not evidence of completeness.

Output is machine-generated and assistive. Replies to members, diagnosis, eligibility decisions and
booking or assignment confirmations remain with the responsible staff.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2), document-generation pattern
- Generation mode: `llm` (with a deterministic fallback when no model is injected)
- HITL: not enabled
- Persistence: none — no knowledge base, no database, no framework checkpointer
- External API clients: deployment-injected; none bundled

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection, using injected fakes and the sample policy files. Running
the agent itself starts cleanly with `uvicorn src.api.server:app`; with no model injected it
returns the deterministic fallback — see *Behaviour without the platform*.

## Documentation

`docs/02_design.md` carries the node flow, the state schema, the handoff-packet contract, the
policy-derivation rules and the security design, together with the design decision record.
`docs/03_test_spec.md` records the test specification and the verification results, including a
live-model run.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

---
