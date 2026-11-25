from __future__ import annotations

import json
import logging
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

import lambda_handler
from omnidamus.channels.whatsapp_social import WhatsAppSendError
from omnidamus.inbound import normalize_phone
from omnidamus.schema import RoutingDecision
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
    with pytest.raises(lambda_handler.HandlerError, match="Unrecognized event"):
        lambda_handler.handler({"hello": "world"})


SECRETS = ("919800000000", "98000 00000", "c@example.com", "wamid.SECRET", "my private words")


def _client_error(message):
    return ClientError({"Error": {"Code": "ValidationException", "Message": message}}, "SendWhatsAppMessage")


def _send_error():
    try:
        raise _client_error("to +919800000000 (+91 98000 00000) c@example.com wamid.SECRET")
    except ClientError as exc:
        raise WhatsAppSendError(f"send_whatsapp_message failed: {exc}") from exc


def _model_error():
    RoutingDecision.model_validate({"reply_text": "my private words", "reply_channel": "c@example.com"})


def _bare_client_error():
    raise _client_error("my private words")


def _key_error():
    raise KeyError("my private words")


@pytest.mark.parametrize("fail, kind", [
    (_send_error, "WhatsAppSendError"),
    (_model_error, "ValidationError"),
    (_bare_client_error, "ClientError"),
    (_key_error, "KeyError"),
])
def test_unhandled_errors_reach_the_log_without_customer_data(fail, kind, caplog):
    sms = {"originationNumber": "+14255550182", "messageBody": "hi", "inboundMessageId": "m1"}
    with patch("omnidamus.sns_verify.verify"), \
         patch("omnidamus.service.handle_inbound", side_effect=lambda *_: fail()), \
         caplog.at_level("INFO"), pytest.raises(lambda_handler.HandlerError) as raised:
        lambda_handler.handler({"Records": [_record(SMS_TOPIC, sms)]})
    # Lambda logs the message of whatever leaves the handler, so nothing may be chained to it.
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    seen = caplog.text + str(raised.value)
    assert kind in seen and "test_lambda_handler.py" in seen
    for secret in SECRETS:
        assert secret not in seen


def test_send_errors_keep_the_reason_aws_gave():
    with patch("omnidamus.service.start_conversation", side_effect=lambda **_: _send_error()), \
         pytest.raises(lambda_handler.HandlerError, match="ValidationException"):
        lambda_handler.handler({"action": "start_conversation", "phone": "+14255550182",
                                "notification": "x"})


def test_agent_library_logs_are_off():
    # Strands logs the model's raw text when its output does not parse.
    assert not logging.getLogger("strands.event_loop.streaming").isEnabledFor(logging.ERROR)


def test_bad_phone_error_does_not_repeat_the_input():
    with pytest.raises(ValueError) as raised:
        normalize_phone("my private words")
    assert "private" not in str(raised.value)


EMAIL_TOPIC = "arn:aws:sns:us-east-1:123456789012:omnidamus-email-inbound"


def test_email_topic_is_accepted_when_configured(monkeypatch):
    monkeypatch.setenv("OMNIDAMUS_EMAIL_TOPIC_ARN", EMAIL_TOPIC)
    with patch("omnidamus.sns_verify.verify") as verify, \
         patch("omnidamus.inbound.parse_ses_event", return_value=[]) as parse:
        lambda_handler.handler({"Records": [_record(EMAIL_TOPIC, {})]})
    assert EMAIL_TOPIC in set(verify.call_args.kwargs["allowed_topic_arns"])
    parse.assert_called_once()


def test_unconfigured_topics_are_not_allowlisted(monkeypatch):
    monkeypatch.delenv("OMNIDAMUS_EMAIL_TOPIC_ARN", raising=False)
    with patch("omnidamus.sns_verify.verify") as verify, \
         patch("omnidamus.service.handle_inbound", return_value={}):
        lambda_handler.handler({"Records": [_record(SMS_TOPIC, {"messageBody": "x", "originationNumber": "+1555"})]})
    assert "" not in set(verify.call_args.kwargs["allowed_topic_arns"])


def test_whatsapp_statuses_are_applied():
    status = {"message_id": "aws-1", "status": "read", "errors": []}
    with patch("omnidamus.sns_verify.verify"), \
         patch("omnidamus.inbound.parse_whatsapp_event", return_value=[]), \
         patch("omnidamus.inbound.parse_whatsapp_statuses", return_value=[status]), \
         patch("omnidamus.service.apply_whatsapp_status") as apply:
        lambda_handler.handler({"Records": [_record(WA_TOPIC, {})]})
    apply.assert_called_once()
    assert apply.call_args.args[0] == status


def test_start_conversation_on_whatsapp_with_template():
    with patch("omnidamus.service.start_conversation", return_value={}) as start:
        lambda_handler.handler({"action": "start_conversation", "phone": "+14255550182",
                                "channel": "whatsapp", "template": "demo_request",
                                "template_params": ["Asha"]})
    kwargs = start.call_args.kwargs
    assert kwargs["channel"].value == "whatsapp"
    assert kwargs["template"] == "demo_request" and kwargs["template_params"] == ["Asha"]
