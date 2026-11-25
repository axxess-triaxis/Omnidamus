"""Deterministic dispatch of an agent's RoutingDecision to the right channel.

Kept separate from the agent's own reasoning (agent.py) on the same principle
Callismatic uses elsewhere in this org's codebase: the model decides, plain code
acts -- the rest of the pipeline never re-interprets prose, it dispatches on an
already-validated, typed decision.
"""

from __future__ import annotations

from .channels import ses_email, sms_rcs, whatsapp_social
from .schema import Channel, RoutingDecision


EMAIL_SUBJECT = "Our conversation"


class UnroutableChannelError(RuntimeError):
    pass


def dispatch_reply(to_address: str, decision: RoutingDecision, *,
                   whatsapp_origination_id: str | None = None,
                   sms_origination_identity: str | None = None) -> str:
    """Send `decision.reply_text` on `decision.reply_channel`. Returns the provider message ID.
    The origination arguments carry an organization's own sender; None uses the deployed default."""

    if decision.reply_channel is Channel.SMS_RCS:
        origin = {"origination_identity": sms_origination_identity} if sms_origination_identity else {}
        return sms_rcs.send_text_message(to_address, decision.reply_text, **origin)
    if decision.reply_channel is Channel.WHATSAPP:
        origin = {"origination_phone_number_id": whatsapp_origination_id} if whatsapp_origination_id else {}
        return whatsapp_social.send_text_message(to_address, decision.reply_text, **origin)
    if decision.reply_channel is Channel.EMAIL:
        return ses_email.send_email(
            to_address,
            subject=EMAIL_SUBJECT,  # the summary is the agent's note to itself
            body_text=decision.reply_text,
        )
    raise UnroutableChannelError(f"Unhandled channel: {decision.reply_channel!r}")


def dispatch_handoff(next_address: str, decision: RoutingDecision) -> str | None:
    """If the decision calls for a channel handoff, notify the user on the new
    channel that the conversation is continuing there. Returns the provider
    message ID, or None if no handoff was requested.
    """

    if decision.handoff_to is None:
        return None

    handoff_notice = RoutingDecision(
        summary=decision.summary,
        reply_channel=decision.handoff_to,
        reply_text=decision.reply_text,
    )
    return dispatch_reply(next_address, handoff_notice)
