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
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field

from .schema import Channel

MAX_TURNS = 20
MAX_PROCESSED_IDS = 50
TTL_SECONDS = 30 * 24 * 3600


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Turn(BaseModel):
    direction: Literal["inbound", "outbound"]
    channel: Channel
    text: str
    at: datetime = Field(default_factory=_now)


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
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    version: int = 0

    def add_turn(self, direction: str, channel: Channel, text: str) -> None:
        self.turns.append(Turn(direction=direction, channel=channel, text=text))
        del self.turns[:-MAX_TURNS]

    def mark_processed(self, provider_message_id: str) -> None:
        self.processed_message_ids.append(provider_message_id)
        del self.processed_message_ids[:-MAX_PROCESSED_IDS]

    def already_processed(self, provider_message_id: str) -> bool:
        return provider_message_id in self.processed_message_ids


class ConversationConflictError(RuntimeError):
    """The conversation changed between read and write; re-read and retry."""


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
                raise ConversationConflictError(state.conversation_id) from exc
            raise
