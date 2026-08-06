"""Bind the S3 mock to the ADR-019 header contract.

Milestone 2 writes a publisher against the preconditions ADR-019 specifies. The
publisher will be tested against `moto`, so the mock is what the acceptance
tests actually measure. That makes mock fidelity load-bearing: a `moto` release
that accepted a stale `If-Match`, or returned 412 where S3 returns 404, would
turn every promotion test green while the contract was broken.

So each test here names one ADR-019 clause and asserts what the mock does with
it. This is the same shape as `test_identity.py`, which recomputes committed
identities from the fixture rather than from the runtime: bind the thing under
trust to the contract, never to itself.

These tests exercise `moto` alone. No repository code is imported, because none
exists yet -- that is the point. When the publisher lands it is tested against
the behavior proven here, and if a dependency bump breaks one of these, the
failure names the clause rather than surfacing as a confusing publisher error.

Two limits are recorded rather than worked around, because reading a passing
suite as broader evidence than it is would be the worse failure: `moto` does
not evaluate a conditional write atomically, so concurrent promotion cannot be
verified here, and it never emits 409, so both indeterminate branches are
tested by injection instead. ADR-019's milestone-2 testing section carries the
measurements.
"""

import sys
import threading
import unittest
from collections import Counter
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from moto import mock_aws

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

BUCKET = "release-bucket"
POINTER = "active-versions.json"
REGION = "us-east-1"


def error_of(raised) -> tuple[int, str]:
    """Return the HTTP status and S3 error code of a raised `ClientError`."""

    response = raised.exception.response
    return response["ResponseMetadata"]["HTTPStatusCode"], response["Error"]["Code"]


