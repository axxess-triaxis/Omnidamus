"""Conversation memory, persisted in Amazon DynamoDB.

One item per conversation, keyed by `conversation_id` (the customer's phone number
in E.164 form, so SMS and WhatsApp messages from the same person land in the same
conversation). The conversation itself is stored as one JSON attribute rather than
mapped field-by-field -- it is always read and written whole, and this keeps the
pydantic model the single source of truth for its shape.

Writes are optimistic: every item carries a `version`, and `save` only succeeds if
the stored version is still the one that was read. SNS can deliver the same
message more than once, so two Lambda invocations may race on one conversation;
the loser gets `ConversationConflictError` and Lambda's async retry re-reads.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Literal

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field

from .schema import Channel

MAX_TURNS = 20
MAX_PROCESSED_IDS = 50
TTL_SECONDS = 30 * 24 * 3600
DELIVERY_RANK = {"accepted": 0, "sent": 1, "delivered": 2, "read": 3, "failed": 4}


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Turn(BaseModel):
    direction: Literal["inbound", "outbound"]
    channel: Channel
    text: str
    at: datetime = Field(default_factory=_now)
    provider_message_id: str | None = None
    status: str | None = None  # WhatsApp delivery status (see DELIVERY_RANK)


class ConversationState(BaseModel):
    conversation_id: str
    customer_phone: str
    customer_email: str | None = None
    context: str = ""
    current_channel: Channel = Channel.SMS_RCS
    status: Literal["open", "complete"] = "open"
    turns: list[Turn] = Field(default_factory=list)
    key_facts: dict[str, str] = Field(default_factory=dict)
    processed_message_ids: list[str] = Field(default_factory=list)
    last_whatsapp_inbound_at: datetime | None = None
    receipt_message_id: str | None = None
    opted_out: bool = False
    # Set when AXXESS started this conversation for an organization (2026-11-20). None for
    # Triaxis's own (Tenant 0) conversations, which keep the phone-number key.
    organization_id: str | None = None
    job_id: str | None = None
    line: str | None = None  # the organization's own business number, E.164
    whatsapp_origination_id: str | None = None
    sms_origination_identity: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    version: int = 0

    def add_turn(
        self,
        direction: str,
        channel: Channel,
        text: str,
        provider_message_id: str | None = None,
    ) -> None:
        self.turns.append(
            Turn(direction=direction, channel=channel, text=text,
                 provider_message_id=provider_message_id)
        )
        del self.turns[:-MAX_TURNS]

    def mark_processed(self, provider_message_id: str) -> None:
        self.processed_message_ids.append(provider_message_id)
        del self.processed_message_ids[:-MAX_PROCESSED_IDS]

    def already_processed(self, provider_message_id: str) -> bool:
        return provider_message_id in self.processed_message_ids


class ConversationConflictError(RuntimeError):
    """The conversation changed between read and write; re-read and retry. Carries no
    conversation ID: that is the customer's phone number, and this message is logged."""


