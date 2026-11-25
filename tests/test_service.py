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
    # The summary is the agent's internal note and never reaches the customer.
    assert subject == service.RECEIPT_SUBJECT
    assert "Pickup booked" not in subject + body
    assert body.startswith("Booked. A receipt is on its way.")
    assert "- Time: Fri 3pm" in body and "email" not in body.lower()
    assert store.get(PHONE).receipt_message_id == "ses-1"


def test_failed_receipt_still_saves_the_turn(store, rec):
    def failing_email(to, subject, body_text):
        raise service.ses_email.SesSendError("AccessDenied")

    decision = RoutingDecision(summary="Demo booked", reply_channel=Channel.WHATSAPP,
                               reply_text="Confirmed for 6pm.", conversation_complete=True,
                               key_facts={"email": "cust@example.com"})
    message = _msg(Channel.WHATSAPP, "6pm, cust@example.com", "wamid.1")
    result = service.handle_inbound(message, store, route=lambda *_: decision, send=rec.send,
                                    send_email=failing_email, send_template=rec.send_template,
                                    now=NOW)

    assert result["status"] == "complete"
    state = store.get(PHONE)
    assert [t.text for t in state.turns] == ["6pm, cust@example.com", "Confirmed for 6pm."]
    assert state.receipt_message_id is None
    # A Lambda retry of the same event is dropped instead of replying twice.
    retry, _ = _run(store, rec, message, decision)
    assert retry["skipped"] == "duplicate"
    assert len(rec.sent) == 1


def test_receipt_labels_facts_readably(store, rec):
    decision = RoutingDecision(summary="Remind the customer and close", reply_channel=Channel.WHATSAPP,
                               reply_text="See you then.", conversation_complete=True,
                               key_facts={"demo_time": "tomorrow, 6 pm IST", "email": "c@example.com"})
    _run(store, rec, _msg(Channel.WHATSAPP, "ok", "wamid.1"), decision)
    [(_, _, body)] = rec.emails
    assert "- Demo time: tomorrow, 6 pm IST" in body


def test_question_closed_by_email_is_also_answered_where_it_was_asked(store, rec):
    decision = RoutingDecision(summary="remind and close", reply_channel=Channel.WHATSAPP,
                               handoff_to=Channel.EMAIL, reply_text="Your demo is at 6 pm IST.",
                               conversation_complete=True, key_facts={"email": "c@example.com"})
    _run(store, rec, _msg(Channel.WHATSAPP, "What time is my demo?", "wamid.1"), decision)
    assert rec.sent == [(Channel.WHATSAPP, PHONE, "Your demo is at 6 pm IST.")]
    assert len(rec.emails) == 1


def test_late_whatsapp_message_closed_by_email_gets_only_the_receipt(store, rec):
    decision = RoutingDecision(summary="s", reply_channel=Channel.EMAIL, reply_text="6 pm IST",
                               conversation_complete=True, key_facts={"email": "c@example.com"})
    late = NOW + timedelta(hours=25)
    _run(store, rec, _msg(Channel.WHATSAPP, "What time?", "wamid.1"), decision, now=late)
    assert rec.sent == []  # the 24-hour window is closed, so WhatsApp would refuse free text
    assert len(rec.emails) == 1


def test_email_exchange_closed_by_email_gets_only_the_receipt(store, rec):
    service.start_conversation(PHONE, store, notification="Pickup ready",
                               email="cust@example.com", send=rec.send)
    decision = RoutingDecision(summary="done", reply_channel=Channel.EMAIL, reply_text="Moved to 4pm",
                               conversation_complete=True)
    _run(store, rec, _email("4pm please"), decision)
    assert [channel for channel, *_ in rec.sent] == [Channel.SMS_RCS]  # only the opening message
    [(_, _, body)] = rec.emails
    assert body.startswith("Moved to 4pm")


def test_receipt_failure_is_logged_without_addresses(store, rec, caplog):
    def failing_email(to, subject, body_text):
        raise service.ses_email.SesSendError(f"MessageRejected: {to} for {PHONE}")

    decision = RoutingDecision(summary="s", reply_channel=Channel.WHATSAPP, reply_text="ok",
                               conversation_complete=True, key_facts={"email": "cust@example.com"})
    with caplog.at_level("INFO"):
        service.handle_inbound(_msg(Channel.WHATSAPP, "x", "wamid.1"), store, route=lambda *_: decision,
                               send=rec.send, send_email=failing_email, now=NOW)
    assert "MessageRejected" in caplog.text
    assert PHONE.lstrip("+") not in caplog.text and "cust@example.com" not in caplog.text


