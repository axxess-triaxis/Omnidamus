"""POST /start on the Function URL (2026-11-20): only AXXESS, holding the key, can open a
conversation for an organization, and only an organization start is accepted."""

from __future__ import annotations

import base64
import json
from unittest.mock import patch

import pytest

import lambda_handler
from omnidamus.service import StartRefused

KEY = "k" * 40
BODY = {"organization_id": "11111111-2222-3333-4444-555555555555",
        "job_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "line": "+971500000001",
        "phone": "+971500000099", "channel": "sms_rcs", "notification": "Hello",
        "sms_origination_identity": "sender-a"}


@pytest.fixture(autouse=True)
def configured_key(monkeypatch):
    # Not named "key": a parametrize argument of that name would replace this fixture.
    monkeypatch.setattr(lambda_handler, "_start_key", KEY)
    monkeypatch.setattr(lambda_handler, "_store", object())


def _event(body=BODY, key=KEY, method="POST", path="/start", b64=False):
    raw = json.dumps(body) if not isinstance(body, str) else body
    return {"rawPath": path, "requestContext": {"http": {"method": method}},
            "headers": {"x-api-key": key} if key is not None else {},
            "isBase64Encoded": b64, "body": base64.b64encode(raw.encode()).decode() if b64 else raw}


def _call(event, result=None, error=None):
    with patch("omnidamus.service.start_conversation",
               return_value=result or {"conversation_id": "c", "message_id": "m1"},
               side_effect=error) as start:
        out = lambda_handler.handler(event)
    return out["statusCode"], json.loads(out["body"]), start


def test_a_keyed_organization_start_is_accepted():
    status, body, start = _call(_event())
    assert (status, body) == (202, {"status": "started", "message_id": "m1"})
    kwargs = start.call_args.kwargs
    assert kwargs["organization_id"] == BODY["organization_id"] and kwargs["job_id"] == BODY["job_id"]
    assert kwargs["line"] == "+971500000001" and kwargs["sms_origination_identity"] == "sender-a"


def test_a_base64_body_is_decoded():
    assert _call(_event(b64=True))[0] == 202


@pytest.mark.parametrize("key", [None, "", "wrong" * 8, KEY[:-1]])
def test_a_missing_or_wrong_key_is_unauthorized(key):
    status, body, start = _call(_event(key=key))
    assert (status, body) == (401, {"error": "unauthorized"})
    start.assert_not_called()


def test_without_a_configured_key_everything_is_unauthorized(monkeypatch):
    monkeypatch.setattr(lambda_handler, "_start_key", None)
    monkeypatch.delenv("OMNIDAMUS_START_API_KEY_SECRET_ARN", raising=False)
    assert _call(_event())[0] == 401


def test_a_short_configured_key_is_never_accepted(monkeypatch):
    monkeypatch.setattr(lambda_handler, "_start_key", "short")
    assert _call(_event(key="short"))[0] == 401


@pytest.mark.parametrize("method, path", [("GET", "/start"), ("POST", "/"), ("POST", "/start/x")])
def test_other_routes_are_not_found(method, path):
    assert _call(_event(method=method, path=path))[0] == 404


@pytest.mark.parametrize("field", ["organization_id", "job_id", "line", "phone", "channel"])
def test_a_tenant_0_start_without_organization_fields_is_refused(field):
    status, body, start = _call(_event(body={k: v for k, v in BODY.items() if k != field}))
    assert (status, body) == (400, {"error": "missing_fields"})
    start.assert_not_called()


def test_invalid_json_is_rejected():
    assert _call(_event(body="{not json"))[:2] == (400, {"error": "invalid_json"})


def test_an_unknown_channel_is_rejected():
    assert _call(_event(body={**BODY, "channel": "carrier_pigeon"}))[0] == 400


def test_a_refused_start_is_unprocessable():
    status, body, _ = _call(_event(), error=StartRefused("An organization cannot send from a Triaxis number"))
    assert status == 422 and body["error"] == "start_refused"


def test_an_opted_out_contact_is_a_conflict():
    assert _call(_event(), result={"conversation_id": "c", "skipped": "opted_out"})[:2] == (409, {"error": "opted_out"})


def test_logs_carry_ids_not_phone_numbers(caplog):
    with caplog.at_level("INFO"):
        _call(_event())
    assert BODY["phone"] not in caplog.text and BODY["job_id"] in caplog.text


def test_the_direct_invoke_passes_organization_fields_through():
    event = {"action": "start_conversation", **BODY}
    with patch("omnidamus.service.start_conversation", return_value={}) as start:
        lambda_handler.handler(event)
    assert start.call_args.kwargs["organization_id"] == BODY["organization_id"]
    assert start.call_args.kwargs["line"] == "+971500000001"
