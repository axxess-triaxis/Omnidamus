"""Inbound parsers, fed the example payloads from AWS's documentation."""

from __future__ import annotations

import json

import pytest

from omnidamus.inbound import normalize_phone, parse_sms_event, parse_whatsapp_event
from omnidamus.schema import Channel

HEADER = {
    "context": {
        "MetaWabaIds": [{"wabaId": "1234567890abcde", "arn": "arn:aws:social-messaging:us-east-1:123456789012:waba/x"}],
        "MetaPhoneNumberIds": [{"metaPhoneNumberId": "abcde1234567890", "arn": "arn:aws:social-messaging:us-east-1:123456789012:phone-number-id/y"}],
    },
    "aws_account_id": "123456789012",
    "message_timestamp": "2025-01-08T23:30:43.271279391Z",
    "messageId": "6d69f07a-c317-4278-9d5c-6a84078419ec",
}


def _wa_event(value: dict) -> str:
    entry = {"id": "503131219501234", "changes": [{"value": value, "field": "messages"}]}
    return json.dumps({**HEADER, "whatsAppWebhookEntry": json.dumps(entry)})


def test_whatsapp_text_message():
    event = _wa_event({
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "12065550100", "phone_number_id": "321010217760100"},
        "contacts": [{"profile": {"name": "Diego"}, "wa_id": "12065550102"}],
        "messages": [{"from": "14255550150", "id": "wamid.ABC", "timestamp": "1723506230",
                      "type": "text", "text": {"body": "Can I come at 3?"}}],
    })
    [msg] = parse_whatsapp_event(event)
    assert msg.channel is Channel.WHATSAPP
    assert msg.conversation_id == msg.from_address == "+14255550150"
    assert msg.body == "Can I come at 3?"
    assert msg.provider_message_id == "wamid.ABC"
    assert msg.received_at.year == 2024


def test_whatsapp_button_reply():
    event = _wa_event({"messages": [{"from": "14255550150", "id": "wamid.B", "type": "interactive",
                                     "interactive": {"type": "button_reply",
                                                     "button_reply": {"id": "b1", "title": "Book a demo"}}}]})
    [msg] = parse_whatsapp_event(event)
    assert msg.body == "Book a demo"


def test_whatsapp_status_receipts_and_media_are_skipped():
    statuses = _wa_event({"statuses": [{"id": "wamid.X", "status": "sent", "recipient_id": "01234567890"}]})
    media = _wa_event({"messages": [{"from": "14255550150", "id": "wamid.I", "type": "image",
                                     "image": {"id": "530339869524171"}}]})
    assert parse_whatsapp_event(statuses) == []
    assert parse_whatsapp_event(media) == []


def test_sms_two_way_payload():
    payload = json.dumps({
        "originationNumber": "+14255550182",
        "destinationNumber": "+12125550101",
        "messageKeyword": "JOIN",
        "messageBody": "EXAMPLE",
        "inboundMessageId": "cae173d2-66b9-564c-8309-21f858e9fb84",
        "previousPublishedMessageId": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    })
    [msg] = parse_sms_event(payload)
    assert msg.channel is Channel.SMS_RCS
    assert msg.conversation_id == "+14255550182"
    assert msg.body == "EXAMPLE"
    assert msg.provider_message_id == "cae173d2-66b9-564c-8309-21f858e9fb84"


def test_whatsapp_and_sms_from_same_person_share_a_conversation():
    assert normalize_phone("14255550150") == normalize_phone("+1 425-555-0150")


def test_not_a_phone_number():
    with pytest.raises(ValueError):
        normalize_phone("abc")


# --- WhatsApp delivery receipts ---------------------------------------------

def test_whatsapp_statuses_are_parsed():
    from omnidamus.inbound import parse_whatsapp_statuses

    event = _wa_event({"statuses": [{"id": "wamid.X", "status": "failed", "recipient_id": "01234567890",
                                     "errors": [{"code": 131047, "title": "Re-engagement message"}]}]})
    [status] = parse_whatsapp_statuses(event)
    assert status == {"conversation_id": "+01234567890", "provider_message_id": "wamid.X",
                      "status": "failed", "errors": [{"code": 131047, "title": "Re-engagement message"}]}


def test_event_outline_has_ids_and_errors_but_no_phone_numbers():
    from omnidamus.inbound import outline_whatsapp_event

    event = _wa_event({"metadata": {"display_phone_number": "12065550100"},
                       "statuses": [{"id": "wamid.X", "status": "failed", "recipient_id": "14255550150",
                                     "errors": [{"code": 131047}]}]})
    outline = outline_whatsapp_event(event)
    assert outline["aws_message_id"] == HEADER["messageId"]
    assert outline["changes"][0]["statuses"] == [{"id": "wamid.X", "status": "failed", "errors": [{"code": 131047}]}]
    assert "14255550150" not in json.dumps(outline) and "12065550100" not in json.dumps(outline)


# --- SES receiving -----------------------------------------------------------

RAW_REPLY = (
    "From: Asha Rao <asha@example.com>\r\n"
    "To: replies@omnidamus.example\r\n"
    "Subject: Re: Confirmation\r\n"
    "Content-Type: text/plain; charset=UTF-8\r\n\r\n"
    "Can we move it to 4pm?\r\n\r\n"
    "On Fri, 24 Oct 2026 at 10:00, Omnidamus <noreply@example.com> wrote:\r\n"
    "> Booked for 3pm.\r\n"
)


def _ses(content=RAW_REPLY, spam="PASS", virus="PASS"):
    return json.dumps({
        "notificationType": "Received",
        "mail": {"source": "bounce@example.com", "messageId": "o3vrnil0e2ic28trm7dfhrc2v0clambda4nbp0g81",
                 "commonHeaders": {"from": ["Asha Rao <asha@example.com>"], "subject": "Re: Confirmation"}},
        "receipt": {"spamVerdict": {"status": spam}, "virusVerdict": {"status": virus},
                    "action": {"type": "SNS", "topicArn": "arn:aws:sns:us-east-1:123456789012:email"}},
        "content": content,
    })


def test_ses_reply_keeps_only_new_text():
    from omnidamus.inbound import parse_ses_event

    [msg] = parse_ses_event(_ses())
    assert msg.channel is Channel.EMAIL
    assert msg.from_address == "asha@example.com"
    assert msg.body == "Can we move it to 4pm?"
    assert msg.provider_message_id == "o3vrnil0e2ic28trm7dfhrc2v0clambda4nbp0g81"


@pytest.mark.parametrize("spam,virus", [("FAIL", "PASS"), ("PASS", "FAIL")])
def test_ses_spam_or_virus_is_dropped(spam, virus):
    from omnidamus.inbound import parse_ses_event

    assert parse_ses_event(_ses(spam=spam, virus=virus)) == []


def test_ses_multipart_uses_plain_part():
    from omnidamus.inbound import parse_ses_event

    raw = (
        "From: asha@example.com\r\nSubject: hi\r\nMIME-Version: 1.0\r\n"
        "Content-Type: multipart/alternative; boundary=b1\r\n\r\n"
        "--b1\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\nPlain body\r\n"
        "--b1\r\nContent-Type: text/html; charset=UTF-8\r\n\r\n<p>Html body</p>\r\n--b1--\r\n"
    )
    [msg] = parse_ses_event(_ses(content=raw))
    assert msg.body == "Plain body"
