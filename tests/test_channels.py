"""Channel wrappers, tested with the underlying boto3 clients mocked -- no live
AWS credentials or resources required, matching this org's existing testing
convention (see apps/callismatic's mocked-boundary test suite for precedent)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from omnidamus.channels import ses_email, sms_rcs, whatsapp_social


def test_send_email_success(monkeypatch):
    monkeypatch.setenv("OMNIDAMUS_SES_FROM_ADDRESS", "sender@example.com")
    with patch("boto3.client") as mock_client:
        mock_client.return_value.send_email.return_value = {"MessageId": "ses-123"}
        message_id = ses_email.send_email("to@example.com", "Subject", "Body")
    assert message_id == "ses-123"
    mock_client.return_value.send_email.assert_called_once()
    call_kwargs = mock_client.return_value.send_email.call_args.kwargs
    assert call_kwargs["FromEmailAddress"] == "sender@example.com"
    assert call_kwargs["Destination"] == {"ToAddresses": ["to@example.com"]}


def test_send_email_missing_sender_raises(monkeypatch):
    monkeypatch.delenv("OMNIDAMUS_SES_FROM_ADDRESS", raising=False)
    with pytest.raises(ses_email.SesSendError):
        ses_email.send_email("to@example.com", "Subject", "Body")


def test_send_text_message_success(monkeypatch):
    monkeypatch.setenv("OMNIDAMUS_SMS_RCS_ORIGINATION_IDENTITY", "pool-abc123")
    with patch("boto3.client") as mock_client:
        mock_client.return_value.send_text_message.return_value = {"MessageId": "sms-123"}
        message_id = sms_rcs.send_text_message("+15550001111", "hello")
    assert message_id == "sms-123"
    call_kwargs = mock_client.return_value.send_text_message.call_args.kwargs
    assert call_kwargs["OriginationIdentity"] == "pool-abc123"


def test_send_text_message_missing_identity_raises(monkeypatch):
    monkeypatch.delenv("OMNIDAMUS_SMS_RCS_ORIGINATION_IDENTITY", raising=False)
    with pytest.raises(sms_rcs.SmsRcsSendError):
        sms_rcs.send_text_message("+15550001111", "hello")


def test_send_rich_rcs_message_not_implemented():
    with pytest.raises(NotImplementedError):
        sms_rcs.send_rich_rcs_message()


def test_whatsapp_send_text_message_success(monkeypatch):
    monkeypatch.setenv("OMNIDAMUS_WHATSAPP_ORIGINATION_PHONE_NUMBER_ID", "phone-number-id-abc")
    with patch("boto3.client") as mock_client:
        mock_client.return_value.send_whatsapp_message.return_value = {"messageId": "wa-123"}
        message_id = whatsapp_social.send_text_message("+15550001111", "hello")
    assert message_id == "wa-123"
    call_kwargs = mock_client.return_value.send_whatsapp_message.call_args.kwargs
    assert call_kwargs["originationPhoneNumberId"] == "phone-number-id-abc"
    assert b'"to": "+15550001111"' in call_kwargs["message"]


def test_whatsapp_send_text_message_missing_id_raises(monkeypatch):
    monkeypatch.delenv("OMNIDAMUS_WHATSAPP_ORIGINATION_PHONE_NUMBER_ID", raising=False)
    with pytest.raises(whatsapp_social.WhatsAppSendError):
        whatsapp_social.send_text_message("+15550001111", "hello")


def test_send_whatsapp_template_message(monkeypatch):
    import json as _json

    monkeypatch.setenv("OMNIDAMUS_WHATSAPP_ORIGINATION_PHONE_NUMBER_ID", "phone-number-id-abc")
    with patch("boto3.client") as mock_client:
        mock_client.return_value.send_whatsapp_message.return_value = {"messageId": "wa-t1"}
        message_id = whatsapp_social.send_template_message(
            "+14255550123", "demo_request", language_code="hi", body_parameters=["Asha", "AXXESS"]
        )
    assert message_id == "wa-t1"
    kwargs = mock_client.return_value.send_whatsapp_message.call_args.kwargs
    body = _json.loads(kwargs["message"])
    assert body["type"] == "template"
    assert body["template"]["name"] == "demo_request"
    assert body["template"]["language"] == {"code": "hi"}
    assert body["template"]["components"] == [
        {"type": "body", "parameters": [{"type": "text", "text": "Asha"}, {"type": "text", "text": "AXXESS"}]}
    ]


def test_send_email_sets_reply_to(monkeypatch):
    monkeypatch.setenv("OMNIDAMUS_SES_FROM_ADDRESS", "sender@example.com")
    monkeypatch.setenv("OMNIDAMUS_SES_REPLY_TO", "replies@example.com")
    with patch("boto3.client") as mock_client:
        mock_client.return_value.send_email.return_value = {"MessageId": "ses-9"}
        ses_email.send_email("to@example.com", "S", "B")
    assert mock_client.return_value.send_email.call_args.kwargs["ReplyToAddresses"] == ["replies@example.com"]
