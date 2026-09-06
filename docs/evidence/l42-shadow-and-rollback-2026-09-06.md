# L-42 shadow and rollback exercise

- Exercise date: 2026-09-05 through 2026-09-06
- Environment: persistent `dev`, `us-east-1`
- Terminal disposition: `incomplete`
- Restricted evidence manifest SHA-256:
  `ffdf5cfade61eee19efeabccbc8c62035359607535b7a0a03ae2910a66994854`

The shadow and configuration portions passed. The application rollback was not
attempted because no distinct retained predecessor satisfied ADR-026's accepted
five-function contract. The exercise did not create an artificial package or
repeat a feed sample to obtain a preferred result.

The restricted bundle stays outside Git. This record omits caller identities,
role ARNs, queue URLs, secret identifiers, subscription endpoints, Terraform
output bodies, feed payloads, and candidate contents.

## Bound identities

| Identity | Value |
| --- | --- |
| Handler-complete application SHA-256 | `c88b49c8f070f1cb808ac005cbe28b484c14be7b29f50a34e21ef3a7ca85ccbd` |
| Application S3 VersionId | `QXNwt_NBIqp0pNKVFalwbZ72587h.GCc` |
| Configured-handler contract SHA-256 | `8d934863e4305f466c8e4982215f37b48222fee3cb18635509fae7163156cf29` |
| Forward release | `8527e2b44432e565b968d869f941cc4a90ce33fbe7376401f5489306e6027ef8` |
| Rolled-back release | `0ffc94ed3c5a16d55561aa00f018c6cf6f1e81ec58539ce002f42c6e45eb7225` |
| Candidate-set SHA-256 | `456a0feb14c5caa399a2ab2c172a474c96b36c45029afe10ff269325859396d0` |

The application package does not carry an independently recomputable source
revision or proof that every named handler symbol is callable. L-52 owns that
repair. The package digest and VersionId above bind the bytes exercised here,
but they do not close that provenance gap.

## Exercise record

### Initial failure and package repair

The first authorized invocation reached Lambda but failed before handler entry
with `Runtime.ImportModuleError`: its retained package had no
`aws_public_change_feed.shadow_runtime` module. Source-state, delivery, and raw
snapshot read-backs were unchanged, and the invocation was not repeated.

Package publication and Terraform were then changed to require the complete
configured module set and its null-framed contract digest. Saved package
remediation plan SHA-256
`b819804380d151e77e4ec7813f9b9b7cd95fa5586ef32d7481aecc5516974c64`
deployed `c88b49c8…` to all five Lambdas while every durable runtime remained
stopped.

### Identity refusals and shadow samples

Three separate identity inversions reached the repaired handler before feed
work:

| Refusal | Lambda request ID |
| --- | --- |
| `expected_release_mismatch` | `d011ebe6-534c-41d0-b69f-78492824af07` |
| `expected_application_mismatch` | `310a52fa-6290-450b-9e6b-c3ddbbb68398` |
| `expected_feed_set_mismatch` | `5860b53b-f547-47ab-96d5-229b4d79f175` |

Each of the three required valid samples fetched the four configured feeds,
normalized 240 items, produced eight route-scoped candidates, and returned the
same candidate-set digest:

| Configuration state | Invocation ID |
| --- | --- |
| Forward release before rollback | `eff3c045-1959-477e-b5be-ade9c176a69d` |
| Retained release during rollback | `8175f1bc-c20d-492f-9552-184f3cf15ee3` |
| Forward release after restoration | `81a39324-9062-4b80-b3aa-d52c9454b430` |

The final sample ran from `2026-09-06T03:14:03Z` through
`2026-09-06T03:14:14Z` with one AWS attempt and no manual retry. Its before and
after hashes matched across 631 source-state items, 31 delivery items, and 635
raw-snapshot versions. Both raw-snapshot inventories had zero delete markers.

### Configuration rollback and restoration

Configuration rollback plan SHA-256
`ff1bb5b1199c84a042cf6ca184b620aa49be894a1bc8011fa6221a6f78d1ee17`
promoted the retained release through pointer VersionId
`_lGOyhR_.KJoSCbNO_iWk3fNNMdXyJo1`. Its compatibility probe and the shadow
sample passed.

An earlier forward preview with SHA-256
`3081dcc35ebf9c15401d57506dc919965654ca24883996f7d17b4cadb73f3a40`
remained unapplied because its timestamp no longer described the reviewed
operation closely enough. The replacement preview bound its Terraform input to
the restricted copy outside `build/`. Plan SHA-256
`cdd175e2173893d8e5ea04a0b0b9c93a09b6a6e45739f88ed35a1a816de57fd2`
restored release `8527e2b4…` through pointer VersionId
`HJbvljTXyq.N1mExbfOiNn2Sv3vQnm5s`. Independent read-back produced pointer
body SHA-256
`5d41bb7adf6e21f64e6a234efa91bce7072c321b57213a33fcb43646be1c5922`.

### Historical replay

Retained-source replay stayed preview-only. Plan SHA-256
`91680e2e2133f6005d543b65bffebdc4afc9ab0584ac4024069cf9336b214d8e`
resolved the pre-exercise snapshot, exact pointer and release objects, and all
four referenced candidate and delivery records. No replay state was applied.

### Runtime restoration

