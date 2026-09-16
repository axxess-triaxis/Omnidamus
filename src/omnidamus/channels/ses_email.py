"""Amazon SES (v2) email channel.

API shape verified against AWS's own SESv2 documentation: the `sesv2` client's
`send_email` operation takes `FromEmailAddress`, a `Destination.ToAddresses` list,
and `Content.Simple.{Subject,Body}`. `FromEmailAddress` must be a verified identity
in the sending AWS account/region -- that verification step has not been done yet
for this project (see README Status).
"""

from __future__ import annotations

import os

import boto3
from botocore.exceptions import ClientError


class SesSendError(RuntimeError):
    pass


def send_email(
    to_address: str,
    subject: str,
    body_text: str,
    from_address: str | None = None,
    region_name: str | None = None,
) -> str:
    """Send a plain-text email via Amazon SES. Returns the SES MessageId."""

    sender = from_address or os.environ.get("OMNIDAMUS_SES_FROM_ADDRESS")
    if not sender:
        raise SesSendError(
            "No from_address given and OMNIDAMUS_SES_FROM_ADDRESS is unset"
        )

    client = boto3.client("sesv2", region_name=region_name or os.environ.get("AWS_REGION"))
    try:
        response = client.send_email(
            FromEmailAddress=sender,
            Destination={"ToAddresses": [to_address]},
            Content={
                "Simple": {
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": body_text, "Charset": "UTF-8"}},
                }
            },
        )
    except ClientError as exc:
        raise SesSendError(f"SES send_email failed: {exc}") from exc

    return response["MessageId"]
