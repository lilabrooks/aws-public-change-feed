# ADR-029: Lambda package provenance and entrypoint proof

- Status: Accepted
- Date: 2026-09-06
- Accepted: 2026-09-06
- Owner: Lila Brooks
- Relates to: [ADR-006](006-terraform-and-python-implementation-baseline.md), [ADR-020](020-exact-application-version-gate-for-delivery.md), [ADR-026](026-central-shadow-and-rollback-proof.md)

## Context

L-42 first reached the deployed shadow Lambda with a package that predated
`shadow_runtime.py`. The later package repair checked the five configured module
paths, yet the publisher still accepts empty modules, invalid Python, absent or
non-callable handler symbols, and unsafe ZIP members. It records a digest and
handler-list digest in S3 metadata. Terraform compares those publisher-written
values with its inputs, so it never checks an S3-computed body digest.

Package review also lacks a durable source statement. The build copies the
working source tree and installs the dependency lock, but the resulting archive
does not identify those inputs. A package can therefore contain untracked or
stale source while retaining valid module names.

Several tempting provenance fields would damage the existing identity model.
Putting a commit, branch, timestamp, host, or observed tool version inside the
archive changes its SHA-256 when the runtime payload has not changed. That
would make documentation-only commits or builder-host differences appear to be
new application versions and could manufacture a false successor for L-54.
ADR-020 remains authoritative: `application_version` is the SHA-256 of the
exact deployable package bytes.

## Decision drivers

- Catch source drift, unsafe archives, and invalid handler declarations before
  the first S3 write.
- Test importability and handler shape on the package's Linux and Python target.
- Preserve byte-identical output across documentation-only commits and avoid
  provenance fields that force otherwise-identical builds to differ.
- Distinguish facts derived from package bytes, facts calculated by S3, and
  statements made by the publisher.
- Keep the repair small enough to complete before L-53 and L-54.
- Preserve the one known five-handler rollback package until L-53 decides its
  compatibility treatment.

## Decision

### Stable in-archive manifest

Every new Lambda package carries one canonical JSON manifest at
`aws-public-change-feed-manifest.json`. Its closed field set contains:

- a manifest contract version;
- a source-tree SHA-256 derived from the packaged
  `aws_public_change_feed/**` members;
- the SHA-256 of `requirements-lambda.txt`;
- the SHA-256 of `scripts/build_lambda_package.py`;
- the sorted configured `module.function` handler list and its existing
  null-framed SHA-256;
- the target Python version, platform tag, implementation, and architecture.

The source-tree digest sorts archive-relative POSIX paths by their UTF-8 bytes.
For each path it hashes an unsigned 64-bit big-endian path length, the path
bytes, an unsigned 64-bit big-endian content length, and the content bytes.
The manifest uses UTF-8 JSON with sorted keys, compact separators,
`ensure_ascii=False`, and no trailing newline. Unknown fields are refused. The
manifest never contains the package ZIP digest because that would be recursive.

The manifest also excludes the Git commit and tree, branch, dirty status,
timestamp, host identity, and observed Python, pip, or zlib versions. Those
values can change without changing package-producing content. Publication
records them as external build attestations in the S3 metadata that fits the
service limit and in its local result. The stored S3 attestation remains the
one written by the first publisher when later publication adopts identical
bytes.

The lock and builder digests state which reviewed input files the builder
declared. The publisher proves that those files match `HEAD` at publication
time. This does not prove that an untrusted builder executed those inputs.

### Publication checks

Before its first S3 write, publication performs all of these checks:

1. Parse the manifest and recompute every byte-derived field.
2. Require exact set and byte equality between packaged
   `aws_public_change_feed/**` members and the tracked `HEAD` blobs under
   `src/aws_public_change_feed/`.
3. Require the lock and builder bytes to equal their tracked `HEAD` blobs and
   reject relevant staged, unstaged, or untracked input changes.
