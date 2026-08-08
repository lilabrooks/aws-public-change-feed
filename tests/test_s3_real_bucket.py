"""ADR-019 revision: the opt-in concurrent-promotion suite against a real bucket.

ADR-019 accepted that moto does not evaluate a conditional write atomically --
twelve publishers released together against one shared ETag produced two
winners in 13 of 60 trials -- so compare-and-swap promotion cannot be verified
under the mock. Its 2026-08-07 revision adds a suite that runs the same
assertions against a dedicated real bucket, which S3's atomic conditional
write makes deterministic: N publishers holding one observed ETag produce
exactly one 200 and N-1 412 on every run.

This file is operator-run evidence, not a CI gate. It is skipped unless
`APCF_CONCURRENCY_BUCKET` names a real bucket, so `make check` stays
credential-free and unchanged. The bucket, its lifecycle rules, and the scoped
identity are provisioned by `infra/bootstrap` (`apcf-concurrency-<deployment_id>`
and the `apcf_concurrency_test` user). Access keys for that user are created
with `aws iam create-access-key` at run time and are never committed.

A fresh access key can take tens of seconds to propagate across the S3
auth backend, and an immediately-used key has returned a transient
`InvalidAccessKeyId` on a single concurrent call while eleven others with the
same key succeeded. Wait roughly thirty seconds after creating the keys before
starting the suite.

Run it with the scoped identity's credentials:

    APCF_CONCURRENCY_BUCKET=apcf-concurrency-dev \
      python -m unittest tests.test_s3_real_bucket

Every test writes under a unique `concurrency/<run-id>/` key prefix, so runs
do not collide and the bucket lifecycle expires the leftovers. A passing run
is evidence for ADR-019's revision and for milestone 2's "concurrent
publishers" verification item; the 409 branch remains unverified against any
backend, which is a property of the outcome rather than a gap in the suite.
"""

from __future__ import annotations

import itertools
import json
import os
import sys
import threading
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aws_public_change_feed.candidates import utc_timestamp  # noqa: E402
from aws_public_change_feed.releases import (  # noqa: E402
    ObjectMissing,
    PreconditionFailed,
    PromotionSuperseded,
    S3ObjectStore,
    promote_pointer,
)

# `cast` because `unittest.skipUnless` does not narrow types for mypy: the suite
# is skipped when the variable is unset, and only runs with a real bucket name.
BUCKET = cast(str, os.environ.get("APCF_CONCURRENCY_BUCKET"))
REGION = "us-east-1"
RUN_ID = uuid.uuid4().hex
_T0 = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)

_skip = "set APCF_CONCURRENCY_BUCKET to a real bucket to run the ADR-019 real-bucket suite"


