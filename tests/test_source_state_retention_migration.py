from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import yaml
from moto import mock_aws

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "migrate_source_state_retention", ROOT / "scripts/migrate_source_state_retention.py"
)
assert SPEC is not None
migration = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = migration
SPEC.loader.exec_module(migration)

from aws_public_change_feed.identity import release_id  # noqa: E402

REGION = "us-east-1"
TABLE = "source-state"
AS_OF = datetime(2026, 8, 30, 2, 0, tzinfo=UTC)
OLD = datetime(2023, 1, 1, tzinfo=UTC)
RECENT = AS_OF - timedelta(days=10)
ANNOUNCEMENT_A = "a" * 64
ANNOUNCEMENT_B = "b" * 64
ANNOUNCEMENT_C = "c" * 64
RUN_ID = "d" * 64
PAGE_SET_ID = "e" * 64
CANDIDATE_ID = "f" * 64


class FakeError(Exception):
    def __init__(self, code: str = "Failure") -> None:
        super().__init__("provider detail must not reach operator output")
        self.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": 400}}


def create_table(client):
    client.create_table(
        TableName=TABLE,
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
    )


def put(client, document):
    client.put_item(TableName=TABLE, Item={name: migration._wire(value) for name, value in document.items()})


def announcement(identifier, observed, *, state_version=1, expires_at=None):
    item = {
        "PK": f"ANNOUNCEMENT#{identifier}",
        "SK": "STATE",
        "item_type": "announcement",
        "last_observed_at": observed.isoformat(),
        "state_version": state_version,
    }
    if expires_at is not None:
        item["expires_at"] = expires_at
    return item


def response_page(*, expires_at=None):
    item = {
        "PK": f"RUN#{RUN_ID}",
        "SK": f"PAGESET#{PAGE_SET_ID}#PAGE#000000",
        "item_type": "response_page",
        "run_id": RUN_ID,
        "page_set_id": PAGE_SET_ID,
        "feed_name": "aws-news",
        "page": 0,
        "candidate_ids": [CANDIDATE_ID],
        "complete": True,
    }
    if expires_at is not None:
        item["expires_at"] = expires_at
    return item


def context():
    return migration.MigrationContext(
        account_id="123456789012",
        region=REGION,
        table_name=TABLE,
        role_arn="arn:aws:iam::123456789012:role/apcf-dev-source-state-retention-migration",
        role_session_arn=("arn:aws:sts::123456789012:assumed-role/apcf-dev-source-state-retention-migration/test"),
        bucket="apcf-config-dev",
        pointer_key="apcf/active-versions.json",
        pointer_version_id="pointer-version",
        pointer_etag='"pointer-etag"',
        release_id="1" * 64,
        application_version=f"sha256:{'2' * 64}",
        config_reference={
            "key": f"apcf/releases/{'1' * 64}/config.yaml",
            "version_id": "config-version",
            "sha256": "3" * 64,
            "schema_version": 4,
        },
        announcement_ttl_days=730,
        feed_state_ttl_days=730,
        deployment_sha256="4" * 64,
        terraform_output_sha256="5" * 64,
    )


class DelegatingClient:
    def __init__(self, client):
        self.client = client

    def scan(self, **kwargs):
        return self.client.scan(**kwargs)

    def get_item(self, **kwargs):
        return self.client.get_item(**kwargs)

    def update_item(self, **kwargs):
        return self.client.update_item(**kwargs)


class ConflictOnFirstWrite(DelegatingClient):
    def __init__(self, client):
        super().__init__(client)
        self.raced = False

    def update_item(self, **kwargs):
        if not self.raced:
            self.raced = True
            self.client.update_item(
                TableName=kwargs["TableName"],
                Key=kwargs["Key"],
                UpdateExpression="SET #version = :version",
                ExpressionAttributeNames={"#version": "state_version"},
                ExpressionAttributeValues={":version": {"N": "99"}},
            )
        return self.client.update_item(**kwargs)


class PageConflictOnFirstWrite(DelegatingClient):
    def __init__(self, client):
        super().__init__(client)
        self.raced = False

    def update_item(self, **kwargs):
        if not self.raced:
            self.raced = True
            self.client.update_item(
                TableName=kwargs["TableName"],
                Key=kwargs["Key"],
                UpdateExpression="SET #candidates = :candidates",
                ExpressionAttributeNames={"#candidates": "candidate_ids"},
                ExpressionAttributeValues={":candidates": {"L": [{"S": "0" * 64}]}},
            )
        return self.client.update_item(**kwargs)


