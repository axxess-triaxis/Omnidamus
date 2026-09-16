# Omnidamus

An omnichannel conversational relay agent, built for the **AWS Communication
Developer Services (CDS) Agentic AI Partner Hackathon**
([aws-cds-partner.devpost.com](https://aws-cds-partner.devpost.com), deadline Oct 29, 2026).

## Concept

Customer conversations rarely stay in one channel. Omnidamus is an agent that lets a
conversation **start in one channel and complete an action in another** — the exact
pattern the hackathon brief calls out: a notification goes out over SMS/RCS, the
two-way back-and-forth happens over WhatsApp, and a written record of the outcome
lands by email — all as one continuous conversation the agent tracks, not three
disconnected integrations.

**Reference scenario** (used to drive the initial build): an appointment/order
lifecycle notification.

1. **AWS End User Messaging (SMS/RCS)** sends the initial notification (e.g. "your
   pickup is ready — reply to schedule a time").
2. **AWS End User Messaging Social (WhatsApp)** carries the actual back-and-forth —
   the agent asks clarifying questions and reasons about the reply.
3. **Amazon SES** sends the final written confirmation/receipt once the exchange
   resolves.

The agent (not a fixed script) decides, per inbound message, whether to reply on the
same channel, hand off to a different one, or close out the conversation.

## Status

Early scaffold, built fresh for this hackathon (no code shared with any other
project). What exists right now:

- Pydantic schemas for conversation events and routing decisions
  ([`schema.py`](src/omnidamus/schema.py))
- Thin, boto3-backed wrappers for each AWS CDS channel
  ([`channels/`](src/omnidamus/channels)) — verified against AWS's own API
  documentation, not guessed
- A deterministic router skeleton for cross-channel handoff
  ([`router.py`](src/omnidamus/router.py))
- A Strands Agents SDK / Amazon Bedrock agent skeleton
  ([`agent.py`](src/omnidamus/agent.py))
- A FastAPI app wiring the above together ([`web.py`](src/omnidamus/web.py))
- Mocked unit tests for the channel wrappers and router

**Not yet done** — say so plainly rather than implying otherwise: no AWS resources
have been provisioned (no linked WhatsApp Business Account, no RCS agent, no SES
verified sender identity), nothing has been deployed, no live message has been sent
or received, and the agent's reasoning prompt has not been tuned against real
conversations. The architecture diagram, demo video, and ACE opportunity required
for hackathon submission are also not yet done.

## AWS CDS services used

| Channel | Service | boto3 client | Key operation(s) |
|---|---|---|---|
| Email | Amazon SES | `sesv2` | `send_email` |
| SMS / RCS | AWS End User Messaging | `pinpoint-sms-voice-v2` | `send_text_message`, `send_rcs_message` |
| WhatsApp | AWS End User Messaging Social | `socialmessaging` | `send_whatsapp_message` |

Amazon Bedrock (via the [Strands Agents SDK](https://strandsagents.com/)) provides
the agentic reasoning layer, per the hackathon FAQ's confirmation that Strands is an
accepted alternative to Bedrock AgentCore.

## Setup

```bash
pip install -e ".[dev]"
cp .env.example .env  # fill in AWS + channel identifiers
pytest
```

AWS credentials resolve via the normal boto3 chain (`aws configure` / env vars) —
none are stored in this repo.

## License

MIT — see [LICENSE](LICENSE).