@unittest.skipUnless(BUCKET, _skip)
class RealBucketContractTests(unittest.TestCase):
    """The ADR-019 clauses a single request can express, driven through the
    publisher's real `S3ObjectStore`.

    `tests/test_s3_preconditions.py` binds `moto` to the same contract. Running
    these again against S3 surfaces any divergence between the two backends as
    a named failure, which is the ADR revision's reason for holding both to one
    set of assertions.
    """

    def setUp(self):
        self.client = boto3.client("s3", region_name=REGION)
        self.store = S3ObjectStore(self.client, BUCKET)
        self._keys = itertools.count()
        self.prefix = f"concurrency/{RUN_ID}/"

    def key(self, name: str) -> str:
        return f"{self.prefix}{next(self._keys)}-{name}"

    def test_create_with_if_none_match_succeeds_on_an_absent_key(self):
        version = self.store.create(self.key("absent"), b"one")
        self.assertTrue(version)

    def test_create_is_refused_when_the_key_exists(self):
        key = self.key("existing")
        self.store.create(key, b"one")
        with self.assertRaises(PreconditionFailed):
            self.store.create(key, b"two")

    def test_create_succeeds_over_a_delete_marker(self):
        key = self.key("deleted")
        self.store.create(key, b"one")
        self.client.delete_object(Bucket=BUCKET, Key=key)
        version = self.store.create(key, b"three")
        self.assertTrue(version)
        versions = self.client.list_object_versions(Bucket=BUCKET, Prefix=key)
        self.assertEqual(len(versions["Versions"]), 2)
        self.assertEqual(len(versions["DeleteMarkers"]), 1)

    def test_promotion_succeeds_against_the_observed_etag(self):
        key = self.key("promote")
        self.store.create(key, b'{"release":"r1"}')
        observed = self.store.read(key)
        promoted = self.store.replace(key, b'{"release":"r2"}', if_match=observed.etag)
        self.assertTrue(promoted)
        self.assertEqual(self.store.read(key).body, b'{"release":"r2"}')

    def test_promotion_with_a_stale_etag_is_refused(self):
        key = self.key("stale")
        self.store.create(key, b'{"release":"r1"}')
        stale = self.store.read(key).etag
        self.store.replace(key, b'{"release":"r2"}', if_match=stale)
        with self.assertRaises(PreconditionFailed):
            self.store.replace(key, b'{"release":"r3"}', if_match=stale)

    def test_if_match_against_a_delete_marker_is_object_missing_not_precondition(self):
        key = self.key("deleted-pointer")
        self.store.create(key, b'{"release":"r1"}')
        observed = self.store.read(key)
        self.client.delete_object(Bucket=BUCKET, Key=key)
        with self.assertRaises(ObjectMissing):
            self.store.replace(key, b'{"release":"r2"}', if_match=observed.etag)

    def test_if_match_against_a_key_that_never_existed_is_object_missing(self):
        with self.assertRaises(ObjectMissing):
            self.store.replace(self.key("never"), b"x", if_match='"abc"')

    def test_first_promotion_uses_if_none_match_and_then_refuses(self):
        key = self.key("first-promotion")
        self.assertTrue(self.store.create(key, b'{"release":"r1"}'))
        with self.assertRaises(PreconditionFailed):
            self.store.create(key, b'{"release":"r2"}')

    def test_a_prior_pointer_version_reads_back_by_id(self):
        key = self.key("rollback")
        original = self.store.create(key, b'{"release":"r1"}')
        observed = self.store.read(key)
        self.store.replace(key, b'{"release":"r2"}', if_match=observed.etag)
        historical = self.store.read(key, original)
        self.assertEqual(historical.body, b'{"release":"r1"}')

    def test_identical_bytes_reproduce_the_prior_etag(self):
        key = self.key("etag")
        first_version = self.store.create(key, b"identical")
        first_etag = self.store.read(key).etag
        second_version = self.store.replace(key, b"identical", if_match=first_etag)
        second_etag = self.store.read(key).etag
        self.assertEqual(first_etag, second_etag)
        self.assertNotEqual(first_version, second_version)


@unittest.skipUnless(BUCKET, _skip)
class RealBucketConcurrencyTests(unittest.TestCase):
    """The suite's one headline assertion.

    N publishers read one pointer, capture the observed ETag from that same
    read, are released together, and each promotes its own release through the
    real `promote_pointer` path. S3 evaluates the conditional write atomically,
    so exactly one promotion succeeds and the rest are `PromotionSuperseded` --
    deterministically, unlike under `moto`.
    """

    PUBLISHERS = 12

    def setUp(self):
        self.prefix = f"concurrency/{RUN_ID}/"
        self.client = boto3.client("s3", region_name=REGION)

    def store_for(self) -> S3ObjectStore:
        return S3ObjectStore(boto3.client("s3", region_name=REGION), BUCKET)

    def test_concurrent_promotion_yields_exactly_one_winner(self):
        pointer = f"{self.prefix}concurrent-pointer"
        store = self.store_for()
        store.create(pointer, json.dumps({"release_id": "0" * 64, "promoted_at": utc_timestamp(_T0)}).encode())
        observed = store.read(pointer)

        barrier = threading.Barrier(self.PUBLISHERS)
        outcomes: list[tuple[str, str | None]] = []
        lock = threading.Lock()

        def promote(index: int) -> None:
            release = f"{index:064d}"
            promoted_at = _T0 + timedelta(seconds=index + 1)
            document = json.dumps({"release_id": release, "promoted_at": utc_timestamp(promoted_at)}).encode()
            barrier.wait()
            try:
                result = promote_pointer(
                    self.store_for(),
                    pointer_key=pointer,
                    document=document,
                    observed=observed,
                )
                outcome: tuple[str, str | None] = ("promoted", result.release_id)
            except PromotionSuperseded as error:
                outcome = ("superseded", error.promoting)
            with lock:
                outcomes.append(outcome)

        threads = [threading.Thread(target=promote, args=(index,)) for index in range(self.PUBLISHERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        promoted = [release for kind, release in outcomes if kind == "promoted"]
        superseded = [release for kind, release in outcomes if kind == "superseded"]
        self.assertEqual(len(promoted), 1, outcomes)
        self.assertEqual(len(superseded), self.PUBLISHERS - 1)
        self.assertEqual(len(outcomes), self.PUBLISHERS)
        self.assertEqual(json.loads(store.read(pointer).body)["release_id"], promoted[0])


if __name__ == "__main__":
    unittest.main()