Before resume, the delivery queue, delivery DLQ, and runtime-failure queue each
contained zero visible, in-flight, or delayed messages. Saved Terraform plan
SHA-256
`f37a7c006cd8c95623718b69bf3ba2295a656fbaca14413c9701f4917710526b`
made seven alarm creations and six updates: three EventBridge rules, the Slack
worker event-source mapping, watcher reserved concurrency, and a derived refresh
of the runtime-failure queue policy.

The policy's canonical SHA-256 remained
`424576bba8de2bbf8230a6e69956ecaf2068640981926e0e3452065d9921f074`.
Independent read-back found all four runtime triggers enabled, watcher reserved
concurrency at one, all five Lambda `CodeSha256` values matching the base64
encoding of `c88b49c8…`, and the exact 28-alarm set at `OK`. Final no-change
Terraform plan SHA-256 was
`6d0a1ce13bd0e820692105c7dc307790c39129987ddef5d64c3217cfbfe3d6f7`.

## Retained artifact audit

The exact-version audit found nine retained package objects and no delete
markers. Every body matched the SHA-256 in its key and had no duplicate ZIP
member. Eight packages had the four durable runtime modules, lacked
`shadow_runtime.py`, and lacked the configured-handler metadata. Only
`c88b49c8…` had all five modules and the expected metadata.

| Package SHA-256 | S3 VersionId | Modules | Shadow module | Handler metadata |
| --- | --- | ---: | --- | --- |
| `1a3db4a6dba414c11cee875986704f0783c517eb07d49d2ce605eadd6839c1e1` | `9zzQALVMVM6hSm1W8D5YIi49nbZ0wic5` | 4 | absent | absent |
| `1b91a51fe93e8c157c97b7adc2d3a78b8da8b2d07e800dd6fcf01074afa6df46` | `uKLH3.p_QxS3kIFwz1AxqEugFA1c0g.W` | 4 | absent | absent |
| `1f7c6790ad7153897440934239f1f325a7db3176211e196af990e7e76fbf75c2` | `agdov6OPk3Ke6EbTl.d5WDDP9UJI3RGg` | 4 | absent | absent |
| `32a6ff1f0955702322a385ce21eec3514c7e5b596280e97687a560577e86757e` | `kFkNMubU5u5hFkU9v0QEE439qSPD6mOs` | 4 | absent | absent |
| `38fabf49de6c61682f50a91ecb6be5dd554eb83bb9aba94e6966dc4b5e756a93` | `SPSW2V00HZ5TIoVER1auxFkbOeHxv4sn` | 4 | absent | absent |
| `39e86a937b26921f23c5ffd646f7b08a06a56f0252e5bc7ff55f35e418f8b324` | `qnzW7OQkD4XofXdT_YW6_xE4dvAmgff7` | 4 | absent | absent |
| `520fce81729e378e1fd9feda213cae27475ad1f3f659f77709646b096e290dfc` | `XXJvLzdWJb.x213YYAaTkisy_Flq8LHz` | 4 | absent | absent |
| `a442a06fe34eff04f0598e5ed153230b3f1caf599d1ef7435954daaf5e39268d` | `Jnki1iWHZZMKSF1OTC3Ozk3oNbZqyt61` | 4 | absent | absent |
| `c88b49c8f070f1cb808ac005cbe28b484c14be7b29f50a34e21ef3a7ca85ccbd` | `QXNwt_NBIqp0pNKVFalwbZ72587h.GCc` | 5 | present | `8d934863…` |

The restricted artifact-audit summary has SHA-256
`49633d2f54cb8d58faa889096c17c5b8c39722552ae6f36d44fe14b91a26e246`.

## Command shapes

Credentials came from separately assumed, scoped roles and are omitted. These
are the mutation and invocation shapes used with the reviewed files:

```bash
python3 scripts/publish_release.py rollback-apply \
  --plan <restricted-config-restoration-plan> \
  --expected-plan-sha256 cdd175e2173893d8e5ea04a0b0b9c93a09b6a6e45739f88ed35a1a816de57fd2

AWS_MAX_ATTEMPTS=1 aws lambda invoke \
  --function-name apcf-dev-shadow-evaluator \
  --invocation-type RequestResponse \
  --payload fileb://<restricted-event.json> \
  <restricted-result.json>

terraform -chdir=infra/central apply -input=false -auto-approve \
  <restricted-terraform-resume-plan>
```

Exact-version package inspection used `s3api list-object-versions`,
`s3api head-object`, and `s3api get-object` with each VersionId in the table.
The final Terraform command used `plan -detailed-exitcode`; exit zero and an
empty non-no-op change set established convergence.

## Evidence limits and successors

The surviving `build/` snapshot contains the package-remediation Terraform plan
but no saved trigger-disable plan bytes. The restricted record states that gap;
it does not infer whether those bytes were never saved or were later removed.

L-42 therefore ends `incomplete`, as its issue contract permits for a bounded
result. Configuration rollback and restoration passed. Application rollback
awaits a legitimate successor package that makes `c88b49c8…` a distinct
eligible predecessor under the boundary selected by L-53.

- [L-52: Bind Lambda artifacts to source and callable entrypoints](https://github.com/lilabrooks/aws-public-change-feed/issues/189)
- [L-53: Decide the application rollback and shadow boundary](https://github.com/lilabrooks/aws-public-change-feed/issues/190)
- [L-54: Prove application rollback with a genuine successor package](https://github.com/lilabrooks/aws-public-change-feed/issues/191)

M3 remains open through those successors, the post-M2 evidence gate, and final
status reconciliation.

References verified: 2026-09-05.