4. Reject duplicate members, absolute paths, traversal, backslash-separated
   paths, symlinks, and non-regular members anywhere in the ZIP. Reject any
   unexpected member under `aws_public_change_feed/` and require the manifest
   at its one fixed root path.
5. Parse every configured source module as Python 3.12 and require a supported
   synchronous two-argument handler declaration with no later top-level
   rebinding.

The last check proves a restricted source declaration. It does not prove that
module import completes or that all dependencies are present.

Publication supplies `ChecksumAlgorithm="SHA256"` and the locally computed
base64 checksum to S3. S3 rejects a mismatch. The publisher then reads the
exact returned VersionId and verifies its body, stored SHA-256 checksum,
byte-derived metadata, and manifest. Adoption compares byte-derived values;
it does not compare commit, host, timestamp, or observed-tool attestations.

### Target import gate

The repository quality workflow builds the real dependency-bearing package on
Ubuntu 24.04 x86_64 with Python 3.12. It extracts the archive to a fresh
directory, imports all five configured modules from that directory, and checks
that each resolved handler is callable, synchronous, and accepts the two
Lambda arguments.

This gate covers import-time failures and dependency omissions on the declared
manylinux target. It does not claim parity with the managed Lambda image. A
Lambda-image probe becomes justified when a defect escapes this gate or an AWS
runtime-specific dependency enters the package.

### Terraform and the legacy package

The AWS provider data source reads the selected exact VersionId with
`checksum_mode = "ENABLED"`. For a checksum-bearing package, Terraform compares
the S3-computed `checksum_sha256` with the expected base64 form of the selected
hex package digest. An independent contract test binds those two encodings.

The current VersionId echo precondition carries no independent evidence and
will no longer be described as one. Selecting the exact VersionId in the data
source and Lambda resources remains required. The handler-list metadata check
also remains because Terraform derives its expected value from its own handler
list.

The retained `c88b49c8...` package has no stored S3 SHA-256 checksum. L-53 must
choose one enforceable transition rule before a checksum requirement can cover
every selection. The choices are an exact digest-and-VersionId legacy exception
or loss of that package's rollback eligibility. A nullable checksum input by
itself is insufficient because an operator could omit it for a new package.

## Failure semantics

Each manifest, source, member, or handler-declaration failure stops before the
first S3 write and reports its own bounded reason. A target import failure makes
the commit ineligible for publication under the runbook. S3 checksum mismatch
is an S3-side refusal. Exact-version read-back mismatch fails publication after
the new immutable object version exists; it never reports that version as
eligible.

Terraform checksum or handler-contract failure stops planning before any
Lambda code change. L-53 owns the preflight and failure treatment for the one
legacy package.

## Cryptographically trusted build provenance

GitHub artifact attestations could sign a claim that one exact ZIP digest came
from this public repository, commit, event, and GitHub Actions workflow. The
repository is public, so the feature is available under current GitHub plans.
Verification could reject a ZIP whose digest lacks an attestation from the
approved workflow and repository.

That protection matters when the threat includes a compromised publisher
workstation, a forged local provenance record, multiple publishers, or an
external artifact consumer. It also shortens incident reconstruction because
the workflow identity and source commit travel with the signed digest.

The current publication path builds and uploads from an operator workstation.
Attesting a separate CI build would say nothing about the locally published
bytes unless their digests match exactly and deployment verifies the
attestation. A useful adoption therefore moves package construction to GitHub
Actions, downloads and publishes those exact attested bytes, and adds an
enforced verification step before Terraform selection. Direct CI publication
would also require a new GitHub-to-AWS OIDC trust boundary.

The project currently has one owner, one dev deployment, a scoped append-only
publisher role, and no external package consumers. L-52's byte comparison,
target import gate, S3 checksum, and exact-version read-back address the
observed accidental-failure class with much lower operational cost. Trusted
build provenance is deferred. No backlog issue is opened yet; this ADR carries
the observable triggers below.

## Options considered

### Stable manifest, S3 checksum, and target import gate

