"""Amazon SNS message signature verification.

Both inbound channels reach Omnidamus through SNS: AWS End User Messaging Social
publishes WhatsApp events to an SNS topic, and AWS End User Messaging SMS publishes
two-way SMS to another. Neither offers a provider-signed HTTP webhook of its own, so
the signature that proves an inbound message is genuine is the SNS one.

Implemented per the SNS developer guide ("Verifying the signatures of Amazon SNS
messages"):

- Reject any message whose TopicArn is not one of ours.
- Fetch the signing certificate only over HTTPS, only from an
  sns.<region>.amazonaws.com host.
- Rebuild the string to sign from the documented fields, in byte-sorted order, each
  as "Name\\nValue\\n" (Subject only when present).
- SignatureVersion "1" is SHA1withRSA, "2" is SHA256withRSA.

Works on both shapes SNS produces: the HTTP POST body (``SigningCertURL``) and the
record Lambda receives from an SNS subscription (``SigningCertUrl``).
"""

from __future__ import annotations

import base64
import re
import urllib.request
from collections.abc import Callable, Iterable
from urllib.parse import urlparse

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

_SNS_CERT_HOST = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$")
_HASHES = {"1": hashes.SHA1, "2": hashes.SHA256}
_NOTIFICATION_FIELDS = ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type")
_SUBSCRIPTION_FIELDS = (
    "Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type",
)

_cert_cache: dict[str, x509.Certificate] = {}


class SnsVerificationError(Exception):
    pass


def _field(message: dict, name: str):
    if name == "SigningCertURL":
        return message.get("SigningCertURL") or message.get("SigningCertUrl")
    if name == "SubscribeURL":
        return message.get("SubscribeURL") or message.get("SubscribeUrl")
    return message.get(name)


def string_to_sign(message: dict) -> bytes:
    msg_type = message.get("Type")
    if msg_type == "Notification":
        fields = _NOTIFICATION_FIELDS
    elif msg_type in ("SubscriptionConfirmation", "UnsubscribeConfirmation"):
        fields = _SUBSCRIPTION_FIELDS
    else:
        raise SnsVerificationError(f"Unsupported SNS message type: {msg_type!r}")

    parts = []
    for name in fields:
        value = _field(message, name)
        if value is None:
            if name == "Subject":
                continue
            raise SnsVerificationError(f"SNS message is missing {name}")
        parts.append(f"{name}\n{value}\n")
    return "".join(parts).encode("utf-8")


def check_cert_url(url: str | None) -> str:
    if not url:
        raise SnsVerificationError("SNS message has no SigningCertURL")
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise SnsVerificationError("SigningCertURL is not HTTPS")
    if not _SNS_CERT_HOST.match(parsed.hostname or ""):
        raise SnsVerificationError(f"SigningCertURL host is not SNS: {parsed.hostname!r}")
    if not parsed.path.endswith(".pem"):
        raise SnsVerificationError("SigningCertURL does not point at a .pem certificate")
    return url


def _fetch_cert(url: str) -> x509.Certificate:
    if url not in _cert_cache:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 -- URL checked above
            _cert_cache[url] = x509.load_pem_x509_certificate(response.read())
    return _cert_cache[url]


def verify(
    message: dict,
    allowed_topic_arns: Iterable[str],
    fetch_cert: Callable[[str], x509.Certificate] = _fetch_cert,
) -> None:
    """Raise SnsVerificationError unless `message` is a genuine SNS message from one
    of `allowed_topic_arns`."""

    allowed = {arn for arn in allowed_topic_arns if arn}
    if message.get("TopicArn") not in allowed:
        raise SnsVerificationError(f"Unexpected TopicArn: {message.get('TopicArn')!r}")

    hash_cls = _HASHES.get(str(message.get("SignatureVersion")))
    if hash_cls is None:
        raise SnsVerificationError(
            f"Unsupported SignatureVersion: {message.get('SignatureVersion')!r}"
        )

    signature_b64 = message.get("Signature")
    if not signature_b64:
        raise SnsVerificationError("SNS message has no Signature")

    cert = fetch_cert(check_cert_url(_field(message, "SigningCertURL")))
    try:
        cert.public_key().verify(
            base64.b64decode(signature_b64),
            string_to_sign(message),
            padding.PKCS1v15(),
            hash_cls(),
        )
    except InvalidSignature as exc:
        raise SnsVerificationError("SNS signature does not match") from exc
