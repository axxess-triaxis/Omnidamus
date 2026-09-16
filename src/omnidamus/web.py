"""FastAPI app wiring inbound-channel intake to the agent and router.

This is intake plumbing only: it normalizes each channel's webhook payload into
an InboundMessage, hands it to the agent, and dispatches the resulting
RoutingDecision. Signature verification for each provider's webhook (WhatsApp's
HMAC-SHA256 header, SNS message signing for EUM inbound, etc.) is not yet
implemented -- do not point a real webhook at this without adding it first.
"""

from __future__ import annotations

from fastapi import FastAPI

from .agent import build_agent, route_message
from .router import dispatch_handoff, dispatch_reply
from .schema import Channel, InboundMessage

app = FastAPI(title="Omnidamus")
_agent = None


def _get_agent():
    global _agent
    if _agent is None:
        _agent = build_agent()
    return _agent


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/inbound/{channel}")
def inbound(channel: Channel, conversation_id: str, from_address: str, body: str) -> dict:
    """Minimal, unauthenticated intake endpoint for local testing.

    Real provider webhooks (WhatsApp, EUM inbound SNS) each have their own
    payload shape and signature scheme -- adapt those into this same
    InboundMessage call once each channel's AWS resource is provisioned.
    """

    message = InboundMessage(
        conversation_id=conversation_id, channel=channel, from_address=from_address, body=body
    )
    decision = route_message(_get_agent(), message)

    reply_id = dispatch_reply(from_address, decision)
    handoff_id = dispatch_handoff(from_address, decision)

    return {
        "decision": decision.model_dump(),
        "reply_message_id": reply_id,
        "handoff_message_id": handoff_id,
    }
