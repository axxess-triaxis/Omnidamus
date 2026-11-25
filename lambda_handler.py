"""AWS Lambda entry point.

Two kinds of event reach this function, and no public HTTP endpoint is needed for
either:

1. SNS records from the three inbound topics -- AWS End User Messaging Social
   (WhatsApp messages and delivery receipts), AWS End User Messaging SMS (two-way
   SMS) and Amazon SES receiving (email). Every record is checked before it is
   parsed: its TopicArn must be one of the configured topics and its SNS signature
   must verify (sns_verify.py).
2. A direct invocation {"action": "start_conversation", ...} to open a conversation
   with an SMS/RCS notification or a WhatsApp template. Only callers with
   lambda:InvokeFunction on this function can send it (scripts/start_conversation.py).
3. POST /start on the Function URL (2026-11-20, off unless StartApiKeySecretArn is set):
   AXXESS's "Message with agent" opens a conversation for an organization. It needs the
   x-api-key header to match the secret, and only organization starts are accepted.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os

from omnidamus import inbound, service, sns_verify
from omnidamus.redact import error_outline
from omnidamus.schema import Channel, InvalidRequest
from omnidamus.service import StartRefused
from omnidamus.state import ConversationStore

logging.getLogger().setLevel(logging.INFO)
# Strands logs the model's raw text when its output does not parse. What went wrong still
# reaches the log through HandlerError.
logging.getLogger("strands").setLevel(logging.CRITICAL)
log = logging.getLogger(__name__)

_store: ConversationStore | None = None


def _get_store() -> ConversationStore:
    global _store
    if _store is None:
        _store = ConversationStore()
    return _store


def _topics() -> dict[str, str]:
    topics = {
        os.environ.get("OMNIDAMUS_WHATSAPP_TOPIC_ARN", ""): "whatsapp",
        os.environ.get("OMNIDAMUS_SMS_TOPIC_ARN", ""): "sms",
        os.environ.get("OMNIDAMUS_EMAIL_TOPIC_ARN", ""): "email",
    }
    topics.pop("", None)
    return topics


_PARSERS = {
    "whatsapp": "parse_whatsapp_event",
    "sms": "parse_sms_event",
    "email": "parse_ses_event",
}


def _handle_sns_record(record: dict) -> list[dict]:
    sns = record["Sns"]
    topics = _topics()
    sns_verify.verify(sns, allowed_topic_arns=topics.keys())

    kind = topics[sns["TopicArn"]]
    results = []
    for message in getattr(inbound, _PARSERS[kind])(sns["Message"]):
        results.append(service.handle_inbound(message, _get_store()))
    if kind == "whatsapp":
        log.info(json.dumps({"whatsapp_event": inbound.outline_whatsapp_event(sns["Message"])}))
        for status in inbound.parse_whatsapp_statuses(sns["Message"]):
            service.apply_whatsapp_status(status, _get_store())
    return results


_start_key: str | None = None
START_FIELDS = ("organization_id", "job_id", "line", "phone", "channel")


def _start_api_key() -> str | None:
    global _start_key
    if _start_key is None:
        arn = os.environ.get("OMNIDAMUS_START_API_KEY_SECRET_ARN", "").strip()
        if not arn:
            return None
        import boto3

        _start_key = boto3.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"].strip()
    return _start_key if len(_start_key) >= 32 else None


def _http(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": {"content-type": "application/json"}, "body": json.dumps(body)}


def _handle_http(event: dict) -> dict:
    """POST /start from AXXESS. Ids and outcomes only in logs and replies: no phone numbers."""
    http = event["requestContext"]["http"]
    if http.get("method") != "POST" or event.get("rawPath") != "/start":
        return _http(404, {"error": "not_found"})
    expected = _start_api_key()
    given = (event.get("headers") or {}).get("x-api-key", "")
    if not expected or not hmac.compare_digest(given.encode(), expected.encode()):
        return _http(401, {"error": "unauthorized"})
    raw = event.get("body") or ""
    try:
        body = json.loads(base64.b64decode(raw) if event.get("isBase64Encoded") else raw)
    except (ValueError, TypeError):
        return _http(400, {"error": "invalid_json"})
    if not isinstance(body, dict) or any(not isinstance(body.get(k), str) or not body[k].strip() for k in START_FIELDS):
        return _http(400, {"error": "missing_fields"})
    try:
        channel = Channel(body["channel"])
        result = service.start_conversation(
            phone=inbound.normalize_phone(body["phone"]),
            store=_get_store(),
            notification=body.get("notification"),
            context=str(body.get("context") or "")[:2000],
            channel=channel,
            template=body.get("template"),
            template_params=[str(p) for p in body.get("template_params") or []],
            organization_id=body["organization_id"],
            job_id=body["job_id"],
            line=inbound.normalize_phone(body["line"]),
            whatsapp_origination_id=body.get("whatsapp_origination_id"),
            sms_origination_identity=body.get("sms_origination_identity"),
        )
    except StartRefused as exc:
        log.info(json.dumps({"start_refused": str(exc), "job_id": body["job_id"]}))
        return _http(422, {"error": "start_refused", "reason": str(exc)})
    except ValueError as exc:
        return _http(400, {"error": "invalid_request", "reason": str(exc)})
    if result.get("skipped") == "opted_out":
        log.info(json.dumps({"start": "opted_out", "job_id": body["job_id"]}))
        return _http(409, {"error": "opted_out"})
    log.info(json.dumps({"start": "sent", "job_id": body["job_id"], "message_id": result["message_id"]}))
    return _http(202, {"status": "started", "message_id": result["message_id"]})


class HandlerError(RuntimeError):
    """What leaves the handler in place of any unhandled error. Lambda writes it to the log, so
    it carries no customer data. It still fails the invocation: retries, the dead-letter queue
    and the alarms work as before."""


def handler(event: dict, context=None) -> dict:
    try:
        return _handle(event)
    except Exception as exc:  # noqa: BLE001 -- re-raised below, masked
        outline = error_outline(exc)
    # Outside the except block, so the original is not chained to it.
    raise HandlerError(json.dumps(outline))


def _handle(event: dict) -> dict:
    if isinstance(event.get("requestContext"), dict) and "http" in event["requestContext"]:
        return _handle_http(event)
    if event.get("action") == "start_conversation":
        channel = Channel(event.get("channel", Channel.SMS_RCS.value))
        return service.start_conversation(
            phone=inbound.normalize_phone(event["phone"]),
            store=_get_store(),
            notification=event.get("notification"),
            email=event.get("email"),
            context=event.get("context", ""),
            channel=channel,
            template=event.get("template"),
            template_params=event.get("template_params"),
            organization_id=event.get("organization_id"),
            job_id=event.get("job_id"),
            line=inbound.normalize_phone(event["line"]) if event.get("line") else None,
            whatsapp_origination_id=event.get("whatsapp_origination_id"),
            sms_origination_identity=event.get("sms_origination_identity"),
        )

    records = [r for r in event.get("Records", []) if r.get("EventSource") == "aws:sns"]
    if not records:
        raise InvalidRequest("Unrecognized event")

    results = []
    for record in records:
        try:
            results.extend(_handle_sns_record(record))
        except sns_verify.SnsVerificationError as exc:
            # Not ours or tampered with: log and drop, never retry.
            log.warning("Rejected SNS record: %s", exc)
    log.info(json.dumps({"processed": len(results)}))
    return {"results": results}
