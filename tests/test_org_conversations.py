"""Stack org routing (2026-11-20): AXXESS starts conversations for an organization, from that
organization's own number. Each organization's conversation with a phone is kept apart from
Triaxis's (Tenant 0) and from every other organization's, and its result carries the job."""

from __future__ import annotations

import json

import jsonschema
import pytest

from omnidamus import inbound, service, still_emit
from omnidamus.schema import Channel, InboundMessage, RoutingDecision
from tests.test_service import NOW, PHONE
from tests.test_still_publish import BUS, SCHEMA, FakeEvents

TRIAXIS_LINE = "+61400000001"
ORG_A, LINE_A, JOB_A = "11111111-2222-3333-4444-555555555555", "+971500000001", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ORG_B, LINE_B, JOB_B = "22222222-3333-4444-5555-666666666666", "+919800000002", "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"


class OrgRecorder:
    """Records each send with the sender identity it used."""

    def __init__(self):
        self.sent = []

    def send(self, address, decision, **origin):
        self.sent.append((decision.reply_channel, address, decision.reply_text, origin))
        return f"msg-{len(self.sent)}"

    def send_template(self, to, name, language_code="en", body_parameters=None, **origin):
        self.sent.append((Channel.WHATSAPP, to, f"template:{name}", origin))
        return f"wamid.out{len(self.sent)}"

    def send_email(self, to, subject, body_text):
        raise AssertionError("no receipt email for an organization's conversation")


@pytest.fixture(autouse=True)
def triaxis_line(monkeypatch):
    monkeypatch.setenv("OMNIDAMUS_SMS_RCS_ORIGINATION_IDENTITY", TRIAXIS_LINE)
    monkeypatch.delenv("OMNIDAMUS_WHATSAPP_DISPLAY_NUMBER", raising=False)
    monkeypatch.delenv("OMNIDAMUS_TENANT0_LINES", raising=False)


@pytest.fixture
def rec():
    return OrgRecorder()


def _start(store, rec, org=ORG_A, line=LINE_A, job=JOB_A, **kw):
    kw.setdefault("notification", f"Hello from {org[:4]}")
    kw.setdefault("sms_origination_identity", f"sender-{org[:4]}")
    return service.start_conversation(PHONE, store, context="lead", send=rec.send,
                                      send_template=rec.send_template, organization_id=org,
                                      job_id=job, line=line, **kw)


def _sms(body, mid, to):
    return InboundMessage(conversation_id=PHONE, channel=Channel.SMS_RCS, from_address=PHONE,
                          to_address=to, body=body, provider_message_id=mid, received_at=NOW)


def _handle(store, rec, message, decision, publish=lambda *a, **k: None):
    return service.handle_inbound(message, store, route=lambda *_: decision, send=rec.send,
                                  send_email=rec.send_email, send_template=rec.send_template,
                                  now=NOW, publish=publish)


def test_two_organizations_messaging_one_phone_get_separate_conversations(store, rec):
    a = _start(store, rec)
    b = _start(store, rec, org=ORG_B, line=LINE_B, job=JOB_B)
    assert a["conversation_id"] == f"line:{LINE_A}#{PHONE}"
    assert b["conversation_id"] == f"line:{LINE_B}#{PHONE}"
    assert store.get(a["conversation_id"]).organization_id == ORG_A
    assert store.get(b["conversation_id"]).job_id == JOB_B
    assert store.get(PHONE) is None, "Triaxis's own conversation with the phone is untouched"
    assert [s[3] for s in rec.sent] == [{"sms_origination_identity": "sender-1111"},
                                        {"sms_origination_identity": "sender-2222"}]


def test_a_reply_to_an_organizations_number_continues_its_conversation_from_its_sender(store, rec):
    _start(store, rec)
    _start(store, rec, org=ORG_B, line=LINE_B, job=JOB_B)
    decision = RoutingDecision(summary="s", reply_channel=Channel.SMS_RCS, reply_text="Thanks")
    result = _handle(store, rec, _sms("yes please", "sms-1", LINE_B), decision)
    assert result["conversation_id"] == f"line:{LINE_B}#{PHONE}"
    assert rec.sent[-1] == (Channel.SMS_RCS, PHONE, "Thanks", {"sms_origination_identity": "sender-2222"})
    assert len(store.get(f"line:{LINE_A}#{PHONE}").turns) == 1, "the other organization's thread is untouched"