class ConversationStore:
    def __init__(self, table_name: str | None = None, region_name: str | None = None):
        name = table_name or os.environ.get("OMNIDAMUS_CONVERSATIONS_TABLE")
        if not name:
            raise RuntimeError("OMNIDAMUS_CONVERSATIONS_TABLE is unset")
        resource = boto3.resource(
            "dynamodb", region_name=region_name or os.environ.get("AWS_REGION")
        )
        self._table = resource.Table(name)

    def get(self, conversation_id: str) -> ConversationState | None:
        item = self._table.get_item(
            Key={"conversation_id": conversation_id}, ConsistentRead=True
        ).get("Item")
        if item is None:
            return None
        state = ConversationState.model_validate_json(item["state"])
        state.version = int(item["version"])
        return state

    def save(self, state: ConversationState) -> None:
        """Write `state` if nobody else has written since it was read."""

        expected = state.version
        state.version = expected + 1
        state.updated_at = _now()
        condition = (
            "attribute_not_exists(conversation_id)" if expected == 0 else "version = :expected"
        )
        kwargs: dict = {
            "Item": {
                "conversation_id": state.conversation_id,
                "version": state.version,
                "state": state.model_dump_json(exclude={"version"}),
                "expires_at": int(time.time()) + TTL_SECONDS,
            },
            "ConditionExpression": condition,
        }
        if expected:
            kwargs["ExpressionAttributeValues"] = {":expected": expected}
        try:
            self._table.put_item(**kwargs)
        except ClientError as exc:
            state.version = expected
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise ConversationConflictError("The conversation changed since it was read") from exc
            raise

    # Inbound email carries an address, not a phone number. A small pointer item
    # ("email#<address>" -> conversation_id) in the same table finds the conversation.

    def link_email(self, email: str, conversation_id: str) -> None:
        self._table.put_item(
            Item={
                "conversation_id": _email_key(email),
                "points_to": conversation_id,
                "expires_at": int(time.time()) + TTL_SECONDS,
            }
        )

    def find_by_email(self, email: str) -> str | None:
        item = self._table.get_item(Key={"conversation_id": _email_key(email)}).get("Item")
        return item["points_to"] if item else None


    # A delivery receipt is its own item ("delivery#<message id>"), not a write to the
    # conversation: receipts arrive while a reply is still being composed, and bumping the
    # conversation's version then would fail that reply's save after it was already sent.

    def record_delivery(self, message_id: str, status: str) -> bool:
        """Keep the furthest status seen: Meta can send "read" before "delivered".
        False when a later status is already stored or this one is unknown."""

        rank = DELIVERY_RANK.get(status)
        if rank is None:
            return False
        try:
            self._table.put_item(
                Item={
                    "conversation_id": _delivery_key(message_id),
                    "status": status,
                    "rank": rank,
                    "expires_at": int(time.time()) + TTL_SECONDS,
                },
                ConditionExpression=Attr("conversation_id").not_exists() | Attr("rank").lt(rank),
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def delivery_status(self, message_id: str) -> str | None:
        item = self._table.get_item(Key={"conversation_id": _delivery_key(message_id)}).get("Item")
        return item["status"] if item else None

    def refresh_delivery(self, state: ConversationState) -> None:
        """Copy stored receipts onto the conversation's outbound WhatsApp turns."""

        for turn in state.turns:
            if (turn.direction == "outbound" and turn.channel is Channel.WHATSAPP
                    and turn.provider_message_id and turn.status not in ("read", "failed")):
                turn.status = self.delivery_status(turn.provider_message_id) or turn.status


def _delivery_key(message_id: str) -> str:
    return f"delivery#{message_id}"


def _email_key(email: str) -> str:
    return f"email#{email.strip().lower()}"


# Organization-scoped keys (2026-11-20). Triaxis's own lines keep the bare phone-number key, so
# existing conversations are untouched. Any other business number gets "line:<number>#<phone>",
# so two organizations writing to the same person never share a thread or an opt-out.


def tenant0_lines() -> set[str]:
    """Triaxis's own business numbers, from the deployed configuration."""
    raw = [
        os.environ.get("OMNIDAMUS_WHATSAPP_DISPLAY_NUMBER", ""),
        os.environ.get("OMNIDAMUS_SMS_RCS_ORIGINATION_IDENTITY", ""),
        *os.environ.get("OMNIDAMUS_TENANT0_LINES", "").split(","),
    ]
    lines = set()
    for value in raw:
        digits = "".join(ch for ch in value if ch.isdigit())
        if value.strip().startswith("+") or (digits and digits == value.strip()):
            lines.add(f"+{digits}")
    return lines


def is_tenant0_line(line: str | None) -> bool:
    return line is None or line in tenant0_lines()


def conversation_key(line: str | None, phone: str) -> str:
    return phone if is_tenant0_line(line) else f"line:{line}#{phone}"