def test_duplicates_and_unknown_senders_are_logged_without_identifiers(store, rec, caplog):
    decision = RoutingDecision(summary="s", reply_channel=Channel.WHATSAPP, reply_text="hi")
    with caplog.at_level("INFO"):
        for _ in range(2):
            _run(store, rec, _msg(Channel.WHATSAPP, "hello", "wamid.SECRETID"), decision)
        _run(store, rec, _email("hello", sender="stranger@example.com"), decision)
        _run(store, rec, _msg(Channel.WHATSAPP, "bye", "wamid.2"),
             decision.model_copy(update={"conversation_complete": True}))
    assert "duplicate" in caplog.text and "unknown sender" in caplog.text
    assert "no email on file" in caplog.text
    for secret in ("SECRETID", "stranger@example.com", PHONE):
        assert secret not in caplog.text


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


# --- one receipt per set of details -----------------------------------------

def _done(text, channel=Channel.WHATSAPP, **facts):
    return RoutingDecision(summary="internal note", reply_channel=channel, reply_text=text,
                           conversation_complete=True, key_facts=facts)


def _booked(store, rec, **extra):
    _run(store, rec, _msg(Channel.WHATSAPP, "6 pm, c@example.com", "wamid.1"),
         _done("Booked.", email="c@example.com", demo_time="6 pm IST", **extra))
    assert len(rec.emails) == 1


@pytest.mark.parametrize("facts", [
    {},
    {"demo_time": "6 pm IST"},
    {"demo_time": " 6 PM  ist"},
    {"confirmed_time": "6 pm IST"},
    {"email": "C@Example.com"},
])
def test_question_after_completion_is_answered_without_another_receipt(store, rec, facts):
    _booked(store, rec)
    result, _ = _run(store, rec, _msg(Channel.WHATSAPP, "What time is my demo?", "wamid.2"),
                     _done("6 pm IST.", **facts))
    assert result["status"] == "complete"
    assert rec.sent[-1] == (Channel.WHATSAPP, PHONE, "6 pm IST.")
    assert len(rec.emails) == 1
    assert store.get(PHONE).receipt_message_id == "ses-1"


@pytest.mark.parametrize("facts", [
    {"demo_time": "7 pm IST"},
    {"location": "Guwahati"},
    {"email": "new@example.com"},
])
def test_changed_details_get_a_new_receipt(store, rec, facts):
    _booked(store, rec)
    _run(store, rec, _msg(Channel.WHATSAPP, "Change it", "wamid.2"), _done("Updated.", **facts))
    assert len(rec.emails) == 2
    assert rec.emails[-1][0] == facts.get("email", "c@example.com")
    _run(store, rec, _msg(Channel.WHATSAPP, "Thanks", "wamid.3"), _done("Welcome.", **facts))
    assert len(rec.emails) == 2


def test_swapped_details_get_a_new_receipt(store, rec):
    _booked(store, rec, pickup="Monday", dropoff="Tuesday")
    _run(store, rec, _msg(Channel.WHATSAPP, "Swap them", "wamid.2"),
         _done("Swapped.", pickup="Tuesday", dropoff="Monday"))
    assert len(rec.emails) == 2


def test_failed_receipt_is_retried_at_the_next_completion(store, rec):
    def failing_email(to, subject, body_text):
        raise service.ses_email.SesSendError("AccessDenied")

    service.handle_inbound(_msg(Channel.WHATSAPP, "6 pm", "wamid.1"), store, send=rec.send,
                           route=lambda *_: _done("Booked.", email="c@example.com"),
                           send_email=failing_email, now=NOW)
    _run(store, rec, _msg(Channel.WHATSAPP, "Thanks", "wamid.2"), _done("Welcome."))
    assert len(rec.emails) == 1


def test_failed_update_receipt_is_retried_and_not_reported_as_sent(store, rec):
    events = []

    def failing_email(to, subject, body_text):
        raise service.ses_email.SesSendError("AccessDenied")

    _booked(store, rec)
    service.handle_inbound(_msg(Channel.WHATSAPP, "7 pm", "wamid.2"), store, send=rec.send,
                           route=lambda *_: _done("Moved.", demo_time="7 pm IST"),
                           send_email=failing_email, now=NOW,
                           publish=lambda source, kind, data, key: events.append(data))
    assert events[-1]["receipt_sent"] is False
    _run(store, rec, _msg(Channel.WHATSAPP, "Thanks", "wamid.3"), _done("Welcome."))
    assert len(rec.emails) == 2


def test_email_question_after_the_receipt_is_answered_by_email(store, rec):
    _booked(store, rec)
    _run(store, rec, _email("What time?", sender="c@example.com"),
         _done("6 pm IST.", channel=Channel.EMAIL))
    assert rec.sent[-1] == (Channel.EMAIL, "c@example.com", "6 pm IST.")
    assert len(rec.emails) == 1


def test_answer_replaced_by_a_template_still_goes_out_in_a_receipt(store, rec, monkeypatch):
    monkeypatch.setenv("OMNIDAMUS_WHATSAPP_HANDOFF_TEMPLATE", "continue_chat")
    _booked(store, rec)
    late = NOW + timedelta(hours=50)
    _run(store, rec, _msg(Channel.SMS_RCS, "What time?", "sms-2", at=late),
         _done("6 pm IST."), now=late)
    assert rec.sent[-1][2].startswith("template:continue_chat")
    assert rec.emails[-1][2].startswith("6 pm IST.") and len(rec.emails) == 2