def test_a_message_to_an_unknown_organization_number_is_dropped(store, rec):
    decision = RoutingDecision(summary="s", reply_channel=Channel.SMS_RCS, reply_text="never sent")
    assert _handle(store, rec, _sms("hi", "sms-1", "+971500009999"), decision) == {"skipped": "unknown_line"}
    assert rec.sent == []


def test_triaxis_conversations_keep_the_phone_key_and_default_sender(store, rec):
    decision = RoutingDecision(summary="s", reply_channel=Channel.SMS_RCS, reply_text="Hi")
    result = _handle(store, rec, _sms("hello", "sms-1", TRIAXIS_LINE), decision)
    assert result["conversation_id"] == PHONE
    assert rec.sent == [(Channel.SMS_RCS, PHONE, "Hi", {})]
    result = _handle(store, rec, _sms("again", "sms-2", None), decision)
    assert result["conversation_id"] == PHONE


@pytest.mark.parametrize("kw, reason", [
    ({"line": TRIAXIS_LINE}, "Triaxis number"),
    ({"line": None}, "own number"),
    ({"job": None}, "job_id"),
    ({"sms_origination_identity": None}, "SMS sender"),
    ({"channel": Channel.WHATSAPP, "template": "t"}, "WhatsApp sender"),
    ({"channel": Channel.EMAIL}, "email"),
])
def test_unsafe_organization_starts_are_refused(store, rec, kw, reason):
    with pytest.raises(service.StartRefused, match=reason):
        _start(store, rec, **kw)
    assert rec.sent == []


def test_tenant0_lines_setting_also_protects_extra_numbers(store, rec, monkeypatch):
    monkeypatch.setenv("OMNIDAMUS_TENANT0_LINES", "+61400000002, +61400000003")
    with pytest.raises(service.StartRefused):
        _start(store, rec, line="+61400000003")


def test_whatsapp_start_uses_the_organizations_phone_number_id(store, rec):
    _start(store, rec, channel=Channel.WHATSAPP, template="intro", notification=None,
           sms_origination_identity=None, whatsapp_origination_id="phone-number-id-a")
    assert rec.sent == [(Channel.WHATSAPP, PHONE, "template:intro",
                         {"origination_phone_number_id": "phone-number-id-a"})]


def test_an_opt_out_is_per_organization(store, rec):
    _start(store, rec)
    stop = RoutingDecision(summary="s", reply_channel=Channel.SMS_RCS, reply_text="")
    _handle(store, rec, _sms("STOP", "sms-1", LINE_A), stop)
    assert store.get(f"line:{LINE_A}#{PHONE}").opted_out is True
    assert _start(store, rec)["skipped"] == "opted_out"
    assert "message_id" in _start(store, rec, org=ORG_B, line=LINE_B, job=JOB_B)


def test_completion_carries_the_job_and_no_email(store, rec, monkeypatch):
    monkeypatch.setenv(still_emit.BUS_ENV, BUS)
    events = FakeEvents()
    _start(store, rec, email="cust@example.com")
    assert store.get(f"line:{LINE_A}#{PHONE}").customer_email is None
    done = RoutingDecision(summary="Demo booked", reply_channel=Channel.SMS_RCS, reply_text="Booked",
                           conversation_complete=True,
                           key_facts={"demo_time": "Fri 3pm", "email": "cust@example.com"})
    _handle(store, rec, _sms("Fri 3pm", "sms-9", LINE_A), done,
            publish=lambda *a, **k: still_emit.emit(*a, client=events, **k))
    [entry] = events.entries
    data = json.loads(entry["Detail"])["data"]
    jsonschema.validate(data, SCHEMA)
    assert data["job_id"] == JOB_A and data["organization_id"] == ORG_A
    assert store.find_by_email("cust@example.com") is None, \
        "an email given in an organization's chat never routes inbound email to it"


def test_whatsapp_receipts_are_keyed_by_message_not_by_customer():
    entry = {"changes": [{"value": {"metadata": {"display_phone_number": LINE_A.lstrip("+")},
                                    "statuses": [{"id": "wamid.1", "recipient_id": PHONE.lstrip("+"),
                                                  "status": "read"}]}}]}
    [status] = inbound.parse_whatsapp_statuses(json.dumps({"messageId": "aws-1",
                                                           "whatsAppWebhookEntry": json.dumps(entry)}))
    assert status == {"message_id": "aws-1", "status": "read", "errors": []}


def test_sms_inbound_records_the_number_it_was_sent_to():
    payload = {"originationNumber": PHONE, "destinationNumber": LINE_B, "messageBody": "hi",
               "inboundMessageId": "m1"}
    [message] = inbound.parse_sms_event(json.dumps(payload))
    assert message.to_address == LINE_B