class FailSecondWrite(DelegatingClient):
    def __init__(self, client):
        super().__init__(client)
        self.calls = 0

    def update_item(self, **kwargs):
        self.calls += 1
        if self.calls == 2:
            raise FakeError()
        return self.client.update_item(**kwargs)


class AmbiguousFirstWrite(DelegatingClient):
    def __init__(self, client):
        super().__init__(client)
        self.wrote = False

    def update_item(self, **kwargs):
        self.client.update_item(**kwargs)
        self.wrote = True
        raise FakeError()

    def get_item(self, **kwargs):
        if self.wrote:
            raise FakeError()
        return self.client.get_item(**kwargs)


class CountingWrites(DelegatingClient):
    def __init__(self, client):
        super().__init__(client)
        self.calls = 0

    def update_item(self, **kwargs):
        self.calls += 1
        return self.client.update_item(**kwargs)


class FakeSTS:
    def get_caller_identity(self):
        return {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/operator"}

    def assume_role(self, **kwargs):
        self.assume_arguments = kwargs
        return {
            "Credentials": {
                "AccessKeyId": "access",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
            },
            "AssumedRoleUser": {
                "Arn": (
                    "arn:aws:sts::123456789012:assumed-role/"
                    "apcf-dev-source-state-retention-migration/apcf-source-state-retention-migration"
                )
            },
        }


class SourceStateRetentionMigrationTests(unittest.TestCase):
    def setUp(self):
        self.aws = mock_aws()
        self.aws.start()
        self.client = boto3.client("dynamodb", region_name=REGION)
        create_table(self.client)

    def tearDown(self):
        self.aws.stop()

    def seed_mixed_inventory(self):
        put(self.client, announcement(ANNOUNCEMENT_A, OLD, state_version=7))
        put(self.client, announcement(ANNOUNCEMENT_B, RECENT, state_version=3))
        put(
            self.client,
            announcement(
                ANNOUNCEMENT_C, RECENT, state_version=2, expires_at=int((AS_OF + timedelta(days=1)).timestamp())
            ),
        )
        put(self.client, response_page())
        put(
            self.client,
            {
                "PK": "FEED#aws-news",
                "SK": "STATE",
                "item_type": "feed",
                "feed_name": "aws-news",
            },
        )

    def preview(self, client=None):
        return migration.create_plan(
            client or self.client,
            context=context(),
            inventory_limit=100,
            migration_as_of=AS_OF,
        )

    def test_preview_is_projected_read_only_and_records_every_eligible_announcement(self):
        self.seed_mixed_inventory()
        plan = self.preview()
        self.assertEqual(plan["inventory_scanned_count"], 5)
        self.assertEqual(
            plan["counts"],
            {
                "announcements": 3,
                "response_pages": 1,
                "legacy_announcements": 2,
                "legacy_response_pages": 1,
                "already_eligible_announcements": 1,
            },
        )
        self.assertEqual(
            plan["already_eligible_announcements"],
            [
                {
                    "PK": f"ANNOUNCEMENT#{ANNOUNCEMENT_A}",
                    "SK": "STATE",
                    "target_expires_at": int((OLD + timedelta(days=730)).timestamp()),
                }
            ],
        )
        page = next(row for row in plan["updates"] if row["kind"] == "response_page")
        self.assertEqual(page["target_expires_at"], int((AS_OF + timedelta(days=730)).timestamp()))
        raw = self.client.get_item(
            TableName=TABLE,
            Key={"PK": {"S": f"ANNOUNCEMENT#{ANNOUNCEMENT_A}"}, "SK": {"S": "STATE"}},
        )["Item"]
        self.assertNotIn("expires_at", raw)

    def test_apply_updates_both_classes_and_increments_announcement_state_versions(self):
        self.seed_mixed_inventory()
        plan = self.preview()
        result = migration.apply_plan(
            self.client,
            context=context(),
            plan=plan,
            plan_sha256="6" * 64,
        )
        self.assertEqual(result["status"], "applied")
        self.assertEqual(len(result["completed"]), 3)
        self.assertEqual(result["ttl_eligible_at_migration_count"], 1)
        self.assertEqual(result["post_apply"]["expired_items_still_present"], 1)
        old = self.client.get_item(
            TableName=TABLE,
            Key={"PK": {"S": f"ANNOUNCEMENT#{ANNOUNCEMENT_A}"}, "SK": {"S": "STATE"}},
            ConsistentRead=True,
        )["Item"]
        self.assertEqual(old["state_version"], {"N": "8"})
        self.assertEqual(old["expires_at"], {"N": str(int((OLD + timedelta(days=730)).timestamp()))})
        page = self.client.get_item(
            TableName=TABLE,
            Key={"PK": {"S": f"RUN#{RUN_ID}"}, "SK": {"S": f"PAGESET#{PAGE_SET_ID}#PAGE#000000"}},
            ConsistentRead=True,
        )["Item"]
        self.assertEqual(page["expires_at"], {"N": str(int((AS_OF + timedelta(days=730)).timestamp()))})
        feed = self.client.get_item(
            TableName=TABLE,
            Key={"PK": {"S": "FEED#aws-news"}, "SK": {"S": "STATE"}},
        )["Item"]
        self.assertNotIn("expires_at", feed)

    def test_inventory_limit_refuses_before_a_plan(self):
        self.seed_mixed_inventory()
        with self.assertRaisesRegex(migration.MigrationError, "inventory"):
            migration.create_plan(
                self.client,
                context=context(),
                inventory_limit=2,
                migration_as_of=AS_OF,
            )

    def test_conflict_inversion_stops_before_writing_the_raced_record(self):
        put(self.client, announcement(ANNOUNCEMENT_A, RECENT, state_version=7))
        plan = self.preview()
        control = copy.deepcopy(plan)
        control_result = migration.apply_plan(
            self.client,
            context=context(),
            plan=control,
            plan_sha256="7" * 64,
        )
        self.assertEqual(control_result["status"], "applied")

        self.client.delete_item(
            TableName=TABLE,
            Key={"PK": {"S": f"ANNOUNCEMENT#{ANNOUNCEMENT_A}"}, "SK": {"S": "STATE"}},
        )
        put(self.client, announcement(ANNOUNCEMENT_A, RECENT, state_version=7))
        raced = ConflictOnFirstWrite(self.client)
        result = migration.apply_plan(raced, context=context(), plan=plan, plan_sha256="7" * 64)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["completed"], [])
        self.assertEqual(len(result["untouched"]), 1)
        durable = self.client.get_item(
            TableName=TABLE,
            Key={"PK": {"S": f"ANNOUNCEMENT#{ANNOUNCEMENT_A}"}, "SK": {"S": "STATE"}},
        )["Item"]
        self.assertEqual(durable["state_version"], {"N": "99"})
        self.assertNotIn("expires_at", durable)

    def test_page_proof_conflict_inversion_leaves_expiry_absent(self):
        put(self.client, response_page())
        plan = self.preview()
        result = migration.apply_plan(
            PageConflictOnFirstWrite(self.client),
            context=context(),
            plan=plan,
            plan_sha256="a" * 64,
        )
        self.assertEqual(result["status"], "conflict")
        durable = self.client.get_item(
            TableName=TABLE,
            Key={"PK": {"S": f"RUN#{RUN_ID}"}, "SK": {"S": f"PAGESET#{PAGE_SET_ID}#PAGE#000000"}},
        )["Item"]
        self.assertEqual(durable["candidate_ids"], {"L": [{"S": "0" * 64}]})
        self.assertNotIn("expires_at", durable)

    def test_partial_result_separates_completed_and_untouched_updates(self):
        put(self.client, announcement(ANNOUNCEMENT_A, RECENT))
        put(self.client, announcement(ANNOUNCEMENT_B, RECENT))
        plan = self.preview()
        result = migration.apply_plan(
            FailSecondWrite(self.client),
            context=context(),
            plan=plan,
            plan_sha256="8" * 64,
        )
        self.assertEqual(result["status"], "write_failed")
        self.assertEqual(len(result["completed"]), 1)
        self.assertEqual(len(result["untouched"]), 1)
        self.assertEqual(result["stopped_at"]["PK"], f"ANNOUNCEMENT#{ANNOUNCEMENT_B}")

    def test_unreadable_strong_reread_reports_ambiguous_without_retry(self):
        put(self.client, announcement(ANNOUNCEMENT_A, RECENT))
        plan = self.preview()
        wrapped = AmbiguousFirstWrite(self.client)
        result = migration.apply_plan(wrapped, context=context(), plan=plan, plan_sha256="9" * 64)
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["completed"], [])
        self.assertEqual(len(result["untouched"]), 1)
        self.assertTrue(wrapped.wrote)

    def test_changed_active_pointer_refuses_after_inventory_and_before_first_write(self):
        put(self.client, announcement(ANNOUNCEMENT_A, RECENT))
        plan = self.preview()
        wrapped = CountingWrites(self.client)

        def stale_pointer():
            raise migration.MigrationError("stale_plan", "active release pointer differs from the preview")

        with self.assertRaisesRegex(migration.MigrationError, "pointer differs"):
            migration.apply_plan(
                wrapped,
                context=context(),
                plan=plan,
                plan_sha256="0" * 64,
                context_verifier=stale_pointer,
            )
        self.assertEqual(wrapped.calls, 0)

    def test_canonical_plan_digest_rejects_any_byte_change(self):
        put(self.client, announcement(ANNOUNCEMENT_A, RECENT))
        plan = self.preview()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            digest = migration.write_plan(path, plan)
            self.assertEqual(migration.load_plan(path, digest), plan)
            path.write_bytes(path.read_bytes() + b" ")
            with self.assertRaisesRegex(migration.MigrationError, "differs"):
                migration.load_plan(path, digest)

    def test_load_context_binds_the_exact_active_release_and_assumed_role(self):
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket="apcf-config-dev")
        s3.put_bucket_versioning(
            Bucket="apcf-config-dev",
            VersioningConfiguration={"Status": "Enabled"},
        )
        config_body = (ROOT / "examples/config.yaml").read_bytes()
        inventory_body = (ROOT / "examples/inventory.json").read_bytes()
        config_sha = hashlib.sha256(config_body).hexdigest()
        inventory_sha = hashlib.sha256(inventory_body).hexdigest()
        identifier = release_id(config_sha, inventory_sha)
        config_key = f"apcf/releases/{identifier}/config.yaml"
        inventory_key = f"apcf/releases/{identifier}/inventory.json"
        config_version = s3.put_object(Bucket="apcf-config-dev", Key=config_key, Body=config_body)["VersionId"]
        inventory_version = s3.put_object(Bucket="apcf-config-dev", Key=inventory_key, Body=inventory_body)["VersionId"]
        pointer = {
            "schema_version": 2,
            "release_id": identifier,
            "promoted_at": "2026-08-29T00:00:00Z",
            "config": {
                "key": config_key,
                "version_id": config_version,
                "sha256": config_sha,
                "schema_version": 4,
            },
            "inventory": {
                "key": inventory_key,
                "version_id": inventory_version,
                "sha256": inventory_sha,
                "schema_version": 3,
            },
        }
        s3.put_object(
            Bucket="apcf-config-dev",
            Key="apcf/active-versions.json",
            Body=json.dumps(pointer).encode("utf-8"),
        )
        role_arn = "arn:aws:iam::123456789012:role/apcf-dev-source-state-retention-migration"
        outputs = {
            "config_bucket_name": {"sensitive": False, "type": "string", "value": "apcf-config-dev"},
            "source_state_table": {"sensitive": False, "type": "string", "value": "apcf-source-state-dev"},
            "watcher_application_version": {
                "sensitive": False,
                "type": "string",
                "value": f"sha256:{'2' * 64}",
            },
            "roles": {
                "sensitive": False,
                "type": ["object", {}],
                "value": {"source_state_retention_migration": role_arn},
            },
        }
        deployment = yaml.safe_load((ROOT / "infra/central/deployment.yaml").read_bytes())
        with tempfile.TemporaryDirectory() as directory:
            deployment_path = Path(directory) / "deployment.yaml"
            outputs_path = Path(directory) / "outputs.json"
            deployment_path.write_text(yaml.safe_dump(deployment), encoding="utf-8")
            outputs_path.write_text(json.dumps(outputs), encoding="utf-8")
            captured = {}

            def factory(**kwargs):
                captured.update(kwargs)
                return self.client

            loaded, ddb = migration.load_context(
                deployment_path=deployment_path,
                terraform_output_path=outputs_path,
                expected_account="123456789012",
                sts_client=FakeSTS(),
                s3_client=s3,
                ddb_client_factory=factory,
            )
        self.assertIs(ddb, self.client)
        self.assertEqual(loaded.release_id, identifier)
        self.assertEqual(loaded.config_reference, pointer["config"])
        self.assertEqual(loaded.announcement_ttl_days, 730)
        self.assertEqual(loaded.feed_state_ttl_days, 730)
        self.assertEqual(captured["region_name"], REGION)
        self.assertNotIn("SecretAccessKey", loaded.plan_document())


if __name__ == "__main__":
    unittest.main()
