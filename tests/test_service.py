"""The conversation loop against a moto-backed DynamoDB table, with the agent and
the senders replaced by fakes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from omnidamus import service
from omnidamus.schema import Channel, InboundMessage, RoutingDecision

PHONE = "+15550001111"
NOW = datetime(2026, 10, 25, 10, 0, tzinfo=timezone.utc)


class Recorder:
    def __init__(self):
        self.sent = []
        self.emails = []

    def send(self, address, decision):
        self.sent.append((decision.reply_channel, address, decision.reply_text))
        return f"msg-{len(self.sent)}"

    def send_email(self, to, subject, body_text):
        self.emails.append((to, subject, body_text))
        return f"ses-{len(self.emails)}"


def _msg(channel, body, mid, at=NOW):
    return InboundMessage(conversation_id=PHONE, channel=channel, from_address=PHONE,
                          body=body, provider_message_id=mid, received_at=at)


def _run(store, rec, message, decision, now=NOW):
    seen = {}

    def route(msg, state, window_open):
        seen["state"] = state.model_copy(deep=True)
        seen["window_open"] = window_open
        return decision

    result = service.handle_inbound(message, store, route=route, send=rec.send,
                                    send_email=rec.send_email, now=now)
    return result, seen


@pytest.fixture
def rec():
    return Recorder()


def test_start_conversation_sends_sms_and_saves(store, rec):
    out = service.start_conversation(PHONE, "Your pickup is ready. Reply to pick a time.",
                                     store, email="cust@example.com", context="pickup", send=rec.send)
    assert rec.sent == [(Channel.SMS_RCS, PHONE, "Your pickup is ready. Reply to pick a time.")]
    state = store.get(PHONE)
    assert state.customer_email == "cust@example.com"
    assert state.turns[0].direction == "outbound"
    assert out["message_id"] == "msg-1"


def test_reply_on_same_channel_and_history_passed_to_agent(store, rec):
    service.start_conversation(PHONE, "Pickup ready", store, context="pickup", send=rec.send)
    decision = RoutingDecision(summary="asks time", reply_channel=Channel.SMS_RCS, reply_text="What time?")
    _, seen = _run(store, rec, _msg(Channel.SMS_RCS, "ok", "sms-1"), decision)

    assert [t.text for t in seen["state"].turns] == ["Pickup ready", "ok"]
    assert rec.sent[-1] == (Channel.SMS_RCS, PHONE, "What time?")
    assert len(store.get(PHONE).turns) == 3


def test_duplicate_delivery_is_dropped(store, rec):
    decision = RoutingDecision(summary="s", reply_channel=Channel.SMS_RCS, reply_text="hi")
    _run(store, rec, _msg(Channel.SMS_RCS, "hello", "sms-1"), decision)
    result, _ = _run(store, rec, _msg(Channel.SMS_RCS, "hello", "sms-1"), decision)
    assert result["skipped"] == "duplicate"
    assert len(rec.sent) == 1


def test_handoff_to_whatsapp_when_window_closed_sends_link_on_sms(store, rec, monkeypatch):
    monkeypatch.setenv("OMNIDAMUS_WHATSAPP_DISPLAY_NUMBER", "+1 206 555 0100")
    decision = RoutingDecision(summary="move", reply_channel=Channel.SMS_RCS,
                               reply_text="Let's sort this on WhatsApp.", handoff_to=Channel.WHATSAPP)
    _, seen = _run(store, rec, _msg(Channel.SMS_RCS, "I need to reschedule", "sms-1"), decision)

    assert seen["window_open"] is False
    channel, address, text = rec.sent[-1]
    assert channel is Channel.SMS_RCS and address == PHONE
    assert text.endswith("Continue on WhatsApp: https://wa.me/12065550100")


def test_whatsapp_inbound_opens_window_and_reply_goes_on_whatsapp(store, rec):
    decision = RoutingDecision(summary="s", reply_channel=Channel.WHATSAPP, reply_text="Sure, 3pm works")
    _, seen = _run(store, rec, _msg(Channel.WHATSAPP, "3pm?", "wamid.1"), decision)
    assert seen["window_open"] is True
    assert rec.sent[-1] == (Channel.WHATSAPP, PHONE, "Sure, 3pm works")


def test_window_closes_after_24_hours(store, rec):
    decision = RoutingDecision(summary="s", reply_channel=Channel.WHATSAPP, reply_text="hi")
    _run(store, rec, _msg(Channel.WHATSAPP, "hello", "wamid.1"), decision)
    later = NOW + timedelta(hours=25)
    _, seen = _run(store, rec, _msg(Channel.SMS_RCS, "still there?", "sms-2", at=later),
                   decision, now=later)
    assert seen["window_open"] is False
    assert rec.sent[-1][0] is Channel.SMS_RCS


def test_completion_sends_receipt_to_email_once(store, rec):
    decision = RoutingDecision(summary="Pickup booked for Fri 3pm", reply_channel=Channel.WHATSAPP,
                               reply_text="Booked. A receipt is on its way.",
                               conversation_complete=True,
                               key_facts={"time": "Fri 3pm", "email": "cust@example.com"})
    result, _ = _run(store, rec, _msg(Channel.WHATSAPP, "Fri 3pm, cust@example.com", "wamid.1"), decision)

    assert result["status"] == "complete"
    assert rec.sent[-1] == (Channel.WHATSAPP, PHONE, "Booked. A receipt is on its way.")
    [(to, subject, body)] = rec.emails
    assert to == "cust@example.com"
    assert "Pickup booked for Fri 3pm" in subject
    assert "time: Fri 3pm" in body and "email:" not in body
    assert store.get(PHONE).receipt_message_id == "ses-1"


def test_completion_without_email_sends_no_receipt(store, rec):
    decision = RoutingDecision(summary="done", reply_channel=Channel.SMS_RCS, reply_text="Thanks",
                               conversation_complete=True)
    result, _ = _run(store, rec, _msg(Channel.SMS_RCS, "bye", "sms-1"), decision)
    assert result["status"] == "complete"
    assert rec.emails == []


def test_email_reply_without_address_falls_back_to_current_channel(store, rec):
    decision = RoutingDecision(summary="s", reply_channel=Channel.EMAIL, reply_text="Details by email")
    _run(store, rec, _msg(Channel.SMS_RCS, "email me", "sms-1"), decision)
    assert rec.sent[-1] == (Channel.SMS_RCS, PHONE, "Details by email")


def test_invalid_email_fact_is_not_stored(store, rec):
    decision = RoutingDecision(summary="s", reply_channel=Channel.SMS_RCS, reply_text="ok",
                               key_facts={"email": "not-an-email"})
    _run(store, rec, _msg(Channel.SMS_RCS, "x", "sms-1"), decision)
    assert store.get(PHONE).customer_email is None


def test_new_message_after_completion_reopens(store, rec):
    done = RoutingDecision(summary="done", reply_channel=Channel.SMS_RCS, reply_text="Thanks",
                           conversation_complete=True, key_facts={"email": "c@example.com"})
    _run(store, rec, _msg(Channel.SMS_RCS, "bye", "sms-1"), done)
    again = RoutingDecision(summary="again", reply_channel=Channel.SMS_RCS, reply_text="Hi again")
    result, _ = _run(store, rec, _msg(Channel.SMS_RCS, "one more thing", "sms-2"), again)
    assert result["status"] == "open"
    assert store.get(PHONE).receipt_message_id is None
