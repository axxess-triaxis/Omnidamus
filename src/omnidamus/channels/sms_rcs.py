"""AWS End User Messaging (SMS/RCS) channel.

API shape verified against AWS's own pinpoint-sms-voice-v2 documentation.
Two operations matter here:

- `send_text_message` -- used for both SMS and plain-text RCS. Which one actually
  gets delivered depends on `OriginationIdentity`: a phone pool ID sends RCS with
  automatic SMS fallback, an AWS RCS Agent ARN sends RCS-only with no fallback.
  Neither a pool nor an RCS agent has been provisioned yet for this project (see
  README Status) -- OMNIDAMUS_SMS_RCS_ORIGINATION_IDENTITY is unset until then.
- `send_rcs_message` -- used only for rich RCS content (cards, carousels, media).
  Its rich-content payload schema is not yet verified against docs in this
  codebase, so `send_rich_rcs_message` below is intentionally left unimplemented
  rather than guessing that shape.
"""

from __future__ import annotations

import os

import boto3
from botocore.exceptions import ClientError


class SmsRcsSendError(RuntimeError):
    pass


def _origination_identity(override: str | None) -> str:
    identity = override or os.environ.get("OMNIDAMUS_SMS_RCS_ORIGINATION_IDENTITY")
    if not identity:
        raise SmsRcsSendError(
            "No origination_identity given and "
            "OMNIDAMUS_SMS_RCS_ORIGINATION_IDENTITY is unset"
        )
    return identity


def send_text_message(
    to_number: str,
    body: str,
    origination_identity: str | None = None,
    configuration_set: str | None = None,
    region_name: str | None = None,
) -> str:
    """Send an SMS or plain-text RCS message. Returns the MessageId."""

    kwargs: dict = {
        "DestinationPhoneNumber": to_number,
        "OriginationIdentity": _origination_identity(origination_identity),
        "MessageBody": body,
    }
    client = boto3.client(
        "pinpoint-sms-voice-v2", region_name=region_name or os.environ.get("AWS_REGION")
    )
    config_set = configuration_set or os.environ.get("OMNIDAMUS_SMS_RCS_CONFIGURATION_SET")
    if config_set:
        kwargs["ConfigurationSetName"] = config_set

    try:
        response = client.send_text_message(**kwargs)
    except ClientError as exc:
        raise SmsRcsSendError(f"send_text_message failed: {exc}") from exc

    return response["MessageId"]


def send_rich_rcs_message(*_args, **_kwargs) -> str:
    """Not implemented yet.

    `send_rcs_message`'s rich-content request shape (cards/carousels/media) has
    not been verified against AWS documentation in this codebase -- implement
    this only after confirming the exact payload schema, rather than guessing it.
    """

    raise NotImplementedError(
        "send_rich_rcs_message: rich RCS payload schema not yet verified; "
        "use send_text_message for plain-text RCS in the meantime"
    )
