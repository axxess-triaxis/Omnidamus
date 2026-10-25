"""Local development server only -- not deployed.

`uvicorn omnidamus.web:app` lets you try the agent's routing decision for a single
message without AWS inbound plumbing. It is unauthenticated and does not send
anything or touch DynamoDB; the deployed path is lambda_handler.py, which only
accepts signature-verified SNS records and direct invocations.
"""

from __future__ import annotations

from fastapi import FastAPI

from .agent import route_message
from .schema import Channel, InboundMessage

app = FastAPI(title="Omnidamus (local)")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/preview/{channel}")
def preview(channel: Channel, conversation_id: str, from_address: str, body: str) -> dict:
    """Return the agent's RoutingDecision for one message. Sends nothing."""

    message = InboundMessage(
        conversation_id=conversation_id, channel=channel, from_address=from_address, body=body
    )
    return {"decision": route_message(message).model_dump(mode="json")}