class S3PreconditionContractTests(unittest.TestCase):
    """Each test cites the ADR-019 clause it holds `moto` to."""

    def setUp(self):
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.s3 = boto3.client("s3", region_name=REGION)
        self.s3.create_bucket(Bucket=BUCKET)
        # ADR-014 requires a versioned bucket, and several clauses below are
        # meaningful only with versioning on: delete markers, retained pointer
        # versions, and exact-version read-back.
        self.s3.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})

    def put(self, key, body, **conditions):
        return self.s3.put_object(Bucket=BUCKET, Key=key, Body=body, **conditions)

    # --- Immutable object creation ---------------------------------------

    def test_create_with_if_none_match_succeeds_on_an_absent_key(self):
        """ADR-019: write `config.yaml` and `inventory.json` with `If-None-Match: *`."""

        created = self.put("releases/r1/config.yaml", b"one", IfNoneMatch="*")
        self.assertTrue(created["VersionId"])

    def test_create_with_if_none_match_is_refused_when_the_key_exists(self):
        """ADR-019: 412 Precondition Failed means an object already exists at that key."""

        self.put("releases/r1/config.yaml", b"one", IfNoneMatch="*")
        with self.assertRaises(ClientError) as raised:
            self.put("releases/r1/config.yaml", b"two", IfNoneMatch="*")
        self.assertEqual(error_of(raised), (412, "PreconditionFailed"))

    def test_create_with_if_none_match_succeeds_over_a_delete_marker(self):
        """ADR-019: a delete marker makes a key look absent, so a create can succeed
        at a key that still holds noncurrent versions.

        ADR-019 calls this harmless because the pointer pins the version ID the
        publisher read back. It is asserted rather than assumed, because a mock
        that treated noncurrent versions as presence would silently make the
        publisher's create path unreachable.
        """

        key = "releases/r1/config.yaml"
        self.put(key, b"one", IfNoneMatch="*")
        self.s3.delete_object(Bucket=BUCKET, Key=key)

        recreated = self.put(key, b"three", IfNoneMatch="*")
        self.assertTrue(recreated["VersionId"])
        versions = self.s3.list_object_versions(Bucket=BUCKET, Prefix=key)
        self.assertEqual(len(versions["Versions"]), 2)
        self.assertEqual(len(versions["DeleteMarkers"]), 1)

    # --- Active-pointer promotion ----------------------------------------

    def test_promotion_succeeds_against_the_observed_etag(self):
        """ADR-019: write the new pointer with `If-Match` set to the observed ETag.
        200 OK means promoted.
        """

        self.put(POINTER, b'{"release":"r1"}')
        observed = self.s3.head_object(Bucket=BUCKET, Key=POINTER)["ETag"]

        promoted = self.put(POINTER, b'{"release":"r2"}', IfMatch=observed)
        self.assertTrue(promoted["VersionId"])
        self.assertEqual(self.s3.get_object(Bucket=BUCKET, Key=POINTER)["Body"].read(), b'{"release":"r2"}')

    def test_promotion_with_a_stale_etag_is_refused(self):
        """ADR-019: 412 Precondition Failed means another publisher promoted between
        the read and the write.

        This is the clause the whole compare-and-swap rests on.
        """

        self.put(POINTER, b'{"release":"r1"}')
        stale = self.s3.head_object(Bucket=BUCKET, Key=POINTER)["ETag"]
        self.put(POINTER, b'{"release":"r2"}', IfMatch=stale)

        with self.assertRaises(ClientError) as raised:
            self.put(POINTER, b'{"release":"r3"}', IfMatch=stale)
        self.assertEqual(error_of(raised), (412, "PreconditionFailed"))

    def test_if_match_against_a_delete_marker_is_404_not_412(self):
        """ADR-019: 404 Not Found means there is no current version, or the current
        version is a delete marker. `If-Match` returns 404 here rather than 412.

        The distinction drives different publisher behavior: 412 restarts the
        read-decide-write cycle, while 404 against a pointer expected to exist
        stops publication and raises an operational alarm. A mock that
        collapsed them would let the publisher treat a deleted pointer as
        ordinary contention.
        """

        self.put(POINTER, b'{"release":"r1"}')
        observed = self.s3.head_object(Bucket=BUCKET, Key=POINTER)["ETag"]
        self.s3.delete_object(Bucket=BUCKET, Key=POINTER)

        with self.assertRaises(ClientError) as raised:
            self.put(POINTER, b'{"release":"r2"}', IfMatch=observed)
        status, code = error_of(raised)
        self.assertEqual(status, 404)
        self.assertEqual(code, "NoSuchKey")

    def test_if_match_against_a_key_that_never_existed_is_404(self):
        """ADR-019: 404 Not Found means there is no current version."""

        with self.assertRaises(ClientError) as raised:
            self.put("never-written.json", b"x", IfMatch='"abc"')
        self.assertEqual(error_of(raised)[0], 404)

    def test_first_promotion_uses_if_none_match_and_then_refuses(self):
        """ADR-019: a first promotion into a new deployment uses `If-None-Match: *`
        instead; a 412 there means the pointer already exists, so the publisher
        restarts on the `If-Match` path.
        """

        key = "fresh-deployment/active-versions.json"
        self.assertTrue(self.put(key, b'{"release":"r1"}', IfNoneMatch="*")["VersionId"])

        with self.assertRaises(ClientError) as raised:
            self.put(key, b'{"release":"r2"}', IfNoneMatch="*")
        self.assertEqual(error_of(raised), (412, "PreconditionFailed"))

    # --- Rollback --------------------------------------------------------

    def test_a_prior_pointer_version_reads_back_by_id(self):
        """ADR-019: rollback reads the historical version by ID.

        Rollback depends on retained pointer versions staying readable after
        later promotions replaced the current object.
        """

        original = self.put(POINTER, b'{"release":"r1"}')
        observed = self.s3.head_object(Bucket=BUCKET, Key=POINTER)["ETag"]
        self.put(POINTER, b'{"release":"r2"}', IfMatch=observed)

        historical = self.s3.get_object(Bucket=BUCKET, Key=POINTER, VersionId=original["VersionId"])
        self.assertEqual(historical["Body"].read(), b'{"release":"r1"}')

    def test_identical_bytes_reproduce_the_prior_etag(self):
        """ADR-019: identical content can produce an identical ETag on two versions
        of a key.

        This is why rollback writes a fresh `promoted_at` rather than
        republishing historical bytes: without it, a concurrent publisher
        holding the old ETag would find its precondition satisfied against a
        pointer that had moved away and come back. The hazard is asserted here
        so the reason for that rule cannot quietly stop being true.
        """

        first = self.put("etag-probe", b"identical")
        second = self.put("etag-probe", b"identical")
        self.assertEqual(first["ETag"], second["ETag"])
        self.assertNotEqual(first["VersionId"], second["VersionId"])

    # --- Concurrency -----------------------------------------------------

    def test_concurrent_preconditions_are_not_evaluated_atomically(self):
        """The mock's hard limit, and the reason promotion concurrency is not
        verified here.

        ADR-019 named concurrent-publisher interleaving as unproven under the
        mock. It is now measured, and it does not hold: twelve publishers
        released together against one shared ETag produced two winners in 13 of
        60 trials. `moto` evaluates the precondition and writes without holding
        a lock across both, so the compare-and-swap that promotion depends on
        is not enforced.

        This test does not assert the defect, because the defect is
        probabilistic and an upstream fix should not fail the suite. It asserts
        the shape the publisher must therefore assume: a single-threaded
        sequence still behaves correctly, so every other test in this file is
        sound, and nothing here may be read as evidence that concurrent
        promotion is safe. Real serialization is an S3 property this mock does
        not reproduce; the publisher's 412 handling is tested by injecting the
        error at its S3 seam.
        """

        self.put(POINTER, b'{"release":"r0"}')
        observed = self.s3.head_object(Bucket=BUCKET, Key=POINTER)["ETag"]

        self.put(POINTER, b'{"release":"r1"}', IfMatch=observed)
        with self.assertRaises(ClientError) as raised:
            self.put(POINTER, b'{"release":"r2"}', IfMatch=observed)
        self.assertEqual(error_of(raised), (412, "PreconditionFailed"))

    def test_conflict_is_not_reachable_under_the_mock(self):
        """The boundary of what this mock can prove.

        ADR-019 gives 409 its own handling on both paths: a bounded retry when
        a delete races a create, and convergence-without-attribution when a
        promotion outcome is indeterminate. Deliberate create/delete races do
        not produce one under `moto`, so those branches are tested by injecting
        the error at the publisher's S3 seam instead of by provoking a race.

        Only the absence of 409 is asserted. Which of the two ordinary outcomes
        each race lands on is timing-dependent, so asserting that both occur
        would make the suite flaky for no added guarantee. If a future `moto`
        does emit 409 here, this fails, and ADR-019's milestone-2 testing
        section should be revisited: a reproducible race would be worth
        asserting directly.
        """

        key = "races/config.yaml"
        seen: Counter = Counter()
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def create():
            client = boto3.client("s3", region_name=REGION)
            barrier.wait()
            try:
                client.put_object(Bucket=BUCKET, Key=key, Body=b"payload", IfNoneMatch="*")
                result = "success"
            except ClientError as error:
                result = error.response["Error"]["Code"]
            with lock:
                seen[result] += 1

        def delete():
            client = boto3.client("s3", region_name=REGION)
            barrier.wait()
            client.delete_object(Bucket=BUCKET, Key=key)

        for _ in range(50):
            barrier.reset()
            threads = [threading.Thread(target=create), threading.Thread(target=delete)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertNotIn("ConditionalRequestConflict", seen)
        self.assertEqual(sum(seen.values()), 50)


if __name__ == "__main__":
    unittest.main()
