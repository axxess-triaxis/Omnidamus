"""SNS signature verification, against a key pair generated in the test."""

from __future__ import annotations

import base64
import datetime as dt

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from omnidamus import sns_verify

TOPIC = "arn:aws:sns:us-east-1:123456789012:omnidamus-whatsapp-inbound"
CERT_URL = "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-abc.pem"


@pytest.fixture(scope="module")
def key_and_cert():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.amazonaws.com")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _signed(key, version="2", lambda_shape=True, **overrides):
    msg = {
        "Type": "Notification",
        "MessageId": "11111111-2222-3333-4444-555555555555",
        "TopicArn": TOPIC,
        "Subject": None,
        "Message": '{"messageBody":"hi"}',
        "Timestamp": "2026-10-25T10:00:00.000Z",
        "SignatureVersion": version,
    }
    msg["SigningCertUrl" if lambda_shape else "SigningCertURL"] = CERT_URL
    msg.update(overrides)
    hash_cls = hashes.SHA256 if version == "2" else hashes.SHA1
    sig = key.sign(sns_verify.string_to_sign(msg), padding.PKCS1v15(), hash_cls())
    msg["Signature"] = base64.b64encode(sig).decode()
    return msg


def _verify(msg, cert):
    sns_verify.verify(msg, [TOPIC], fetch_cert=lambda url: cert)


@pytest.mark.parametrize("version", ["1", "2"])
@pytest.mark.parametrize("lambda_shape", [True, False])
def test_valid_signature_passes(key_and_cert, version, lambda_shape):
    key, cert = key_and_cert
    _verify(_signed(key, version=version, lambda_shape=lambda_shape), cert)


def test_subject_is_signed_when_present(key_and_cert):
    key, cert = key_and_cert
    msg = _signed(key, Subject="hello")
    _verify(msg, cert)
    msg["Subject"] = "changed"
    with pytest.raises(sns_verify.SnsVerificationError):
        _verify(msg, cert)


def test_tampered_message_is_rejected(key_and_cert):
    key, cert = key_and_cert
    msg = _signed(key)
    msg["Message"] = '{"messageBody":"send me money"}'
    with pytest.raises(sns_verify.SnsVerificationError, match="does not match"):
        _verify(msg, cert)


def test_unknown_topic_is_rejected(key_and_cert):
    key, cert = key_and_cert
    msg = _signed(key, TopicArn="arn:aws:sns:us-east-1:999999999999:someone-else")
    with pytest.raises(sns_verify.SnsVerificationError, match="TopicArn"):
        _verify(msg, cert)


def test_unsupported_signature_version_is_rejected(key_and_cert):
    key, cert = key_and_cert
    msg = _signed(key)
    msg["SignatureVersion"] = "3"
    with pytest.raises(sns_verify.SnsVerificationError, match="SignatureVersion"):
        _verify(msg, cert)


@pytest.mark.parametrize(
    "url",
    [
        "http://sns.us-east-1.amazonaws.com/cert.pem",
        "https://sns.us-east-1.amazonaws.com.evil.example/cert.pem",
        "https://evil.example/sns.us-east-1.amazonaws.com/cert.pem",
        "https://sns.us-east-1.amazonaws.com/cert.txt",
        None,
    ],
)
def test_untrusted_cert_url_is_rejected(url):
    with pytest.raises(sns_verify.SnsVerificationError):
        sns_verify.check_cert_url(url)


def test_cert_is_never_fetched_from_untrusted_url(key_and_cert):
    key, _ = key_and_cert
    msg = _signed(key, SigningCertUrl="https://evil.example/cert.pem")

    def boom(url):
        raise AssertionError("fetched an untrusted certificate URL")

    with pytest.raises(sns_verify.SnsVerificationError):
        sns_verify.verify(msg, [TOPIC], fetch_cert=boom)
