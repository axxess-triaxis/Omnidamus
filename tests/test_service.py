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

    def send_template(self, to, name, language_code="en", body_parameters=None):
        self.sent.append((Channel.WHATSAPP, to, f"template:{name}:{body_parameters}"))
        return f"wamid.out{len(self.sent)}"


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
                                    send_email=rec.send_email, send_template=rec.send_template,
                                    now=now)
    return result, seen


@pytest.fixture
def rec():
    return Recorder()


def test_start_conversation_sends_sms_and_saves(store, rec):
    out = service.start_conversation(PHONE, store, notification="Your pickup is ready. Reply to pick a time.",
                                     email="cust@example.com", context="pickup", send=rec.send)
    assert rec.sent == [(Channel.SMS_RCS, PHONE, "Your pickup is ready. Reply to pick a time.")]
    state = store.get(PHONE)
    assert state.customer_email == "cust@example.com"
    assert state.turns[0].direction == "outbound"
    assert out["message_id"] == "msg-1"


def test_reply_on_same_channel_and_history_passed_to_agent(store, rec):
    service.start_conversation(PHONE, store, notification="Pickup ready", context="pickup", send=rec.send)
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


# --- opt-out ---------------------------------------------------------------

def _never_route(*_):
    raise AssertionError("agent must not be called")


def test_stop_opts_out_and_silences_later_messages(store, rec):
    result, _ = _run(store, rec, _msg(Channel.WHATSAPP, "STOP", "wamid.1"), None)
    assert result["status"] == "opted_out"
    assert rec.sent[-1] == (Channel.WHATSAPP, PHONE, service.OPT_OUT_CONFIRMATION)

    result = service.handle_inbound(_msg(Channel.WHATSAPP, "hello?", "wamid.2"), store,
                                    route=_never_route, send=rec.send, now=NOW)
    assert result["skipped"] == "opted_out"
    assert len(rec.sent) == 1


def test_sms_stop_sends_no_confirmation_of_our_own(store, rec):
    _run(store, rec, _msg(Channel.SMS_RCS, "Stop.", "sms-1"), None)
    assert rec.sent == []
    assert store.get(PHONE).opted_out


def test_start_opts_back_in(store, rec):
    _run(store, rec, _msg(Channel.WHATSAPP, "unsubscribe", "wamid.1"), None)
    result, _ = _run(store, rec, _msg(Channel.WHATSAPP, "START", "wamid.2"), None)
    assert result["status"] == "opted_in"
    assert not store.get(PHONE).opted_out


def test_cannot_start_conversation_with_opted_out_customer(store, rec):
    _run(store, rec, _msg(Channel.WHATSAPP, "stop", "wamid.1"), None)
    out = service.start_conversation(PHONE, store, notification="hi", send=rec.send)
    assert out["skipped"] == "opted_out"


# --- WhatsApp templates ----------------------------------------------------

def test_whatsapp_first_conversation_opens_with_template(store, rec):
    out = service.start_conversation(PHONE, store, channel=Channel.WHATSAPP,
                                     template="demo_request", template_params=["Asha"],
                                     send=rec.send, send_template=rec.send_template)
    assert rec.sent == [(Channel.WHATSAPP, PHONE, "template:demo_request:['Asha']")]
    state = store.get(PHONE)
    assert state.current_channel is Channel.WHATSAPP
    assert state.turns[0].provider_message_id == out["message_id"]


def test_whatsapp_start_without_template_is_refused(store, rec):
    with pytest.raises(ValueError):
        service.start_conversation(PHONE, store, channel=Channel.WHATSAPP, send=rec.send)


def test_handoff_uses_template_when_configured(store, rec, monkeypatch):
    monkeypatch.setenv("OMNIDAMUS_WHATSAPP_HANDOFF_TEMPLATE", "continue_on_whatsapp")
    decision = RoutingDecision(summary="move", reply_channel=Channel.SMS_RCS,
                               reply_text="Let's move to WhatsApp", handoff_to=Channel.WHATSAPP)
    _run(store, rec, _msg(Channel.SMS_RCS, "need help", "sms-1"), decision)
    assert rec.sent[-1] == (Channel.WHATSAPP, PHONE, "template:continue_on_whatsapp:None")
    assert store.get(PHONE).current_channel is Channel.WHATSAPP


# --- inbound email ---------------------------------------------------------

def _email(body, mid="ses-in-1", sender="cust@example.com"):
    return InboundMessage(conversation_id="", channel=Channel.EMAIL, from_address=sender,
                          body=body, provider_message_id=mid, received_at=NOW)


def test_email_reply_continues_the_phone_conversation(store, rec):
    service.start_conversation(PHONE, store, notification="Pickup ready",
                               email="cust@example.com", send=rec.send)
    decision = RoutingDecision(summary="moved", reply_channel=Channel.EMAIL, reply_text="Moved to 4pm")
    result, seen = _run(store, rec, _email("Can we make it 4pm?"), decision)
    assert result["conversation_id"] == PHONE
    assert seen["state"].turns[-1].channel is Channel.EMAIL
    assert rec.sent[-1] == (Channel.EMAIL, "cust@example.com", "Moved to 4pm")


def test_email_from_unknown_sender_is_dropped(store, rec):
    result = service.handle_inbound(_email("hello", sender="stranger@example.com"), store,
                                    route=_never_route, send=rec.send, now=NOW)
    assert result == {"skipped": "unknown_sender"}


def test_email_given_in_chat_links_for_later_replies(store, rec):
    decision = RoutingDecision(summary="s", reply_channel=Channel.WHATSAPP, reply_text="Noted",
                               key_facts={"email": "Cust@Example.com"})
    _run(store, rec, _msg(Channel.WHATSAPP, "my email is Cust@Example.com", "wamid.1"), decision)
    assert store.find_by_email("cust@example.com") == PHONE


# --- delivery receipts -----------------------------------------------------

def test_whatsapp_status_updates_outbound_turn(store, rec):
    out = service.start_conversation(PHONE, store, channel=Channel.WHATSAPP, template="demo_request",
                                     send=rec.send, send_template=rec.send_template)
    status = {"conversation_id": PHONE, "provider_message_id": out["message_id"],
              "status": "read", "errors": []}
    assert service.apply_whatsapp_status(status, store) is True
    assert store.get(PHONE).turns[0].status == "read"


def test_status_for_unknown_message_is_ignored(store):
    status = {"conversation_id": PHONE, "provider_message_id": "wamid.none",
              "status": "failed", "errors": [{"code": 131047}]}
    assert service.apply_whatsapp_status(status, store) is False
