"""Router dispatch logic, tested with the channel wrappers mocked."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from omnidamus.router import dispatch_handoff, dispatch_reply
from omnidamus.schema import Channel, RoutingDecision


def test_dispatch_reply_routes_to_sms_rcs():
    decision = RoutingDecision(
        summary="ack", reply_channel=Channel.SMS_RCS, reply_text="hi"
    )
    with patch("omnidamus.router.sms_rcs.send_text_message", return_value="sms-1") as mock_send:
        message_id = dispatch_reply("+15550001111", decision)
    assert message_id == "sms-1"
    mock_send.assert_called_once_with("+15550001111", "hi")


def test_dispatch_reply_routes_to_whatsapp():
    decision = RoutingDecision(
        summary="ack", reply_channel=Channel.WHATSAPP, reply_text="hi"
    )
    with patch(
        "omnidamus.router.whatsapp_social.send_text_message", return_value="wa-1"
    ) as mock_send:
        message_id = dispatch_reply("+15550001111", decision)
    assert message_id == "wa-1"
    mock_send.assert_called_once_with("+15550001111", "hi")


def test_dispatch_reply_routes_to_email():
    decision = RoutingDecision(
        summary="Receipt", reply_channel=Channel.EMAIL, reply_text="Confirmed."
    )
    with patch("omnidamus.router.ses_email.send_email", return_value="ses-1") as mock_send:
        message_id = dispatch_reply("to@example.com", decision)
    assert message_id == "ses-1"
    mock_send.assert_called_once_with("to@example.com", subject="Receipt", body_text="Confirmed.")


def test_dispatch_handoff_returns_none_when_no_handoff():
    decision = RoutingDecision(summary="ack", reply_channel=Channel.SMS_RCS, reply_text="hi")
    assert dispatch_handoff("+15550001111", decision) is None


def test_dispatch_handoff_notifies_new_channel():
    decision = RoutingDecision(
        summary="Let's continue on WhatsApp",
        reply_channel=Channel.SMS_RCS,
        reply_text="We'll continue this on WhatsApp.",
        handoff_to=Channel.WHATSAPP,
    )
    with patch(
        "omnidamus.router.whatsapp_social.send_text_message", return_value="wa-2"
    ) as mock_send:
        message_id = dispatch_handoff("+15550001111", decision)
    assert message_id == "wa-2"
    mock_send.assert_called_once_with("+15550001111", "We'll continue this on WhatsApp.")
