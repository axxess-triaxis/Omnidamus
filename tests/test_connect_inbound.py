"""scripts/connect_inbound.py against mocked AWS clients: dry run changes nothing,
--apply makes exactly the documented calls, and an active SES rule set that isn't
ours is never replaced without --replace-active-rule-set."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from unittest.mock import MagicMock, patch

import pytest

_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "connect_inbound.py"
_spec = importlib.util.spec_from_file_location("connect_inbound", _PATH)
connect_inbound = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(connect_inbound)

OUTPUTS = {
    "WhatsAppInboundTopicArn": "arn:aws:sns:us-east-1:123456789012:wa",
    "SmsInboundTopicArn": "arn:aws:sns:us-east-1:123456789012:sms",
    "InboundRuleSetName": "omnidamus-rules",
}


def _clients(active_rule_set=None, wabas=None):
    cfn, social, sms, ses = MagicMock(), MagicMock(), MagicMock(), MagicMock()
    cfn.describe_stacks.return_value = {"Stacks": [{"Outputs": [
        {"OutputKey": k, "OutputValue": v} for k, v in OUTPUTS.items()]}]}
    social.list_linked_whatsapp_business_accounts.return_value = {"linkedAccounts": wabas if wabas is not None else [
        {"id": "waba-1", "wabaId": "1234567890", "wabaName": "Triaxis test", "arn": "x"}]}
    ses.describe_active_receipt_rule_set.return_value = (
        {"Metadata": {"Name": active_rule_set}} if active_rule_set else {})
    by_name = {"cloudformation": cfn, "socialmessaging": social,
               "pinpoint-sms-voice-v2": sms, "ses": ses}
    session = MagicMock()
    session.client.side_effect = lambda name: by_name[name]
    return session, social, sms, ses


def _run(argv, session):
    with patch.object(sys, "argv", ["connect_inbound.py", *argv]), \
         patch.object(connect_inbound.boto3, "Session", return_value=session):
        connect_inbound.main()


def test_dry_run_changes_nothing(capsys):
    session, social, sms, ses = _clients()
    _run(["--stack", "s", "--sms-phone-number-id", "phone-1"], session)
    social.put_whatsapp_business_account_event_destinations.assert_not_called()
    sms.update_phone_number.assert_not_called()
    ses.set_active_receipt_rule_set.assert_not_called()
    assert "[PLAN ]" in capsys.readouterr().out


def test_apply_wires_all_three_channels():
    session, social, sms, ses = _clients()
    _run(["--stack", "s", "--sms-phone-number-id", "phone-1", "--apply"], session)
    social.put_whatsapp_business_account_event_destinations.assert_called_once_with(
        id="waba-1", eventDestinations=[{"eventDestinationArn": OUTPUTS["WhatsAppInboundTopicArn"]}])
    sms.update_phone_number.assert_called_once_with(
        PhoneNumberId="phone-1", TwoWayEnabled=True, TwoWayChannelArn=OUTPUTS["SmsInboundTopicArn"])
    ses.set_active_receipt_rule_set.assert_called_once_with(RuleSetName="omnidamus-rules")


def test_other_active_rule_set_is_not_replaced(capsys):
    session, _, _, ses = _clients(active_rule_set="someone-elses-rules")
    _run(["--stack", "s", "--apply"], session)
    ses.set_active_receipt_rule_set.assert_not_called()
    assert "[stop ]" in capsys.readouterr().out


def test_replace_flag_allows_switching_rule_set():
    session, _, _, ses = _clients(active_rule_set="someone-elses-rules")
    _run(["--stack", "s", "--apply", "--replace-active-rule-set"], session)
    ses.set_active_receipt_rule_set.assert_called_once_with(RuleSetName="omnidamus-rules")


def test_ambiguous_waba_stops():
    session, social, _, _ = _clients(wabas=[{"id": "waba-1"}, {"id": "waba-2"}])
    with pytest.raises(SystemExit):
        _run(["--stack", "s", "--apply"], session)
    social.put_whatsapp_business_account_event_destinations.assert_not_called()
