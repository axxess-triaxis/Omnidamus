"""Turn each channel's inbound SNS payload into InboundMessage objects.

Payload shapes are taken from AWS's own documentation:

- AWS End User Messaging Social, "Message and event format": the SNS message is a
  JSON header (`context`, `aws_account_id`, `message_timestamp`, `messageId`) plus
  `whatsAppWebhookEntry`, which is Meta's webhook entry *as a JSON string*. Inbound
  messages sit at changes[].value.messages[]; delivery receipts at
  changes[].value.statuses[] (ignored here).
- AWS End User Messaging SMS, "Example of a two-way SMS message payload":
  originationNumber, destinationNumber, messageKeyword, messageBody,
  inboundMessageId, previousPublishedMessageId.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .schema import Channel, InboundMessage


def normalize_phone(number: str) -> str:
    """E.164 with a leading '+'. WhatsApp sends bare digits; SMS sends '+'."""

    digits = "".join(ch for ch in number if ch.isdigit())
    if not digits:
        raise ValueError(f"Not a phone number: {number!r}")
    return f"+{digits}"


def parse_whatsapp_event(sns_message: str) -> list[InboundMessage]:
    """Inbound customer messages in one EUM Social event. Text and interactive
    button/list replies become InboundMessages; status receipts and other message
    types (media, location, ...) are skipped."""

    event = json.loads(sns_message)
    entry = event.get("whatsAppWebhookEntry")
    if isinstance(entry, str):
        entry = json.loads(entry)
    if not entry:
        return []

    messages: list[InboundMessage] = []
    for change in entry.get("changes", []):
        for msg in change.get("value", {}).get("messages", []):
            body = _whatsapp_text(msg)
            if body is None:
                continue
            phone = normalize_phone(msg["from"])
            received_at = (
                datetime.fromtimestamp(int(msg["timestamp"]), tz=timezone.utc)
                if msg.get("timestamp")
                else datetime.now(timezone.utc)
            )
            messages.append(
                InboundMessage(
                    conversation_id=phone,
                    channel=Channel.WHATSAPP,
                    from_address=phone,
                    body=body,
                    received_at=received_at,
                    provider_message_id=msg.get("id"),
                )
            )
    return messages


def _whatsapp_text(msg: dict) -> str | None:
    msg_type = msg.get("type")
    if msg_type == "text":
        return msg.get("text", {}).get("body")
    if msg_type == "button":
        return msg.get("button", {}).get("text")
    if msg_type == "interactive":
        interactive = msg.get("interactive", {})
        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
        return reply.get("title")
    return None


def parse_sms_event(sns_message: str) -> list[InboundMessage]:
    payload = json.loads(sns_message)
    body = payload.get("messageBody")
    sender = payload.get("originationNumber")
    if not body or not sender:
        return []
    phone = normalize_phone(sender)
    return [
        InboundMessage(
            conversation_id=phone,
            channel=Channel.SMS_RCS,
            from_address=phone,
            body=body,
            provider_message_id=payload.get("inboundMessageId"),
        )
    ]
