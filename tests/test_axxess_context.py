"""Phase 3 (2026-11-16): reading the customer's AXXESS lead history before a reply."""

import json
import logging

import pytest

from omnidamus import agent, axxess_context
from omnidamus.schema import Channel, InboundMessage

LEAD = "11111111-2222-3333-4444-555555555555"
CONTEXT = {
    "lead": {"id": LEAD, "stage": "qualifying", "organizationName": "Rao Clinics"},
    "timeline": [
        {"kind": "change", "at": "2026-11-15T09:00:00Z", "action": "crm.lead.updated"},
        {"kind": "attempt", "at": "2026-11-12T09:00:00Z", "channel": "voice_human", "decision": "allowed", "outcome": "call_back", "note": "after 5pm"},
        {"kind": "conversation", "at": "2026-11-14T09:00:00Z", "channel": "voice", "direction": "inbound", "summary": "Asked\nfor pricing."},
        {"kind": "reply", "at": "2026-11-13T09:00:00Z", "category": "interested", "subject": "Ten seats"},
    ],
}


@pytest.fixture
def axxess(monkeypatch):
    monkeypatch.setenv("AXXESS_MCP_URL", "https://staging.example/api/agents/mcp")
    monkeypatch.setenv("AXXESS_API_KEY_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:1:secret:key")
    monkeypatch.setattr(axxess_context, "_secret", lambda arn: "secret")
    calls = []
    replies = {"search_leads": [{"id": "other", "contactPhoneE164": "+15550002222999"},
                                {"id": LEAD, "contactPhoneE164": "+15550002222"}],
               "get_lead_context": CONTEXT}

    def fake_call(name, arguments):
        calls.append((name, arguments))
        reply = replies[name]
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(axxess_context, "_call", fake_call)
    return calls, replies


def test_off_without_configuration(monkeypatch):
    monkeypatch.delenv("AXXESS_MCP_URL", raising=False)
    monkeypatch.delenv("AXXESS_API_KEY_SECRET_ARN", raising=False)
    assert axxess_context.lead_history("+15550002222", None) is None


def test_finds_the_exact_lead_and_returns_a_newest_first_brief(axxess):
    calls, _ = axxess
    history = axxess_context.lead_history("+15550002222", "cust@example.com")
    assert calls == [("search_leads", {"query": "+15550002222"}), ("get_lead_context", {"leadId": LEAD})]
    assert history == "\n".join([
        "Lead stage: qualifying, company: Rao Clinics",
        "2026-11-14 inbound voice: Asked for pricing.",
        "2026-11-13 email reply (interested): Ten seats",
        "2026-11-12 voice human call: call back (after 5pm)",
    ])


def test_falls_back_to_email_when_the_phone_matches_nothing(axxess):
    calls, replies = axxess
    replies["search_leads"] = [{"id": LEAD, "contactEmail": "Cust@Example.com"}]
    assert axxess_context.lead_history("+15550009999", "cust@example.com") is not None
    assert [c[1] for c in calls if c[0] == "search_leads"] == [{"query": "+15550009999"}, {"query": "cust@example.com"}]


def test_unknown_customer_or_any_failure_means_no_history(axxess, caplog):
    calls, replies = axxess
    replies["search_leads"] = []
    assert axxess_context.lead_history("+15550002222", None) is None
    replies["search_leads"] = RuntimeError("AXXESS MCP error -32001")
    with caplog.at_level(logging.WARNING):
        assert axxess_context.lead_history("+15550002222", None) is None
    assert "+1555" not in caplog.text


def test_brief_stays_within_limits():
    many = {"lead": {}, "timeline": [
        {"kind": "conversation", "at": f"2026-11-{10 + i % 9:02d}T09:00:00Z", "channel": "sms", "direction": "inbound", "summary": "x" * 400}
        for i in range(40)]}
    text = axxess_context.brief(many)
    assert len(text) <= axxess_context.MAX_BRIEF and len(text.splitlines()) <= axxess_context.MAX_LINES
    assert axxess_context.brief({"lead": {"stage": "new"}, "timeline": []}) is None


def test_prompt_carries_the_history_only_when_there_is_one():
    message = InboundMessage(channel=Channel.SMS_RCS, from_address="+15550002222", conversation_id="+15550002222", body="Hi")
    assert "What our CRM already knows" not in agent.build_prompt(message)
    prompt = agent.build_prompt(message, lead_history="2026-11-14 inbound voice: Asked for pricing.")
    assert "What our CRM already knows about this customer" in prompt
    assert prompt.index("Asked for pricing") < prompt.index("New message:")


def test_call_sends_the_key_and_the_staging_bypass(monkeypatch):
    monkeypatch.setenv("AXXESS_MCP_URL", "https://staging.example/api/agents/mcp")
    monkeypatch.setenv("AXXESS_API_KEY_SECRET_ARN", "key-arn")
    monkeypatch.setenv("AXXESS_BYPASS_SECRET_ARN", "bypass-arn")
    monkeypatch.setattr(axxess_context, "_secret", lambda arn: {"key-arn": "axk_test", "bypass-arn": "bp"}[arn])
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"result": {"content": [{"type": "text", "text": "[]"}]}}).encode()

    def fake_urlopen(request, timeout):
        seen["headers"] = dict(request.header_items())
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr(axxess_context.urllib.request, "urlopen", fake_urlopen)
    assert axxess_context._call("search_leads", {"query": "+15550002222"}) == []
    assert seen["headers"]["Authorization"] == "Bearer axk_test"
    assert seen["headers"]["X-vercel-protection-bypass"] == "bp"
    assert seen["timeout"] == axxess_context.TIMEOUT_SECONDS
