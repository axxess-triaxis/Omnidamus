"""Builds the Strands Agent that reasons about cross-channel conversation routing.

Uses the Strands Agents SDK on Amazon Bedrock -- the hackathon FAQ confirms this
is an accepted alternative to Bedrock AgentCore. `structured_output_model` forces
the model's reply into `RoutingDecision`, so `router.py` never has to parse prose.
"""

from __future__ import annotations

import os

import boto3
from strands import Agent
from strands.models import BedrockModel

from .schema import InboundMessage, RoutingDecision

SYSTEM_PROMPT = """You are Omnidamus, an agent that carries one customer conversation across \
multiple channels -- SMS/RCS, WhatsApp, and email -- deciding at each inbound message what \
should happen next.

You will be handed the text of one inbound message and which channel it arrived on. Decide:

- reply_channel: which channel to reply on. Usually the same channel the message arrived on, \
  unless a handoff is in progress.
- reply_text: the exact text to send back.
- handoff_to: set only when the conversation should move to a different channel next -- for \
  example, an initial SMS/RCS notification that needs real back-and-forth belongs on WhatsApp; \
  a WhatsApp exchange that has reached a firm outcome belongs on email as a written receipt. \
  Leave unset when the conversation should continue on its current channel.
- conversation_complete: true only when the exchange has reached a final, actionable outcome \
  (a confirmed time, a resolved request, an explicit decline) -- not merely when the user stops \
  responding.
- key_facts: concrete facts actually present in the message -- names, dates, amounts, decisions \
  -- never invented.

Be concrete: pull real details out of the message rather than describing it in the abstract."""


def get_model() -> BedrockModel:
    region = os.environ.get("BEDROCK_REGION", "us-east-1")
    session = boto3.Session(profile_name=os.environ.get("AWS_PROFILE"), region_name=region)
    return BedrockModel(
        model_id=os.environ.get(
            "OMNIDAMUS_BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-4-6"
        ),
        boto_session=session,
        temperature=0.2,
    )


def build_agent() -> Agent:
    return Agent(model=get_model(), system_prompt=SYSTEM_PROMPT)


def route_message(agent: Agent, message: InboundMessage) -> RoutingDecision:
    prompt = (
        f"Channel: {message.channel.value}\n"
        f"From: {message.from_address}\n"
        f"Conversation ID: {message.conversation_id}\n\n"
        f"Message:\n{message.body}"
    )
    response = agent(prompt, structured_output_model=RoutingDecision)
    return response.structured_output
