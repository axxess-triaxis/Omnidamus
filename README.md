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

Built fresh for this hackathon (no code shared with any other project).

**Built and covered by tests (52 passing, no AWS account needed to run them):**

- Conversation memory in Amazon DynamoDB ([`state.py`](src/omnidamus/state.py)):
  one item per customer phone number, so SMS and WhatsApp from the same person are
  one conversation. Optimistic versioning guards concurrent writes; items expire
  after 30 days.
- Inbound parsing for both channels ([`inbound.py`](src/omnidamus/inbound.py)),
  using the payload shapes in AWS's EUM Social and EUM SMS documentation.
- SNS signature verification on every inbound record
  ([`sns_verify.py`](src/omnidamus/sns_verify.py)): topic allowlist, HTTPS-only
  SNS certificate URLs, SignatureVersion 1 and 2.
- The conversation loop ([`service.py`](src/omnidamus/service.py)): the agent sees
  the conversation's history; replies go to the right address per channel;
  WhatsApp's 24-hour free-text window is enforced (outside it, the customer gets a
  click-to-chat link instead); SNS redeliveries are dropped; the closing receipt is
  sent by Amazon SES exactly once when the agent marks the conversation complete.
- Infrastructure as code ([`template.yaml`](template.yaml), AWS SAM): DynamoDB,
  two SNS topics with service-scoped publish policies, the Lambda function with
  least-privilege IAM, a dead-letter queue. Passes `cfn-lint`.

**Not yet done** — say so plainly rather than implying otherwise: no AWS resources
have been provisioned for this project yet (no linked WhatsApp Business Account, no
SMS number, no verified SES sender), the stack has not been deployed, no live
message has been sent or received, and the agent's prompt has not been tuned
against real conversations. The demo video and the Devpost submission are not done.

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

## Deploy

One-time provisioning in the AWS console (same region as the stack):

1. **AWS End User Messaging Social:** link a WhatsApp Business Account and phone
   number. Use a test number, not a production one.
2. **AWS End User Messaging SMS:** get a phone number that supports two-way SMS in
   the customer's country.
3. **Amazon SES:** verify the sender identity. In the SES sandbox, also verify each
   recipient address.
4. **Amazon Bedrock:** make sure the model in `BedrockModelId` is enabled.

Then build and deploy (needs Docker and the
[AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)):

```bash
sam build
sam deploy --guided
```

Finally, connect the inbound side using the stack outputs:

- EUM Social console → Business account → **Event destination** → Amazon SNS →
  `WhatsAppInboundTopicArn`.
- EUM SMS phone number → enable **two-way SMS** → destination Amazon SNS →
  `SmsInboundTopicArn`.

Start a conversation:

```bash
python scripts/start_conversation.py --function <FunctionName> --phone +14255550123 \
  --email you@example.com --context "Order 1042 ready for pickup" \
  --notification "Your order 1042 is ready. Reply with a pickup time."
```

## License

MIT — see [LICENSE](LICENSE).
