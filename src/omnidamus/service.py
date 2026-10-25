"""The conversation loop: one inbound message in, replies out, state saved.

The agent decides (agent.py); this module applies the channel rules the agent
cannot enforce on its own and does the sending:

- Each channel has its own address: phone for SMS/RCS and WhatsApp, email for SES.
- WhatsApp free-text replies are only allowed within 24 hours of the customer's own
  last WhatsApp message. Outside that window, a reply or handoff to WhatsApp goes
  out on the current channel instead, with a click-to-chat link so the customer
  can open the WhatsApp conversation themselves.
- On a handoff, the reply is sent on the channel the conversation is moving to (one
  message, not the same text twice).
- When the agent marks the conversation complete, the closing receipt goes out by
  Amazon SES exactly once per conversation (tracked in state).
- SNS redeliveries are dropped by the channel's own message ID.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from . import router
from .channels import ses_email
from .schema import Channel, InboundMessage, RoutingDecision
from .state import ConversationState, ConversationStore

log = logging.getLogger(__name__)

WHATSAPP_WINDOW = timedelta(hours=24)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

RouteFn = Callable[[InboundMessage, ConversationState, bool], RoutingDecision]
SendFn = Callable[[str, RoutingDecision], str]
EmailFn = Callable[..., str]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def whatsapp_window_open(state: ConversationState, now: datetime) -> bool:
    last = state.last_whatsapp_inbound_at
    return last is not None and now - last < WHATSAPP_WINDOW


def whatsapp_link() -> str | None:
    number = os.environ.get("OMNIDAMUS_WHATSAPP_DISPLAY_NUMBER", "")
    digits = "".join(ch for ch in number if ch.isdigit())
    return f"https://wa.me/{digits}" if digits else None


def address_for(channel: Channel, state: ConversationState) -> str | None:
    if channel is Channel.EMAIL:
        return state.customer_email
    return state.customer_phone


def receipt_text(state: ConversationState, decision: RoutingDecision) -> tuple[str, str]:
    subject = f"Confirmation: {decision.summary}"[:120]
    lines = [decision.summary, ""]
    if state.key_facts:
        lines.append("Details:")
        lines.extend(f"- {k}: {v}" for k, v in state.key_facts.items() if k != "email")
        lines.append("")
    lines.append(decision.reply_text)
    return subject, "\n".join(lines)


def _default_route(message, state, window_open):
    from .agent import route_message  # imported lazily: pulls in Strands/Bedrock

    return route_message(message, state, window_open)


def handle_inbound(
    message: InboundMessage,
    store: ConversationStore,
    route: RouteFn = _default_route,
    send: SendFn = router.dispatch_reply,
    send_email: EmailFn = ses_email.send_email,
    now: datetime | None = None,
) -> dict:
    now = now or _now()
    state = store.get(message.conversation_id) or ConversationState(
        conversation_id=message.conversation_id,
        customer_phone=message.from_address,
        current_channel=message.channel,
    )

    if message.provider_message_id and state.already_processed(message.provider_message_id):
        log.info("Dropping duplicate %s", message.provider_message_id)
        return {"conversation_id": state.conversation_id, "skipped": "duplicate"}

    if state.status == "complete":
        # The customer wrote again after the receipt: treat it as a new exchange.
        state.status = "open"
        state.receipt_message_id = None

    if message.channel is Channel.WHATSAPP:
        state.last_whatsapp_inbound_at = message.received_at
    state.current_channel = message.channel
    state.add_turn("inbound", message.channel, message.body)

    window_open = whatsapp_window_open(state, now)
    decision = route(message, state, window_open)

    email = decision.key_facts.get("email", "").strip()
    if email and _EMAIL.match(email):
        state.customer_email = email
    state.key_facts.update(decision.key_facts)

    sent: list[dict] = []

    def _send(channel: Channel, text: str) -> None:
        address = address_for(channel, state)
        out = RoutingDecision(summary=decision.summary, reply_channel=channel, reply_text=text)
        message_id = send(address, out)
        state.add_turn("outbound", channel, text)
        sent.append({"channel": channel.value, "message_id": message_id})

    target = decision.handoff_to or decision.reply_channel
    text = decision.reply_text
    if target is Channel.WHATSAPP and not window_open:
        link = whatsapp_link()
        target = message.channel if message.channel is not Channel.WHATSAPP else Channel.SMS_RCS
        if link:
            text = f"{text}\n\nContinue on WhatsApp: {link}"
    elif target is Channel.EMAIL and not state.customer_email:
        target = message.channel

    if not (decision.conversation_complete and target is Channel.EMAIL):
        # A completed conversation heading to email gets the receipt below instead.
        _send(target, text)
        state.current_channel = target

    if decision.conversation_complete:
        state.status = "complete"
        if state.receipt_message_id:
            log.info("Receipt already sent for %s", state.conversation_id)
        elif not state.customer_email:
            log.warning("Conversation %s complete but no email on file", state.conversation_id)
        else:
            subject, body = receipt_text(state, decision)
            receipt_id = send_email(state.customer_email, subject=subject, body_text=body)
            state.receipt_message_id = receipt_id
            state.add_turn("outbound", Channel.EMAIL, subject)
            sent.append({"channel": Channel.EMAIL.value, "message_id": receipt_id})

    if message.provider_message_id:
        state.mark_processed(message.provider_message_id)
    store.save(state)

    return {
        "conversation_id": state.conversation_id,
        "decision": decision.model_dump(mode="json"),
        "sent": sent,
        "status": state.status,
    }


def start_conversation(
    phone: str,
    notification: str,
    store: ConversationStore,
    email: str | None = None,
    context: str = "",
    send: SendFn = router.dispatch_reply,
) -> dict:
    """Open a conversation with an outbound SMS/RCS notification (step 1 of the
    reference flow). Replaces any earlier conversation with the same number."""

    existing = store.get(phone)
    state = ConversationState(
        conversation_id=phone,
        customer_phone=phone,
        customer_email=email if email and _EMAIL.match(email) else None,
        context=context,
        current_channel=Channel.SMS_RCS,
        version=existing.version if existing else 0,
    )
    decision = RoutingDecision(
        summary=context or "notification", reply_channel=Channel.SMS_RCS, reply_text=notification
    )
    message_id = send(phone, decision)
    state.add_turn("outbound", Channel.SMS_RCS, notification)
    store.save(state)
    return {"conversation_id": phone, "message_id": message_id}
