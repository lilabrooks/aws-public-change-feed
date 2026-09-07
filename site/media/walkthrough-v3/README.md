# Service walkthrough media

One 3:41 video covers the architecture, delivery results, lifecycle changes,
and the completed readiness review. The public page plays `aws-public-change-alerting-walkthrough-web.mp4`
(1280 × 720); the download is `aws-public-change-alerting-walkthrough.mp4`
(1920 × 1080). Both contain 30-frame/second H.264 video, AAC narration, and
chapter metadata. The page has one player with `captions.vtt` and `chapters.vtt`.

## Current edit and voice

The opening describes configurable public AWS feed matching and shows M1, M2,
and M3 closed. The final readiness slide retains the four-feed reviewed scope. Eight SVG scenes accompany a complete
replacement narration. Slides 2 and 3 show the five Lambda roles and explain
DynamoDB, S3, SQS, CloudWatch, CloudTrail, and the IAM boundary. The last two
slides summarize M3 in 48 seconds. The poster matches the first video frame.
The [transcript](../../../docs/evidence/mvp-walkthrough.md#narration-transcript)
identifies the governing specifications and source records.

The voice setup is unchanged: Kokoro 0.9.4, `hexgrad/Kokoro-82M`, American
English (`lang_code="a"`), `af_heart`, speed `1.0`, CPU inference, and 24 kHz
mono audio. Sentences were synthesized locally, with 120 ms between chunks.
Each scene has 300 ms before speech and at least 550 ms after it. Audio padding
and scene durations use the same 30-frame/second timeline. Captions follow the
generated sentence timings. Playback does not invoke the model.

Every whole-word `AWS` in the synthesis input uses Kokoro/Misaki’s explicit
pronunciation override, `[AWS](/ˌAdˌʌbᵊljuˈɛs/)`. These are the phonemes from
the correctly pronounced opening occurrence. The generated phonemes were
checked for every occurrence; this prevents the model from reading later
instances as “aus.” The transcript and captions retain the spelling `AWS`.

`scene-01.svg` through `scene-08.svg` are the editable frame sources.
`poster.svg` supplies the PNG poster and matches scene 1. The
[readiness diagram](../../readiness.svg) explains the same evidence review
outside the video.

The architecture slides embed official [AWS Architecture Icons](https://aws.amazon.com/architecture/icons/)
from the 2026-07-31 package. Lambda, DynamoDB, S3, SQS, CloudWatch, SNS,
Secrets Manager, and Systems Manager artwork matches the existing site
diagram; EventBridge, CloudTrail, and IAM use the same official package.
AWS owns the service artwork; the project license applies to original
narration, text, and layout. Icons are embedded in the SVGs for offline rendering.

| Start | Chapter |
| --- | --- |
| 0:00.000 | Delivery, lifecycle, and readiness |
| 0:20.267 | How the Lambda jobs connect |
| 0:54.367 | Why these services have separate jobs |
| 1:39.600 | What M1 exercised |
| 2:05.333 | What M2 changed |
| 2:31.367 | What the matching score supports |
| 2:53.567 | The remaining rollback proof |
| 3:17.500 | The readiness result and its limits |

FFmpeg 7.1 encoded the final files with fast-start MP4 metadata. `SHA256SUMS`
binds both video renditions, captions, chapters, poster, and editable SVGs.
The [final assessment](../../../docs/evidence/m3-production-readiness-assessment-2026-09-06.md)
and its linked operational records own the readiness evidence.

## Historical MVP media

The [original MVP release](https://github.com/lilabrooks/aws-public-change-feed/releases/tag/mvp-evidence-v2)
and its nine-slide PDF and PowerPoint remain unchanged. They record M1 and do
not contain this revised narration or the M2/M3 scenes. The original 1080p MP4
has SHA-256 `adfdd7c7ef8c1071e2b848b49c93e6f75a6003a331f3fc2a31ee54dcb43c5bd7`;
its [historical manifest](../mvp-evidence-v2/SHA256SUMS) also covers the committed
MVP assets. The current edit uses that release's voice settings, with new
narration and visuals throughout.

Copyright © 2026 Lila Brooks. Original narration, text, and layouts are
licensed under [Apache-2.0](../../../LICENSE).

References verified: 2026-09-06.
