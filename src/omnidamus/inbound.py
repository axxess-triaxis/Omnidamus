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
import re
from datetime import datetime, timezone
from email import message_from_string
from email import policy as email_policy
from email.utils import parseaddr

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


def outline_whatsapp_event(sns_message: str) -> dict:
    """A phone-number-free outline of one EUM Social event, for logging: the AWS
    messageId, each change's field and value keys, and each status's id, status and
    errors."""

    event = json.loads(sns_message)
    entry = event.get("whatsAppWebhookEntry")
    if isinstance(entry, str):
        entry = json.loads(entry)
    changes = []
    for change in (entry or {}).get("changes", []):
        value = change.get("value", {})
        changes.append({
            "field": change.get("field"),
            "value_keys": sorted(value),
            "statuses": [{"id": s.get("id"), "status": s.get("status"), "errors": s.get("errors", [])}
                         for s in value.get("statuses", [])],
        })
    return {"aws_message_id": event.get("messageId"), "changes": changes}


def parse_whatsapp_statuses(sns_message: str) -> list[dict]:
    """Delivery receipts (sent / delivered / read / failed) in one EUM Social event,
    from changes[].value.statuses[]."""

    event = json.loads(sns_message)
    entry = event.get("whatsAppWebhookEntry")
    if isinstance(entry, str):
        entry = json.loads(entry)
    if not entry:
        return []

    statuses = []
    for change in entry.get("changes", []):
        for status in change.get("value", {}).get("statuses", []):
            if not status.get("id") or not status.get("recipient_id"):
                continue
            statuses.append({
                "conversation_id": normalize_phone(status["recipient_id"]),
                "provider_message_id": status["id"],
                "status": status.get("status", "unknown"),
                "errors": status.get("errors", []),
            })
    return statuses


def parse_ses_event(sns_message: str) -> list[InboundMessage]:
    """An email received by Amazon SES through a receipt rule's SNS action
    ("Contents of notifications for Amazon SES email receiving"). Mail that SES
    scored as spam or virus is dropped. `conversation_id` is left empty: the sender
    is identified by email address, which the service maps to a conversation."""

    notification = json.loads(sns_message)
    if notification.get("notificationType") != "Received":
        return []
    receipt = notification.get("receipt", {})
    for verdict in ("spamVerdict", "virusVerdict"):
        if receipt.get(verdict, {}).get("status") == "FAIL":
            return []

    mail = notification.get("mail", {})
    sender = _sender_address(mail)
    body = _reply_text(notification.get("content", ""))
    if not sender or not body:
        return []
    return [
        InboundMessage(
            conversation_id="",
            channel=Channel.EMAIL,
            from_address=sender,
            body=body,
            provider_message_id=mail.get("messageId"),
        )
    ]


def _sender_address(mail: dict) -> str | None:
    headers_from = mail.get("commonHeaders", {}).get("from") or []
    candidate = headers_from[0] if headers_from else mail.get("source", "")
    _, address = parseaddr(candidate)
    return address.lower() or None


_QUOTE_START = re.compile(r"^On .+wrote:\s*$|^-{2,}\s*Original Message\s*-{2,}", re.IGNORECASE)


def _reply_text(raw_mime: str) -> str:
    """The new text of a reply: the first text/plain part, cut at the quoted
    original ("On ... wrote:", "-----Original Message-----", or '>' lines)."""

    if not raw_mime:
        return ""
    message = message_from_string(raw_mime, policy=email_policy.default)
    part = message.get_body(preferencelist=("plain",))
    text = part.get_content() if part is not None else ""
    lines = []
    for line in text.splitlines():
        if _QUOTE_START.match(line.strip()) or line.lstrip().startswith(">"):
            break
        lines.append(line)
    return "\n".join(lines).strip()


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
