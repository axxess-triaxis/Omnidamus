from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

TABLE = "omnidamus-conversations-test"


@pytest.fixture
def aws_env(monkeypatch):
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
        "OMNIDAMUS_CONVERSATIONS_TABLE": TABLE,
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


@pytest.fixture
def store(aws_env):
    from omnidamus.state import ConversationStore

    with mock_aws():
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "conversation_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "conversation_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield ConversationStore()