def test_late_whatsapp_question_after_the_receipt_is_still_answered(store, rec):
    _booked(store, rec)
    late = NOW + timedelta(hours=50)
    _run(store, rec, _msg(Channel.WHATSAPP, "What time?", "wamid.2", at=NOW),
         _done("6 pm IST.", channel=Channel.EMAIL), now=late)
    assert rec.sent[-1] == (Channel.EMAIL, "c@example.com", "6 pm IST.")
    assert len(rec.emails) == 1


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


def test_start_while_subscribed_never_reaches_the_agent(store, rec):
    done = RoutingDecision(summary="done", reply_channel=Channel.WHATSAPP, reply_text="Booked",
                           conversation_complete=True, key_facts={"email": "c@example.com"})
    _run(store, rec, _msg(Channel.WHATSAPP, "book it", "wamid.1"), done)
    result = service.handle_inbound(_msg(Channel.WHATSAPP, "START", "wamid.2"), store, route=_never_route,
                                    send=rec.send, send_email=rec.send_email, now=NOW)
    assert result["status"] == "already_opted_in"
    assert rec.sent[-1] == (Channel.WHATSAPP, PHONE, service.ALREADY_OPTED_IN)
    assert len(rec.emails) == 1 and store.get(PHONE).status == "complete"


def test_start_from_a_new_phone_never_reaches_the_agent(store, rec):
    result = service.handle_inbound(_msg(Channel.WHATSAPP, "Start", "wamid.1"), store, route=_never_route,
                                    send=rec.send, now=NOW)
    assert result["status"] == "already_opted_in"
    assert store.get(PHONE).last_whatsapp_inbound_at == NOW


def test_sentence_containing_start_still_reaches_the_agent(store, rec):
    decision = RoutingDecision(summary="s", reply_channel=Channel.WHATSAPP, reply_text="At 9")
    result, _ = _run(store, rec, _msg(Channel.WHATSAPP, "When does it start?", "wamid.1"), decision)
    assert result["status"] == "open"


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

def _status(message_id, status, errors=()):
    return {"message_id": message_id, "status": status, "errors": list(errors)}


def test_whatsapp_status_reaches_the_outbound_turn(store, rec):
    out = service.start_conversation(PHONE, store, channel=Channel.WHATSAPP, template="demo_request",
                                     send=rec.send, send_template=rec.send_template)
    assert service.apply_whatsapp_status(_status(out["message_id"], "read"), store) is True
    decision = RoutingDecision(summary="s", reply_channel=Channel.WHATSAPP, reply_text="hi")
    _run(store, rec, _msg(Channel.WHATSAPP, "hello", "wamid.1"), decision)
    assert store.get(PHONE).turns[0].status == "read"


def test_late_delivered_does_not_replace_read(store):
    # Meta sent "read" before "delivered" on 25 Nov 2026.
    for status, recorded in (("accepted", True), ("sent", True), ("read", True), ("delivered", False)):
        assert service.apply_whatsapp_status(_status("aws-1", status), store) is recorded
    assert store.delivery_status("aws-1") == "read"


def test_status_arriving_mid_reply_does_not_conflict_with_the_reply(store, rec):
    """A conflict here would make Lambda retry the event and send the reply twice."""
    out = service.start_conversation(PHONE, store, channel=Channel.WHATSAPP, template="demo_request",
                                     send=rec.send, send_template=rec.send_template)

    def route(*_):
        service.apply_whatsapp_status(_status(out["message_id"], "read"), store)
        return RoutingDecision(summary="s", reply_channel=Channel.WHATSAPP, reply_text="hi")

    result = service.handle_inbound(_msg(Channel.WHATSAPP, "hello", "wamid.1"), store, route=route,
                                    send=rec.send, now=NOW)
    assert result["status"] == "open"
    assert len(rec.sent) == 2


def test_delivery_records_hold_no_conversation_or_phone(store):
    service.apply_whatsapp_status(_status("aws-1", "sent"), store)
    [item] = store._table.scan()["Items"]
    assert set(item) == {"conversation_id", "status", "rank", "expires_at"}
    assert item["conversation_id"] == "delivery#aws-1"


def test_status_is_logged_without_phone_number(store, caplog):
    with caplog.at_level("INFO", logger="omnidamus.service"):
        service.apply_whatsapp_status(
            _status("wamid.HBgLMTU1NTAwMDExMTE", "failed", [{"code": 131047, "message": f"to {PHONE}"}]), store)
    assert "131047" in caplog.text and "failed" in caplog.text
    assert "HBgLMTU1NTAwMDExMTE" not in caplog.text and PHONE.lstrip("+") not in caplog.text
