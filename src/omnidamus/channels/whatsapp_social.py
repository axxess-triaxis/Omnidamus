"""AWS End User Messaging Social (WhatsApp) channel.

API shape verified against AWS's own socialmessaging documentation:
`send_whatsapp_message` requires `originationPhoneNumberId` (the linked WhatsApp
Business Account phone number ID) and `message` -- a base64-encoded WhatsApp
Message object in the same JSON shape as Meta's WhatsApp Business Platform Cloud
API `messages` request body. boto3 base64-encodes blob-type parameters
automatically, so this passes the JSON body's raw UTF-8 bytes directly.

No WhatsApp Business Account has been linked to AWS yet for this project (see
README Status) -- `associate_whatsapp_business_account` is a one-time AWS Console
or CLI setup step, not something this module does at send time.
"""

from __future__ import annotations

import json
import os

import boto3
from botocore.exceptions import ClientError


class WhatsAppSendError(RuntimeError):
    pass


def send_text_message(
    to_number: str,
    body: str,
    origination_phone_number_id: str | None = None,
    meta_api_version: str | None = None,
    region_name: str | None = None,
) -> str:
    """Send a plain-text WhatsApp message. Returns the AWS message ID.

    Only allowed within 24 hours of the customer's own last WhatsApp message;
    outside that window use send_template_message.
    """

    meta_message = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "text",
        "text": {"body": body},
    }
    return _send(meta_message, origination_phone_number_id, meta_api_version, region_name)


def send_template_message(
    to_number: str,
    template_name: str,
    language_code: str = "en",
    body_parameters: list[str] | None = None,
    origination_phone_number_id: str | None = None,
    meta_api_version: str | None = None,
    region_name: str | None = None,
) -> str:
    """Send a Meta-approved template -- the only kind of WhatsApp message a business
    may start a conversation with, or send outside the 24-hour window. Body
    parameters fill the template's {{1}}, {{2}}, ... in order. Returns the AWS
    message ID."""

    template: dict = {"name": template_name, "language": {"code": language_code}}
    if body_parameters:
        template["components"] = [
            {
                "type": "body",
                "parameters": [{"type": "text", "text": value} for value in body_parameters],
            }
        ]
    meta_message = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_number,
        "type": "template",
        "template": template,
    }
    return _send(meta_message, origination_phone_number_id, meta_api_version, region_name)


def _send(
    meta_message: dict,
    origination_phone_number_id: str | None,
    meta_api_version: str | None,
    region_name: str | None,
) -> str:
    phone_number_id = origination_phone_number_id or os.environ.get(
        "OMNIDAMUS_WHATSAPP_ORIGINATION_PHONE_NUMBER_ID"
    )
    if not phone_number_id:
        raise WhatsAppSendError(
            "No origination_phone_number_id given and "
            "OMNIDAMUS_WHATSAPP_ORIGINATION_PHONE_NUMBER_ID is unset"
        )

    client = boto3.client("socialmessaging", region_name=region_name or os.environ.get("AWS_REGION"))
    try:
        response = client.send_whatsapp_message(
            originationPhoneNumberId=phone_number_id,
            message=json.dumps(meta_message).encode("utf-8"),
            metaApiVersion=meta_api_version
            or os.environ.get("OMNIDAMUS_WHATSAPP_META_API_VERSION", "v20.0"),
        )
    except ClientError as exc:
        raise WhatsAppSendError(f"send_whatsapp_message failed: {exc}") from exc

    return response["messageId"]
