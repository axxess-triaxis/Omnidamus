# Live verification

What was run against real AWS and a real phone, with the evidence for each claim.
Times are IST (UTC+5:30). Phone numbers and email addresses are masked:
the customer's WhatsApp number is `+91XXXXXX7374`, the receipt mailbox is
`o*******@triaxisventures.com`, and the sender is `om*******@triaxisventures.com`.

## Summary

| Step | Status | Evidence |
|---|---|---|
| Stack deployed (ap-southeast-2) | Done | [Deployment](#deployment) |
| Inbound WhatsApp wired | Done | [Wiring](#inbound-wiring) |
| Conversation opened with an approved WhatsApp template | Done, delivered | [27 Oct 16:23](#conversation-27-oct) |
| Two-way WhatsApp chat with the agent (Nova Pro) | Done, delivered until 27 Oct 16:49 | [27 Oct 16:48-16:49](#conversation-27-oct) |
| Conversation closes, SES receipt arrives | Done (after an IAM fix) | [Receipt](#closing-receipt) |
| STOP / START | Opt-out and opt-in recorded; STOP and START confirmations delivered and read 25 Nov | [STOP and START](#stop-and-start) |
| Customer replies by email | **Not done** | [Not verified](#not-verified) |
| SMS | **Not done** (optional; Indian SMS needs DLT registration) | [Not verified](#not-verified) |

From 27 Oct 21:58, Meta refused outbound WhatsApp messages with error **131037**
("WhatsApp provided number needs display name approval before message can be sent").
The last failure recorded here is 28 Oct 15:12; a message was delivered and read on
29 Oct, and the STOP confirmation on 25 Nov. See [Open issues](#open-issues).

## Deployment

- AWS account `227214487086`, region **ap-southeast-2**. That is the region where the
  WhatsApp Business Account was already linked; an attempt to link it in us-east-1
  failed with "WABA is already configured in another AWS region"
  (RequestId `a2249fd1-04a9-4414-afa4-ab88385cb005`).
- Region availability was checked against the AWS General Reference endpoint tables
  (SES receiving, End User Messaging Social) and the Bedrock model card before choosing.
- `sam build` and `sam deploy`, stack `omnidamus`:
  - first deploy `CREATE_COMPLETE` on 26 Oct 16:04:40
  - redeploys `UPDATE_COMPLETE` on 27 Oct 21:53:56, 22:11:18 and 22:43:12 (fixes below)
- Parameters: `BedrockModelId=apac.amazon.nova-pro-v1:0`,
  `WhatsAppPhoneNumberId=phone-number-id-a114e89244d0443f9ad6354afbb5c7c8`
  (Meta test number `+1 555-XXX-8714`), `SesIdentity=triaxisventures.com`,
  `InboundEmailAddress` empty, no SMS identity, no alarm email.
- Resources: function `omnidamus-OmnidamusFunction-t2yytmoeQwpF`, table
  `omnidamus-ConversationsTable-ITS8GHHHQY5B`, topic
  `omnidamus-WhatsAppInboundTopic-4EvZxn2GiJgi`.

**Model.** The template's default model is Claude Sonnet 4.6. Its first call failed with
`AccessDeniedException ... INVALID_PAYMENT_INSTRUMENT` (the AWS Marketplace subscription
could not be created), so the live stack runs Amazon Nova Pro (`apac.amazon.nova-pro-v1:0`).
Nova in ap-southeast-2 was briefly blocked ("Your account is currently being verified")
on 26 Oct 15:49 and worked at 17:06.

## Inbound wiring

`scripts/connect_inbound.py` dry run, then `--apply` on 26 Oct 17:06:01:

```
[APPLY] WhatsApp: waba-295a454d41424b839274f1465e253f5e (Sudipta Sarmah) event destination -> arn:aws:sns:ap-southeast-2:227214487086:omnidamus-WhatsAppInboundTopic-4EvZxn2GiJgi
[skip ] SMS: no --sms-phone-number-id
[skip ] Email: stack deployed without InboundEmailAddress
```

`GetLinkedWhatsAppBusinessAccount` afterwards returned that topic as the only event
destination.

## Conversation (27 Oct)

Template `demo_request` (Utility, `en`) was submitted on 26 Oct 17:07:27 (Meta template ID
`2073046406652805`) and was `APPROVED` by 27 Oct 16:22:38.

| Time | Direction | Text | ID | Delivered |
|---|---|---|---|---|
| 16:23:21 | out (template) | "Hi Sudipta, we have received your demo request with Triaxis Ventures. ..." | `8dea7e76-2a7d-4dbf-87af-bab91d283da0` | Yes (screenshot) |
| 16:48:30 | in | "Hi" | | |
| 16:48:37 | out | "Hello! I'd be happy to set up a demo for you. Please let me know your preferred time for the demo." | `efc259cc-e190-4408-83fa-c928ab64f646` | Yes (screenshot) |
| 16:49 | in | "Tomorrow, 6 pm IST" | | |
| 16:49:05 | out | "Great! Your demo is confirmed for tomorrow at 6 pm IST. You will receive a confirmation email shortly." | not stored (see below) | Yes (screenshot) |
| 16:49:18 | in | "Thanks" | | |
| 16:49:20 | out | "Great! Could you please let me know your preferred time for the demo?" | `c381bb59-e541-49d2-a189-7c91a258b0e1` | Yes (screenshot) |

The template was sent with `scripts/start_conversation.py --channel whatsapp --template demo_request`.
The agent's routing decisions appear in the function log, for example:

```
16:48:35 <thinking> The customer has responded with a simple 'Hi' on WhatsApp, which is an open channel for free-text replies. ...
16:48:35 Tool #1: RoutingDecision
16:48:37 {"processed": 1}
```

## Closing receipt

**First attempt failed (27 Oct 16:49:05).** The agent closed the conversation and SES refused
the receipt:

```
[ERROR] SesSendError: SES send_email failed: An error occurred (AccessDeniedException) when calling the SendEmail operation: User 'arn:aws:sts::227214487086:assumed-role/omnidamus-OmnidamusFunctionRole-xLIxxXx8MPce/omnidamus-OmnidamusFunction-t2yytmoeQwpF' is not authorized to perform 'ses:SendEmail' on resource 'arn:aws:ses:ap-southeast-2:227214487086:configuration-set/my-first-configuration-set'
```

The `triaxisventures.com` identity has a default configuration set, and the function's
policy only covered the identity. The exception also stopped the turn from being saved:
the "6 pm" message and the bot's confirmation are missing from DynamoDB. Lambda retried
twice (16:50:02, 16:52:05), and the event then went to the dead-letter queue
(1 message; `FailedInvocationsAlarm` went to ALARM on 27 Oct 16:57:48).

**Fix, deployed 27 Oct 21:53:56:**

- `template.yaml`: `ses:SendEmail` is also allowed on `configuration-set/*`.
- `service.py`: a failed receipt is logged and the turn is still saved, so a retry is dropped
  as a duplicate instead of re-running the agent. Test:
  `test_failed_receipt_still_saves_the_turn`.

**Second attempt succeeded (27 Oct 21:58).**

| Time | Direction | Text | ID |
|---|---|---|---|
| 21:58:12 | in | "Please confirm my demo for tomorrow, 6 pm IST" | |
| 21:58:17 | out WhatsApp | "Your demo has been confirmed for tomorrow at 6 pm IST. You will receive a confirmation email shortly." | `fc874062-8d02-47a7-adf9-faa81d98bca7` (not delivered, see open issues) |
| 21:58:17 | out email | Receipt to `o*******@triaxisventures.com` | SES `010801a17e30e9f3-c88a433d-c0db-41f4-b62a-e3a448d3b264-000000` |

DynamoDB afterwards: `status=complete`, `key_facts={"demo_time": "tomorrow, 6 pm IST"}`,
`receipt_message_id` set. The founder confirmed the email arrived in the inbox.

There is no booking system. "Confirmed" means the agent recorded the time in the
conversation and sent the receipt; no calendar entry is created.

## STOP and START

| Time | In | State after | Reply ID | Reply delivered |
|---|---|---|---|---|
| 27 Oct 22:00:24 | STOP | `opted_out=true` | `93bf4412-3413-4638-bd43-75e1be360859` | No |
| 27 Oct 22:31:12 | START | `opted_out=false` | `3779c6b5-6042-4b55-9e05-e849a5668c9f` | No |
| 28 Oct 09:35:30 | STOP | `opted_out=true` | `096c13f3-be03-4fc2-9f8e-b3ed3b76ef38` | No (error 131037) |
| 28 Oct 15:12:11 | START | `opted_out=false` | `41337899-1ff0-4bd3-ad7e-c1358fe970bd` | No (error 131037) |

The opt-out and opt-in logic worked: the flag changed each time and the agent was not
called. The confirmation messages were accepted by AWS but never reached the phone. Meta's
status for the 28 Oct messages (logged after adding delivery-status logging on 27 Oct 22:43):

```
09:35:30 {"whatsapp_event": {"aws_message_id": "096c13f3-be03-4fc2-9f8e-b3ed3b76ef38", "changes": [{"field": "messages", "value_keys": ["metadata", "statuses"], "statuses": [{"id": null, "status": "failed", "errors": [{"code": 131037, "message": "(#131037) WhatsApp provided number needs display name approval before message can be sent.", ...}]}]}]}}
```

The customer's number is **opted in** as of 28 Oct 15:12:11.

### 25 Nov re-check

The messages came from a second phone, `+91XXXXXX9963`, so this was a new conversation.
They reached AWS at 15:28, START first. Statuses are from CloudWatch (all `errors: []`):

| Time | In | Reply | Reply ID | Statuses |
|---|---|---|---|---|
| 15:28:34 | START | "Hello! How can I assist you today?" (from the agent) | `ab09d644-8c3f-40d4-8504-a8ec908d58c5` | accepted, sent, delivered, read |
| 15:28:43 | STOP | "You won't hear from us again. Reply START if you change your mind." | `00ede2a7-a614-4e80-a464-268cc5cd2bef` | accepted, sent, delivered, read |

The STOP confirmation is verified. That phone was not opted out when its START arrived,
so START went to the agent (open issue 5), and the 15:28:43 STOP then opted it out.

At 20:06, on the fixed code ([Fixes deployed 25 Nov](#fixes-deployed-25-nov)), it sent
START again:

| Received (IST) | Message | Reply | Message ID | Statuses |
|---|---|---|---|---|
| 20:06:42 | START | "You're subscribed again. How can we help?" | `c770b0be-b2cf-43d6-974b-230d88a3a04a` | accepted, sent, delivered, read |

The reply went out once, one second after the message. The START confirmation is
verified, and `+91XXXXXX9963` is opted in.

## Not verified

- **Customer replies by email.** SES receiving needs an MX record on a domain we control.
  `triaxisventures.com` DNS is hosted by Wix, which would not accept an MX or NS record
  for the `reply` subdomain, so the stack was deployed without `InboundEmailAddress`.
  A Route 53 zone for `reply.triaxisventures.com` exists (stack `omnidamus-reply-dns`,
  `infra/reply-dns.yaml`) but is not delegated, so it receives nothing.
- **SMS.** No SMS number; Indian SMS needs DLT registration. Out of scope for the demo.
- **Claude on Bedrock.** Blocked by the Marketplace payment error above.

## Open issues

1. **Meta error 131037 (WhatsApp delivery, 27 Oct 21:58 to 28 Oct 15:12 at least).**
   Cleared by 29 Oct. Both test numbers had the display name "Sudipta Sarmah". On 28 Oct
   the founder submitted "Triaxis Ventures" with a business profile in WhatsApp Manager.
   Meta sent a `phone_number_name_update` event at 10:29:27 (its values were not logged),
   but the 15:12 reply still failed with 131037. On 29 Oct 18:14 (12:44 UTC) an outbound
   message went accepted, sent, delivered and read with `errors: []`, as recorded from
   CloudWatch on 20 Nov in [#7](https://github.com/axxess-triaxis/Omnidamus/pull/7).
   DynamoDB, read on 25 Nov, shows that turn (`a57e227d-e77b-4e7a-ae63-8e0d463042fc`).
   On 25 Nov both replies were delivered and read. A START confirmation sent
   after a STOP was delivered and read at 20:06 the same day.
2. **Delivery receipts are never recorded.** Receipts are matched on Meta's message ID,
   but turns store AWS's message ID. Status events carry AWS's ID in the top-level
   `messageId`, and failed statuses have no Meta ID at all. Seen again on 25 Nov: all
   eight statuses logged `matched_turn: false`. Fixed, see
   [Fixes deployed 25 Nov](#fixes-deployed-25-nov).
3. **Receipt subject and first line are the agent's internal summary.** For example:
   "Confirmation: Remind the customer of the confirmed demo time and close the
   conversation with an email receipt." Fixed in code, not checked live.
4. **A question can be answered by email only.** On 28 Oct 09:34, "What time is my demo?"
   got a second receipt (SES `010801a180ae602a-d1e5a3e7-6998-463d-a05d-ad1f076d281f-000000`)
   and no WhatsApp reply, because the agent closed the conversation again. Fixed in
   code, not checked live.
5. **START while already opted in goes to the agent.** On 28 Oct 09:35 it produced a
   confirmation and a third receipt (SES `010801a180aef759-945bb0d5-3872-4027-ba9e-1736f9aa4fc2-000000`).
   START no longer reaches the agent (checked on staging). "Receipt exactly once" still
   holds per completion, not per conversation.
6. **Dead-letter queue.** Cleared by itself: on 25 Nov 19:49 the queue was empty and both
   alarms were OK. The failed 27 Oct 16:49 event was not removed by hand.
7. **Logged Meta message IDs contain the customer's phone number.** A `wamid` is base64
   and decodes to a value that includes the recipient's number, so the status logging
   added on 27 Oct puts phone numbers in CloudWatch in recoverable form, although the
   tests check that none appear in plain text. Fixed for new entries; entries from
   before 25 Nov 19:48 are unchanged.

## Fixes deployed 25 Nov

[#12](https://github.com/axxess-triaxis/Omnidamus/pull/12), commit `54ac872`, image
`sha256:1fc8dd438d0a0d22dbf8b579e3df03eed4ed700feee1cde90dfe0b3b79e30541`. The change set
changed only the function's code. Roll back to
`sha256:7535492c2089180fee1b947fd00d4c1345a4fa8251edcce15b41691504d65898`.

| Where | Check | Result |
|---|---|---|
| Staging, 16:43 | START from a new number, STOP, START (replies captured, not sent) | Fixed reply; opt-out confirmation; "You're subscribed again. How can we help?" |
| Staging, 16:43 | Synthetic sent, read, then delivered; and a failed status with no Meta ID | `read` kept, shown on the turn; `failed` recorded |
| Staging, 16:43 | 42 log events searched for the test number, the raw `wamid` and agent text | None found |
| Staging, 16:44-16:55 | AXXESS cross-product test, run [45602536225](https://github.com/axxess-triaxis/AXXESSTRIAXIS/actions/runs/45602536225) | 7 of 7 passed |
| Production, 19:48 | Stack `omnidamus` `UPDATE_COMPLETE` (19:47:59); `/start` with no key and a wrong key | 401 and 401 |
| Production, 19:48 | Synthetic receipt, sent to no one | Recorded; 16 log events with no number, raw `wamid` or error |
| Production, 20:06 | A real START from an opted-out phone | Opted in; one reply, after one second; the founder confirmed it on the phone |
| Production, 20:06 | That reply's receipts | accepted, sent, delivered and read, all `recorded: true` with `errors: []`; stored as `read` |
| Production, 20:06 | 30 log events searched for phone numbers, raw `wamid`, tracebacks and agent text | None found |
| Production, 20:08 | Alarms | OK |

Not checked live: the receipt email's subject, the email-only fix, and START while already
subscribed (staging only).

## Code changes made during verification

| File | Change |
|---|---|
| `template.yaml` | `BedrockRegion` parameter (empty = stack region); `SmsOriginationIdentity` defaults to empty; `ses:SendEmail` on `configuration-set/*` |
| `src/omnidamus/service.py` | Failed receipt no longer loses the turn; delivery statuses logged (no phone numbers) |
| `src/omnidamus/inbound.py` | `outline_whatsapp_event`: phone-free outline of each WhatsApp event for the log |
| `lambda_handler.py` | Logs that outline for every WhatsApp event |
| `tests/` | 3 new tests (83 total, all passing); `sam validate --lint` passes |
| `infra/reply-dns.yaml` | Route 53 zone for the receiving subdomain (deployed, not delegated) |

The repo's Python environment cannot read `aws login` credentials without
`botocore[crt]`. The scripts were run with short-lived credentials exported by
`aws configure export-credentials` instead.