This is the selected option. It catches the observed packaging errors, keeps
ADR-020 identity stable, and adds one S3-computed fact. Build-execution details
remain publisher attestations.

### Commit and builder environment inside the package

This gives every ZIP a self-contained statement. Documentation-only commits
and harmless host differences then change the package digest. It conflicts
with the current double-build boundary and can create an artificial L-54
successor.

### GitHub signed build provenance now

This ties an exact digest to a hosted build identity and source commit. It pays
off only after the deployed bytes are built by that workflow and verification
becomes a required selection gate. That build-promotion and identity change is
larger than L-52's observed failure and current single-owner dev exposure.

### Keep the module-presence check

This retains the failure that reached L-42 and accepts several malformed or
unsafe package shapes. It supplies no source identity and no S3-computed body
check.

## Consequences

- Runtime-affecting source, lock, builder-contract, handler, or target changes
  change the manifest and package digest. Documentation-only commits do not.
- A later documentation-only commit may adopt identical existing bytes. The S3
  metadata continues to identify the first publisher; the new publication
  result records the adoption attempt separately.
- The CI import job installs the production dependency tree and therefore adds
  network time to pull-request checks.
- Publisher attestations aid audit and diagnosis but remain forgeable by a
  principal that can bypass the publisher script.
- The S3 checksum independently binds the stored body to the expected digest.
  It does not validate source review, handler semantics, or package safety.
- L-53 must settle the exact legacy exception before L-54 can roll back from a
  checksum-bearing successor to `c88b49c8...`.

## Verification

- Independent known-answer tests recompute manifest bytes, the source-tree
  digest, handler digest, and hex-to-base64 package checksum from literals.
- Every pre-write publication refusal asserts that no S3 write occurred.
- Archive mutation tests cover missing, changed, and extra source members plus
  unsafe paths, symlinks, non-regular members, duplicates, malformed ZIP data,
  missing handlers, unsupported declarations, and later rebinding.
- The Linux quality job builds and extracts the dependency-bearing package,
  then imports and checks every configured handler.
- Publication tests cover S3 checksum request and exact-version read-back.
- Adoption tests prove that changed external attestations preserve existing
  bytes and first-publication metadata.
- Terraform contract tests bind hex and base64 digest representations and
  refuse mismatched checksum-bearing selections.

## Migration and rollback

Existing S3 objects remain unchanged. New packages use the manifest and S3
SHA-256 checksum. L-53 will define whether the exact retained
`c88b49c8...` digest and VersionId receive a bounded legacy exception.

Rolling back this proposal removes the manifest requirement, target import
gate, and checksum-aware selection through reviewed source and Terraform
changes. Published objects remain immutable and need no rewrite.

## Revisit conditions

Open a separate trusted-build-provenance decision before the first production
deployment if any of these occurs: a second publisher gains artifact-write
authority; CI begins publishing to AWS; another party consumes the packages;
an incident calls local publisher evidence into question; or a compliance rule
requires signed build origin. Revisit the native Linux import gate if a defect
escapes it and a Lambda-image probe would have caught that defect.

## References

References verified: 2026-09-06.

- [Amazon S3 PutObject checksums](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html)
- [AWS provider 6.58.0 S3 object data source](https://github.com/hashicorp/terraform-provider-aws/blob/v6.58.0/website/docs/d/s3_object.html.markdown)
- [AWS Lambda Python handler requirements](https://docs.aws.amazon.com/lambda/latest/dg/python-handler.html)
- [GitHub artifact attestations](https://docs.github.com/en/actions/concepts/security/artifact-attestations)
- [SLSA build track](https://slsa.dev/spec/v1.2/build-track-basics)
- [L-52: Bind Lambda artifacts to source and callable entrypoints](https://github.com/lilabrooks/aws-public-change-feed/issues/189)
- [L-53: Decide the application rollback and shadow boundary](https://github.com/lilabrooks/aws-public-change-feed/issues/190)
