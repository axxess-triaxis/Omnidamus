"""Data shapes shared across the router, agent, and channel wrappers."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class Channel(str, Enum):
    SMS_RCS = "sms_rcs"
    WHATSAPP = "whatsapp"
    EMAIL = "email"


class InboundMessage(BaseModel):
    """A message received on any channel, normalized to one shape."""

    conversation_id: str
    channel: Channel
    from_address: str
    body: str
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RoutingDecision(BaseModel):
    """The agent's judgment on what to do with one inbound message.

    Forced as Strands structured output, mirroring the pattern of keeping the
    rest of the pipeline deterministic once a decision has been made.
    """

    summary: str = Field(description="One or two sentence plain-language summary")
    reply_channel: Channel = Field(description="Which channel to send the reply on")
    reply_text: str = Field(description="The text to send on reply_channel")
    handoff_to: Channel | None = Field(
        default=None,
        description="Set only when the conversation should move to a different "
        "channel next (e.g. SMS notification -> WhatsApp back-and-forth)",
    )
    conversation_complete: bool = Field(
        default=False,
        description="True only when the conversation has reached a final outcome "
        "and a closing receipt should be sent",
    )
    key_facts: dict[str, str] = Field(default_factory=dict)
