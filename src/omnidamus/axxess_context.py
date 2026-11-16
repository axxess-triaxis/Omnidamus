"""What AXXESS already knows about this customer, read before the agent replies.

Automation plan Phase 3 (2026-11-16): Omnidamus asks the AXXESS MCP server for the customer's lead
(search_leads by phone, then email) and its history (get_lead_context: earlier calls, email replies,
meetings and conversations on every channel), so a reply does not ask again what the customer
already told us elsewhere.

Off unless AXXESS_MCP_URL and AXXESS_API_KEY_SECRET_ARN are set. Fails open: any error, timeout or
missing capability means the agent replies without the history, exactly as before. Logs carry no
phone number, email or history text.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

URL_ENV = "AXXESS_MCP_URL"
KEY_ENV = "AXXESS_API_KEY_SECRET_ARN"
BYPASS_ENV = "AXXESS_BYPASS_SECRET_ARN"
TIMEOUT_SECONDS = 4.0
MAX_BRIEF = 1500
MAX_LINES = 10

_secrets: dict[str, str] = {}


def enabled() -> bool:
    return bool(os.environ.get(URL_ENV, "").strip() and os.environ.get(KEY_ENV, "").strip())


def _secret(arn: str) -> str:
    if arn not in _secrets:
        import boto3

        _secrets[arn] = boto3.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"].strip()
    return _secrets[arn]


def _call(name: str, arguments: dict):
    headers = {
        "Authorization": f"Bearer {_secret(os.environ[KEY_ENV])}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    bypass_arn = os.environ.get(BYPASS_ENV, "").strip()
    if bypass_arn:
        headers["x-vercel-protection-bypass"] = _secret(bypass_arn)
    body = json.dumps({"jsonrpc": "2.0", "id": f"omnidamus-{name}", "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}}).encode()
    request = urllib.request.Request(os.environ[URL_ENV], data=body, method="POST", headers=headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        reply = json.loads(response.read().decode())
    if "error" in reply:
        raise RuntimeError(f"AXXESS MCP error {reply['error'].get('code')}")
    result = reply.get("result") or {}
    if result.get("isError"):
        raise RuntimeError(f"AXXESS tool {name} refused")
    return json.loads((result.get("content") or [{}])[0].get("text") or "{}")


def _find_lead_id(phone: str | None, email: str | None) -> str | None:
    """The lead whose phone or email matches exactly. search_leads matches substrings, so the
    results are checked here before one is used."""
    for term, field in ((phone, "contactPhoneE164"), (email, "contactEmail")):
        if not term:
            continue
        found = _call("search_leads", {"query": term})
        for lead in found if isinstance(found, list) else []:
            if (lead.get(field) or "").lower() == term.lower():
                return lead.get("id")
    return None


def _flat(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _line(entry: dict) -> str | None:
    day = (entry.get("at") or "")[:10]
    kind = entry.get("kind")
    if kind == "conversation":
        return f"{day} {entry.get('direction')} {entry.get('channel')}: {_flat(entry.get('summary', ''), 220)}"
    if kind == "reply":
        return f"{day} email reply ({str(entry.get('category', '')).replace('_', ' ')}): {_flat(entry.get('subject', ''), 120)}"
    if kind == "milestone":
        note = f": {_flat(entry['note'], 120)}" if entry.get("note") else ""
        return f"{day} {str(entry.get('milestone', '')).replace('_', ' ')}{note}"
    if kind == "attempt" and entry.get("decision") == "allowed" and entry.get("outcome"):
        note = f" ({_flat(entry['note'], 120)})" if entry.get("note") else ""
        return f"{day} {str(entry.get('channel', '')).replace('_', ' ')} call: {str(entry['outcome']).replace('_', ' ')}{note}"
    return None


def brief(context: dict) -> str | None:
    """Newest first, one line per event, at most MAX_LINES lines and MAX_BRIEF characters."""
    lines: list[str] = []
    length = 0
    lead = context.get("lead") or {}
    if lead.get("stage"):
        header = f"Lead stage: {lead['stage']}" + (f", company: {lead['organizationName']}" if lead.get("organizationName") else "")
        lines.append(header)
        length += len(header) + 1
    timeline = sorted(context.get("timeline") or [], key=lambda e: e.get("at") or "", reverse=True)
    for entry in timeline:
        text = _line(entry)
        if not text:
            continue
        if length + len(text) + 1 > MAX_BRIEF or len(lines) >= MAX_LINES:
            break
        lines.append(text)
        length += len(text) + 1
    return "\n".join(lines) if len(lines) > (1 if lead.get("stage") else 0) else None


def lead_history(phone: str | None, email: str | None) -> str | None:
    """The brief for this customer, or None when off, unknown, or AXXESS could not answer in time."""
    if not enabled() or not (phone or email):
        return None
    try:
        lead_id = _find_lead_id(phone, email)
        if not lead_id:
            return None
        return brief(_call("get_lead_context", {"leadId": lead_id}))
    except (urllib.error.URLError, TimeoutError, RuntimeError, ValueError, KeyError) as error:
        log.warning("axxess_context_unavailable: %s", type(error).__name__)
        return None
    except Exception as error:  # noqa: BLE001 - never let context reading block a reply
        log.warning("axxess_context_failed: %s", type(error).__name__)
        return None
