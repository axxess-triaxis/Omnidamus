"""Connect the inbound side of a deployed Omnidamus stack -- the steps that are
otherwise console clicks. Dry run by default; pass --apply to make changes.

1. WhatsApp: set the linked WhatsApp Business Account's event destination to the
   stack's WhatsAppInboundTopicArn (socialmessaging PutWhatsAppBusinessAccountEventDestinations).
2. SMS: enable two-way messaging on the EUM SMS phone number, publishing to
   SmsInboundTopicArn (pinpoint-sms-voice-v2 UpdatePhoneNumber).
3. Email: activate the stack's SES receipt rule set -- only if no other rule set is
   active, because an account/region has exactly one active rule set and switching
   it would stop whatever the other set does. --replace-active-rule-set overrides.

    python scripts/connect_inbound.py --stack omnidamus --sms-phone-number-id phone-abc123 [--apply]
"""

from __future__ import annotations

import argparse

import boto3


def stack_outputs(cfn, stack: str) -> dict[str, str]:
    [desc] = cfn.describe_stacks(StackName=stack)["Stacks"]
    return {o["OutputKey"]: o["OutputValue"] for o in desc.get("Outputs", [])}


def pick_waba(social, waba_id: str | None) -> dict:
    accounts = social.list_linked_whatsapp_business_accounts().get("linkedAccounts", [])
    if waba_id:
        accounts = [a for a in accounts if waba_id in (a["id"], a.get("wabaId"))]
    if len(accounts) != 1:
        names = [f"{a['id']} ({a.get('wabaName')})" for a in accounts] or ["none"]
        raise SystemExit(f"Need exactly one linked WABA; found: {', '.join(names)}. Pass --waba-id.")
    return accounts[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stack", required=True)
    parser.add_argument("--region")
    parser.add_argument("--waba-id", help="Linked WABA id (waba-...) or Meta WABA id; required if more than one is linked")
    parser.add_argument("--sms-phone-number-id", help="EUM SMS PhoneNumberId to enable two-way on; omit to skip SMS")
    parser.add_argument("--replace-active-rule-set", action="store_true")
    parser.add_argument("--apply", action="store_true", help="Make the changes (default: print the plan)")
    args = parser.parse_args()

    session = boto3.Session(region_name=args.region)
    outputs = stack_outputs(session.client("cloudformation"), args.stack)
    act = "APPLY" if args.apply else "PLAN "

    # 1. WhatsApp event destination
    social = session.client("socialmessaging")
    waba = pick_waba(social, args.waba_id)
    topic = outputs["WhatsAppInboundTopicArn"]
    print(f"[{act}] WhatsApp: {waba['id']} ({waba.get('wabaName')}) event destination -> {topic}")
    if args.apply:
        social.put_whatsapp_business_account_event_destinations(
            id=waba["id"], eventDestinations=[{"eventDestinationArn": topic}]
        )

    # 2. Two-way SMS
    if args.sms_phone_number_id:
        topic = outputs["SmsInboundTopicArn"]
        print(f"[{act}] SMS: two-way on {args.sms_phone_number_id} -> {topic}")
        if args.apply:
            session.client("pinpoint-sms-voice-v2").update_phone_number(
                PhoneNumberId=args.sms_phone_number_id, TwoWayEnabled=True, TwoWayChannelArn=topic
            )
    else:
        print("[skip ] SMS: no --sms-phone-number-id")

    # 3. SES receipt rule set
    rule_set = outputs.get("InboundRuleSetName")
    if not rule_set:
        print("[skip ] Email: stack deployed without InboundEmailAddress")
        return
    ses = session.client("ses")
    active = ses.describe_active_receipt_rule_set().get("Metadata", {}).get("Name")
    if active == rule_set:
        print(f"[ok   ] Email: {rule_set} already active")
    elif active and not args.replace_active_rule_set:
        print(f"[stop ] Email: rule set {active!r} is active; activating {rule_set!r} would replace it. "
              "Add Omnidamus's rule to it instead, or rerun with --replace-active-rule-set.")
    else:
        print(f"[{act}] Email: activate receipt rule set {rule_set}" + (f" (replacing {active})" if active else ""))
        if args.apply:
            ses.set_active_receipt_rule_set(RuleSetName=rule_set)


if __name__ == "__main__":
    main()
