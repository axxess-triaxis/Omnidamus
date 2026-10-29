"""The conversation loop: one inbound message in, replies out, state saved.

The agent decides (agent.py); this module applies the channel rules the agent
cannot enforce on its own and does the sending:

- Each channel has its own address: phone for SMS/RCS and WhatsApp, email for SES.
  Inbound email is matched to its conversation by the sender's address.
- WhatsApp free-text replies are only allowed within 24 hours of the customer's own
  last WhatsApp message. Outside that window, a reply or handoff to WhatsApp goes
  out as an approved template (when OMNIDAMUS_WHATSAPP_HANDOFF_TEMPLATE is set) or
  on the current channel with a click-to-chat link.
- On a handoff, the reply is sent on the channel the conversation is moving to (one
  message, not the same text twice).
- STOP (or UNSUBSCRIBE, ...) on any channel ends all contact; START opts back in.
- When the agent marks the conversation complete, the closing receipt goes out by
  Amazon SES exactly once per conversation (tracked in state).
- SNS redeliveries are dropped by the channel's own message ID.
- WhatsApp delivery receipts update the matching outbound turn.
- A completed conversation is published to STILL, the Triaxis cross-product event
  bus, as triaxis.omnidamus conversation.completed.v1 (a no-op unless
  STILL_EVENT_BUS_ARN is set; never blocks the reply or the save).
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from . import router, still_emit
from .channels import ses_email, whatsapp_social
from .schema import Channel, InboundMessage, RoutingDecision
from .state import ConversationState, ConversationStore

log = logging.getLogger(__name__)

WHATSAPP_WINDOW = timedelta(hours=24)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
OPT_OUT_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "opt out", "optout"}
OPT_IN_WORDS = {"start", "unstop", "subscribe"}
OPT_OUT_CONFIRMATION = "You won't hear from us again. Reply START if you change your mind."
OPT_IN_CONFIRMATION = "You're subscribed again. How can we help?"

RouteFn = Callable[[InboundMessage, ConversationState, bool], RoutingDecision]
SendFn = Callable[[str, RoutingDecision], str]
EmailFn = Callable[..., str]
TemplateFn = Callable[..., str]
PublishFn = Callable[..., str | None]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _keyword(body: str) -> str:
    return re.sub(r"[^a-z ]", "", body.strip().lower()).strip()


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


class _Outbox:
    """Sends on behalf of one conversation and records each send as a turn."""

    def __init__(self, state, summary, send, send_template):
        self.state, self.summary = state, summary
        self.send, self.send_template = send, send_template
        self.sent: list[dict] = []

    def text(self, channel: Channel, text: str) -> None:
        out = RoutingDecision(summary=self.summary, reply_channel=channel, reply_text=text)
        message_id = self.send(address_for(channel, self.state), out)
        self._record(channel, text, message_id)

    def template(self, name: str, params: list[str] | None = None) -> None:
        message_id = self.send_template(
            self.state.customer_phone,
            name,
            language_code=os.environ.get("OMNIDAMUS_WHATSAPP_TEMPLATE_LANGUAGE", "en"),
            body_parameters=params or None,
        )
        self._record(Channel.WHATSAPP, f"[template {name}] {' | '.join(params or [])}".strip(), message_id)

    def _record(self, channel: Channel, text: str, message_id: str) -> None:
        self.state.add_turn("outbound", channel, text, provider_message_id=message_id)
        self.sent.append({"channel": channel.value, "message_id": message_id})


def completion_event_data(state: ConversationState, decision: RoutingDecision,
                          channel: Channel, now: datetime) -> dict:
    """The data of a STILL triaxis.omnidamus conversation.completed.v1 event."""
    return {
        "conversation_id": state.conversation_id,
        "completed_at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "channel": channel.value,
        "context": state.context,
        "summary": decision.summary,
        "key_facts": {k: str(v) for k, v in state.key_facts.items() if k != "email"},
        "customer_phone": state.customer_phone,
        "customer_email": state.customer_email,
        "receipt_sent": bool(state.receipt_message_id),
    }


def _publish_completion(publish: PublishFn, state: ConversationState, decision: RoutingDecision,
                        message: InboundMessage, now: datetime) -> None:
    # Keyed on the inbound message that closed the exchange, so an SNS redelivery or
    # Lambda retry publishes the same event id and STILL drops the duplicate.
    key = f"{state.conversation_id}:{message.provider_message_id or now.isoformat()}"
    try:
        publish("triaxis.omnidamus", "conversation.completed.v1",
                completion_event_data(state, decision, message.channel, now), key=key)
    except Exception as error:  # noqa: BLE001 -- STILL is a side channel, never a blocker
        log.warning("STILL publish failed: %s", type(error).__name__)


def _resolve_conversation(message: InboundMessage, store: ConversationStore) -> str | None:
    if message.channel is Channel.EMAIL:
        return store.find_by_email(message.from_address)
    return message.conversation_id


def handle_inbound(
    message: InboundMessage,
    store: ConversationStore,
    route: RouteFn = _default_route,
    send: SendFn = router.dispatch_reply,
    send_email: EmailFn = ses_email.send_email,
    send_template: TemplateFn = whatsapp_social.send_template_message,
    now: datetime | None = None,
    publish: PublishFn = still_emit.emit,
) -> dict:
    now = now or _now()
    conversation_id = _resolve_conversation(message, store)
    if not conversation_id:
        # Email from an address no conversation knows: never start one from it.
        log.info("No conversation for %s on %s; dropped", message.from_address, message.channel.value)
        return {"skipped": "unknown_sender"}

    state = store.get(conversation_id) or ConversationState(
        conversation_id=conversation_id,
        customer_phone=message.from_address,
        current_channel=message.channel,
    )

    if message.provider_message_id and state.already_processed(message.provider_message_id):
        log.info("Dropping duplicate %s", message.provider_message_id)
        return {"conversation_id": state.conversation_id, "skipped": "duplicate"}

    if message.channel is Channel.WHATSAPP:
        state.last_whatsapp_inbound_at = message.received_at
    state.add_turn("inbound", message.channel, message.body)
    if message.provider_message_id:
        state.mark_processed(message.provider_message_id)

    outbox = _Outbox(state, "", send, send_template)
    keyword = _keyword(message.body)

    # Opt-out and opt-in are handled here, never by the model.
    if keyword in OPT_OUT_WORDS:
        state.opted_out = True
        if message.channel is not Channel.SMS_RCS:
            # EUM SMS answers STOP itself through the number's opt-out list.
            outbox.text(message.channel, OPT_OUT_CONFIRMATION)
        store.save(state)
        return {"conversation_id": state.conversation_id, "status": "opted_out", "sent": outbox.sent}
    if state.opted_out:
        if keyword in OPT_IN_WORDS:
            state.opted_out = False
            outbox.text(message.channel, OPT_IN_CONFIRMATION)
            store.save(state)
            return {"conversation_id": state.conversation_id, "status": "opted_in", "sent": outbox.sent}
        store.save(state)
        return {"conversation_id": state.conversation_id, "skipped": "opted_out"}

    if state.status == "complete":
        # The customer wrote again after the receipt: treat it as a new exchange.
        state.status = "open"
        state.receipt_message_id = None
    state.current_channel = message.channel

    window_open = whatsapp_window_open(state, now)
    decision = route(message, state, window_open)
    outbox.summary = decision.summary

    email = decision.key_facts.get("email", "").strip()
    if email and _EMAIL.match(email):
        state.customer_email = email
    state.key_facts.update(decision.key_facts)

    target = decision.handoff_to or decision.reply_channel
    text = decision.reply_text
    handoff_template = os.environ.get("OMNIDAMUS_WHATSAPP_HANDOFF_TEMPLATE")
    if target is Channel.WHATSAPP and not window_open and handoff_template:
        target = None  # handled by the template below
    elif target is Channel.WHATSAPP and not window_open:
        link = whatsapp_link()
        target = message.channel if message.channel is not Channel.WHATSAPP else Channel.SMS_RCS
        if link:
            text = f"{text}\n\nContinue on WhatsApp: {link}"
    elif target is Channel.EMAIL and not state.customer_email:
        target = message.channel

    if target is None:
        outbox.template(handoff_template)
        state.current_channel = Channel.WHATSAPP
    elif not (decision.conversation_complete and target is Channel.EMAIL):
        # A completed conversation heading to email gets the receipt below instead.
        outbox.text(target, text)
        state.current_channel = target

    if decision.conversation_complete:
        state.status = "complete"
        if state.receipt_message_id:
            log.info("Receipt already sent for %s", state.conversation_id)
        elif not state.customer_email:
            log.warning("Conversation %s complete but no email on file", state.conversation_id)
        else:
            subject, body = receipt_text(state, decision)
            try:
                receipt_id = send_email(state.customer_email, subject=subject, body_text=body)
            except ses_email.SesSendError:
                # The reply has already gone out: save the turn anyway, so a Lambda
                # retry neither loses it nor sends the reply again. receipt_message_id
                # stays empty, so the next completion tries the receipt again.
                log.exception("Receipt for %s not sent", state.conversation_id)
            else:
                state.receipt_message_id = receipt_id
                state.add_turn("outbound", Channel.EMAIL, subject, provider_message_id=receipt_id)
                outbox.sent.append({"channel": Channel.EMAIL.value, "message_id": receipt_id})

    store.save(state)
    if state.customer_email:
        store.link_email(state.customer_email, state.conversation_id)
    if decision.conversation_complete:
        # After the save, so STILL never hears of a completion that was not stored.
        _publish_completion(publish, state, decision, message, now)

    return {
        "conversation_id": state.conversation_id,
        "decision": decision.model_dump(mode="json"),
        "sent": outbox.sent,
        "status": state.status,
    }


def apply_whatsapp_status(status: dict, store: ConversationStore) -> bool:
    """Record a WhatsApp delivery receipt on the outbound turn it belongs to.
    Returns False when the message isn't in the conversation's recent turns."""

    if status["status"] == "failed":
        log.warning("WhatsApp delivery failed for %s: %s",
                    status["provider_message_id"], status.get("errors"))
    state = store.get(status["conversation_id"])
    matched = state is not None and state.set_status(status["provider_message_id"], status["status"])
    # No phone number here: the message ID and outcome are enough to trace a delivery.
    log.info(json.dumps({"whatsapp_status": status["status"],
                         "meta_message_id": status["provider_message_id"],
                         "errors": status.get("errors", []), "matched_turn": matched}))
    if not matched:
        return False
    store.save(state)
    return True


def start_conversation(
    phone: str,
    store: ConversationStore,
    notification: str | None = None,
    email: str | None = None,
    context: str = "",
    channel: Channel = Channel.SMS_RCS,
    template: str | None = None,
    template_params: list[str] | None = None,
    send: SendFn = router.dispatch_reply,
    send_template: TemplateFn = whatsapp_social.send_template_message,
) -> dict:
    """Open a conversation (step 1 of the reference flow): an SMS/RCS notification,
    or -- since a business can only open a WhatsApp chat with an approved
    template -- a WhatsApp template. Replaces any earlier conversation with the
    same number, unless the customer has opted out."""

    existing = store.get(phone)
    if existing and existing.opted_out:
        return {"conversation_id": phone, "skipped": "opted_out"}
    if channel is Channel.WHATSAPP and not template:
        raise ValueError("A WhatsApp conversation must open with an approved template")
    if channel is not Channel.WHATSAPP and not notification:
        raise ValueError("An SMS/RCS conversation needs notification text")

    state = ConversationState(
        conversation_id=phone,
        customer_phone=phone,
        customer_email=email if email and _EMAIL.match(email) else None,
        context=context,
        current_channel=channel,
        version=existing.version if existing else 0,
    )
    outbox = _Outbox(state, context or "notification", send, send_template)
    if channel is Channel.WHATSAPP:
        outbox.template(template, template_params)
    else:
        outbox.text(Channel.SMS_RCS, notification)
    store.save(state)
    if state.customer_email:
        store.link_email(state.customer_email, phone)
    return {"conversation_id": phone, "message_id": outbox.sent[0]["message_id"]}
