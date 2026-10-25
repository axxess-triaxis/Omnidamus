from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import lambda_handler
from omnidamus.sns_verify import SnsVerificationError

WA_TOPIC = "arn:aws:sns:us-east-1:123456789012:omnidamus-whatsapp-inbound"
SMS_TOPIC = "arn:aws:sns:us-east-1:123456789012:omnidamus-sms-inbound"


@pytest.fixture(autouse=True)
def topics(monkeypatch):
    monkeypatch.setenv("OMNIDAMUS_WHATSAPP_TOPIC_ARN", WA_TOPIC)
    monkeypatch.setenv("OMNIDAMUS_SMS_TOPIC_ARN", SMS_TOPIC)
    monkeypatch.setattr(lambda_handler, "_store", object())


def _record(topic, message):
    return {"EventSource": "aws:sns", "Sns": {"TopicArn": topic, "Message": json.dumps(message)}}


def test_sms_record_is_verified_parsed_and_handled():
    sms = {"originationNumber": "+14255550182", "messageBody": "hi", "inboundMessageId": "m1"}
    with patch("omnidamus.sns_verify.verify") as verify, \
         patch("omnidamus.service.handle_inbound", return_value={"ok": 1}) as handle:
        out = lambda_handler.handler({"Records": [_record(SMS_TOPIC, sms)]})
    verify.assert_called_once()
    assert set(verify.call_args.kwargs["allowed_topic_arns"]) == {WA_TOPIC, SMS_TOPIC}
    assert handle.call_args.args[0].body == "hi"
    assert out == {"results": [{"ok": 1}]}


def test_rejected_record_is_dropped_not_handled():
    with patch("omnidamus.sns_verify.verify", side_effect=SnsVerificationError("bad")), \
         patch("omnidamus.service.handle_inbound") as handle:
        out = lambda_handler.handler({"Records": [_record(SMS_TOPIC, {})]})
    handle.assert_not_called()
    assert out == {"results": []}


def test_start_conversation_action():
    with patch("omnidamus.service.start_conversation", return_value={"message_id": "x"}) as start:
        lambda_handler.handler({"action": "start_conversation", "phone": "1 425 555 0182",
                                "notification": "Your pickup is ready", "email": "c@example.com"})
    assert start.call_args.kwargs["phone"] == "+14255550182"


def test_unrecognized_event_raises():
    with pytest.raises(ValueError):
        lambda_handler.handler({"hello": "world"})
