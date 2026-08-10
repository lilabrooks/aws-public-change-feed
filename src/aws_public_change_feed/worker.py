"""The Slack delivery worker: one delivery record to one delivery outcome.

Chapter 02's worker and chapter 04's outcomes define this contract. ADR-007
makes the worker the only component that reads Slack credentials or performs
Slack HTTP requests, ADR-004 defines the delivery states and their guarantees,
and ADR-015 governs rendering, destination pacing, and retry.

The worker validates the exact stored delivery request, loads and verifies the
exact immutable release the candidate embeds, reads destination pacing, claims
the record as `sending` with a lease and attempt ID, renders bounded
plain-text-safe blocks, checks the credential it was handed, performs at most
one Slack network call, and records `posted`, `failed_retryable`,
`failed_terminal`, or `delivery_unknown`. A duplicate queue delivery for
`posted` performs no Slack call, a record still awaiting queue dispatch or held
by another lease is returned unprocessed, and a future retry returns to the
durable outbox instead of sleeping in Lambda.

Three things are revalidated at delivery time rather than trusted from
upstream, because each arrives as mutable state that no earlier check governs.
The stored source URL is re-checked against the canonical HTTPS policy inside
the renderer, since candidate identity does not cover the URL and an edited
record keeps a valid ID. The credential's kind is checked against the release's
delivery mode, and in webhook mode its value — the secret is the URL, so
publication-time validation can never have seen it. A failure in any of them is
`failed_terminal` with no network call and no charge against the Slack
attempt budget, which chapter 06 reserves for calls that actually happen.

The Slack transport and the Secrets Manager adapter stay behind ports here, so
the state machine is exercised against the same boundary that runs in
production without a deployed secret or network. The classification of a Slack
response into a delivery state lives here rather than in the client: the client
reports HTTP-level facts and the worker owns the ADR-004 mapping, so the
retry-safety condition ADR-004 states — that no request bytes were sent — is
proved from a reported fact rather than asserted by whatever adapter is wired
in.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urlsplit

from .credentials import WEBHOOK, CredentialReader, CredentialReadError, SlackCredential
from .dispatch import InvalidDeliveryRequest, validate_delivery_request
from .loading import (
    IncompatibleRelease,
    ReleaseIntegrityError,
    load_release_reference,
)
from .outbox import ACKNOWLEDGED_STATES, DeliveryPace, OutboxStore
from .releases import ObjectStore
from .semantics import CandidateSemanticsError, validate_candidate_against_release
from .urls import FeedUrlRejected, ValidatedUrl, validate_feed_url

__all__ = [
    "CredentialReadError",
    "DELIVERY_UNKNOWN",
    "FAILED_RETRYABLE",
    "FAILED_TERMINAL",
    "InvalidSlackWebhook",
    "MessageTooLarge",
    "NullWorkerMetrics",
    "POSTED",
    "SlackResponse",
    "SlackSender",
    "TransportError",
    "UnrenderableCandidate",
    "UnsafeSourceUrl",
    "WorkerMetrics",
    "WorkerResult",
    "process_delivery",
    "render_message",
    "validate_webhook_url",
]

POSTED = "posted"
FAILED_RETRYABLE = "failed_retryable"
FAILED_TERMINAL = "failed_terminal"
DELIVERY_UNKNOWN = "delivery_unknown"
_QUEUED = "queued"
_SENDING = "sending"
_PENDING = "pending_queue"

# Slack webhook URLs take the path /services/<workspace>/<channel>/<secret>.
_WEBHOOK_PATH_SEGMENTS = 4

_HTTP_OK = 200
_HTTP_TOO_MANY_REQUESTS = 429

# Chapter 04's "reviewed transient server responses". A received status is a
# definite answer from Slack, so retrying one of these is not the ambiguous
# case ADR-004 restricts; the request demonstrably reached Slack and was
# demonstrably not accepted.
_RETRYABLE_STATUS_CODES = frozenset({408, 500, 502, 503, 504})

# The Web API answers HTTP 200 with `ok: false` and an `error` member. Only
# rate limiting is retryable; every other documented error names a token,
# channel, hook, or payload problem that another attempt cannot fix.
_RETRYABLE_SLACK_ERRORS = frozenset({"ratelimited", "service_unavailable", "internal_error"})


# Slack caps a text object at 3000 characters. The environment summary is the
# one field whose length is driven by inventory size rather than by a
# per-field policy cap, so it is bounded here to leave room for its label.
_MAX_TEXT_OBJECT_CHARACTERS = 3000
_MAX_ENVIRONMENT_SUMMARY_CHARACTERS = 2000


class InvalidSlackWebhook(ValueError):
    """A Slack incoming-webhook secret URL failed runtime policy.

    The message may name the offending URL, because the caller decides what is
    safe to record; `process_delivery` discards it for exactly that reason.
    """


class UnrenderableCandidate(ValueError):
    """A stored candidate cannot become a Slack message.

    Chapter 04 forbids silently omitting identity, evidence, or the source URL,
    so a candidate that cannot be rendered within the contract is a
    `failed_terminal` delivery rather than a degraded message.
    """


class UnsafeSourceUrl(UnrenderableCandidate):
    """A stored source URL failed the canonical HTTPS policy at render time."""


class MessageTooLarge(UnrenderableCandidate):
    """The rendered message exceeds the contract after deterministic truncation."""


class TransportError(StrEnum):
    """The bounded transport failures a client may report with no HTTP status.

    A client reports what happened at the socket, not what it should mean.
    `CONNECT_FAILED` and `TLS_FAILED` occur before any request byte leaves, so
    the worker can prove an automatic retry is safe. `TIMEOUT`, `READ_FAILED`,
    and `CONNECTION_LOST` occur once a request is in flight, so they cannot.
    `MALFORMED_URL` never reaches a socket at all.
    """

    MALFORMED_URL = "malformed_url"
    CONNECT_FAILED = "connect_failed"
    TLS_FAILED = "tls_failed"
    TIMEOUT = "timeout"
    READ_FAILED = "read_failed"
    CONNECTION_LOST = "connection_lost"


# The only transport failures that can occur before a request byte is written.
# An allowlist rather than a denylist: a transport error added later defaults
# to `delivery_unknown`, which is the safe answer, instead of silently
# inheriting an automatic retry from a `bytes_sent` flag nobody re-examined.
_RETRY_SAFE_TRANSPORT_ERRORS = frozenset({TransportError.CONNECT_FAILED, TransportError.TLS_FAILED})


@dataclass(frozen=True, slots=True)
class SlackResponse:
    """HTTP-level facts about one Slack attempt, before ADR-004 mapping.

    Facts only: the client reports what it observed and the worker decides what
    it means. That split is the point. ADR-004 permits an automatic retry after
    a transport failure "only when the client proves that no request bytes were
    sent", and a response type that carried a preselected verdict would let any
    future adapter assert that proof instead of supplying it.

    `status_code` is the HTTP status, or `None` when no response arrived, in
    which case `error_class` names the bounded transport failure.
    `bytes_sent` is true when request bytes may have reached Slack; it is the
    single fact the retry-safety decision rests on, so it defaults to the
    unsafe answer and a client must deliberately claim otherwise.
    `slack_error` is the `error` member of a bot-mode JSON body, which the Web
    API returns under HTTP 200. `retry_after_seconds` is the raw header value,
    bounded later by the worker, and `message_ts` is the bot-mode message
    timestamp. Neither a secret nor a response body ever appears here.
    """

    status_code: int | None = None
    error_class: TransportError | None = None
    bytes_sent: bool = True
    latency_ms: int = 0
    slack_error: str | None = None
    retry_after_seconds: int | None = None
    message_ts: str | None = None

    def __post_init__(self) -> None:
        # Exactly `True` or `False`, checked by type rather than truthiness.
        # The whole retry-safety decision reads this field, and every value
        # Python considers falsy — `None` most plausibly, from an adapter that
        # could not determine the answer — would otherwise read as the
        # affirmative proof ADR-004 demands and authorize an automatic retry.
        # "I don't know" must never be spelled the same way as "I am certain
        # nothing was sent".
        # Every field is checked by exact type. A `SlackSender` is the one
        # port with a real network behind it, and the adapters that will
        # implement it do not exist yet — so this is the boundary where a
        # future client's mistake becomes the worker's wrong answer. It is
        # also the worst place to find out late: classification happens after
        # `sender.post`, so a malformed response raises with the Slack call
        # already made and the record still `sending`. Rejecting at
        # construction moves that failure to before the side effect.
        if type(self.bytes_sent) is not bool:
            # The whole retry-safety decision reads this. Every falsy value —
            # `None` most plausibly, from an adapter that could not determine
            # the answer — would otherwise read as the affirmative proof
            # ADR-004 demands. "I don't know" must never be spelled the same
            # way as "I am certain nothing was sent".
            raise ValueError(f"bytes_sent must be exactly True or False, not {self.bytes_sent!r}")
        if type(self.latency_ms) is not int or self.latency_ms < 0:
            raise ValueError(f"latency_ms must be a non-negative integer, not {self.latency_ms!r}")
        if self.message_ts is not None and not isinstance(self.message_ts, str):
            raise ValueError(f"message_ts must be a string when present, not {self.message_ts!r}")
        if self.retry_after_seconds is not None and type(self.retry_after_seconds) is not int:
            raise ValueError(f"retry_after_seconds must be an integer when present, not {self.retry_after_seconds!r}")

        if self.status_code is None:
            if not isinstance(self.error_class, TransportError):
                # A bare string here constructed fine and then crashed
                # `_classify` with `AttributeError` on `.value` — after the
                # Slack call, so the attempt was spent and the record stranded.
                raise ValueError(
                    f"a SlackResponse without a status code must name a TransportError, not {self.error_class!r}"
                )
        else:
            # `bool` is an `int` in Python, so `status_code=True` passed an
            # `isinstance` check and classified as the terminal `http_True`.
            if type(self.status_code) is not int or not 100 <= self.status_code <= 599:
                raise ValueError(f"status_code must be an HTTP status between 100 and 599, not {self.status_code!r}")
            if self.error_class is not None:
                raise ValueError("a SlackResponse with a status code must not name a transport error_class")
            if not self.bytes_sent:
                # A received status proves the request reached Slack. Accepting
                # the contradiction would let a client mint an automatic retry
                # for an attempt Slack answered.
                raise ValueError("a SlackResponse with a status code cannot claim that no bytes were sent")
            if self.slack_error is not None and (not isinstance(self.slack_error, str) or not self.slack_error):
                raise ValueError(f"slack_error must be a nonempty string when present, not {self.slack_error!r}")


class SlackSender(Protocol):
    """The narrow network surface the worker needs.

    `credential.kind` tells the adapter how to post: an incoming webhook is
    posted to its validated secret URL, and a bot token posts to the fixed
    Slack API host. The adapter reports HTTP-level facts in a `SlackResponse`
    and makes no delivery-state decision; the worker owns the ADR-004 mapping.
    """

    def post(
        self, payload: Mapping[str, Any], *, credential: SlackCredential, timeout_seconds: float
    ) -> SlackResponse: ...


class WorkerMetrics(Protocol):
    def delivery_attempted(self) -> None: ...
    def posted(self) -> None: ...
    def retryable(self) -> None: ...
    def terminal(self) -> None: ...
    def unknown(self) -> None: ...
    def duplicate_posted(self) -> None: ...
    def unprocessed(self) -> None: ...
    def dropped_message(self) -> None: ...


class NullWorkerMetrics:
    def delivery_attempted(self) -> None:
        pass

    def posted(self) -> None:
        pass

    def retryable(self) -> None:
        pass

    def terminal(self) -> None:
        pass

    def unknown(self) -> None:
        pass

    def duplicate_posted(self) -> None:
        pass

    def unprocessed(self) -> None:
        pass

    def dropped_message(self) -> None:
        pass


@dataclass(frozen=True, slots=True)
class WorkerResult:
    """What one message needs to happen in the Lambda batch handler.

    `handled` is false only when the message must be returned unprocessed in
    `batchItemFailures`: the record still awaits queue dispatch, another worker
    holds the lease, or the claim race was lost. A recorded outcome, a
    duplicate of posted work, or a message with no actionable record is
    `handled` and acknowledged. `state` is the delivery state recorded, if any,
    and `performed_network_call` records whether Slack was contacted.
    """

    handled: bool
    state: str | None = None
    performed_network_call: bool = False
    reason: str | None = None


def _slack_escape(text: str) -> str:
    """Escape Slack control characters, for `mrkdwn` and fallback text only.

    Chapter 04 escapes "fallback and link text". Those are the contexts Slack
    parses: `&`, `<`, and `>` in a `mrkdwn` element or the top-level fallback
    would otherwise open a link or an entity. A `plain_text` object is not
    parsed, so escaping one does not protect anything — it displays the escape
    sequence to the reader, turning a summary that mentions `<region>` into one
    that reads `&lt;region&gt;`. Untrusted content belongs in `plain_text`
    unescaped, which is why chapter 04 asks for those blocks in the first place.
    """

    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _truncate(text: str, maximum: int) -> str:
    """Truncate to the configured cap with an explicit truncation marker."""

    if len(text) <= maximum:
        return text
    marker = "\u2026"
    if maximum <= len(marker):
        return text[:maximum]
    return text[: maximum - len(marker)] + marker


def _environment_labels(candidate: Mapping[str, Any], inventory: Mapping[str, Any]) -> list[str]:
    """One `Customer (environment_id)` label per potentially relevant stack.

    Rendered with the environment ID so the reader can act on a specific stack
    rather than guess which of a customer's environments is meant. An ID absent
    from the inventory renders as itself rather than being dropped.
    """

    by_id = {env["id"]: env for env in inventory["environments"]}
    labels: list[str] = []
    for environment_id in candidate["environment_ids"]:
        env = by_id.get(environment_id)
        if env is None:
            labels.append(environment_id)
        else:
            labels.append(f"{env['customer']} ({environment_id})")
    return labels


def _join_environments(labels: Sequence[str], visible: int) -> str:
    """Join `visible` labels, naming how many were left out."""

    if visible >= len(labels):
        return ", ".join(labels)
    omitted = len(labels) - visible
    if visible <= 0:
        return f"{len(labels)} potentially relevant environments (too many to list)"
    return f"{', '.join(labels[:visible])} (and {omitted} more)"


def _fit_environment_summary(labels: Sequence[str], *, maximum: int, budget: int) -> str:
    """Shrink the environment summary until it fits, deterministically.

    Chapter 04 requires deterministic field truncation before a message may be
    refused, and this is the field that has to yield. Every other rendered
    field has a per-field policy cap, so the message length they contribute is
    bounded by configuration; the environment list is sized by how many stacks
    actually matched, which is data. A wholly valid audience — thirty
    environments whose customer names are each within the inventory schema's
    100-character maximum — otherwise renders past `max_message_characters`
    and turns a real alert into `failed_terminal`, delivering nothing to
    anyone.

    Dropping display entries here is safe in a way that dropping other fields
    would not be. Chapter 04's "do not silently omit" rule names identity,
    evidence, and the source URL; none of them live in this field, the count of
    what was left out stays visible, and the full environment set remains on
    the candidate for anyone who follows the ID.

    Entries are removed from the end, so the same audience and budget always
    produce the same summary, and a smaller budget yields a prefix of a larger
    one.
    """

    visible = min(len(labels), maximum)
    while visible > 0:
        summary = _join_environments(labels, visible)
        if len(summary) <= budget:
            return summary
        visible -= 1
    # Not even one label fits, which means a single label is itself longer
    # than the budget. Naming the count beats showing one mangled customer
    # name, and the final truncation is a guard for a budget so small that
    # even the count does not fit.
    return _truncate(_join_environments(labels, 0), budget)


def _candidate_timestamp(candidate: Mapping[str, Any]) -> str:
    announcement = candidate["announcement"]
    published_at = announcement.get("published_at")
    if isinstance(published_at, str) and published_at:
        return published_at
    observed: str = announcement["observed_at"]
    return observed


def render_message(
    candidate: Mapping[str, Any],
    inventory: Mapping[str, Any],
    *,
    message_policy: Mapping[str, Any],
    approved_source_hosts: Sequence[str],
    usergroup_id: str | None = None,
) -> dict[str, Any]:
    """Render the Slack payload for one candidate, bounded and plain-text safe.

    Chapter 04 requires the message to carry the fallback string, potentially
    relevant wording, title and source link, service and risk, the match
    explanation, the bounded environment summary, the recommended action, the
    publication or observation time, and the candidate ID. Each source field is
    truncated to its configured cap with an explicit marker, control characters
    are escaped in the fallback and in link text, and untrusted content goes
    into unescaped `plain_text` objects that Slack does not parse.

    The source URL is revalidated here against the canonical HTTPS policy, and
    `approved_source_hosts` is required rather than optional. Acquisition
    already applied that policy, but a candidate reaches this function as bytes
    read back from the delivery table, and the candidate ID is derived from
    revision, service, risk, route, and audience — not from the URL. A record
    edited in place therefore keeps a valid identity while carrying any URL at
    all, and chapter 04 requires the check "before becoming links". Doing it
    inside the renderer rather than in the caller is what makes it unskippable.

    Raises `UnsafeSourceUrl` when the stored URL fails that policy and
    `MessageTooLarge` when the message still exceeds the contract after
    truncation. Both are `failed_terminal` for the caller; chapter 04 forbids
    silently omitting the source URL or the evidence instead.

    High-priority mention behavior uses only the configured user-group ID, so
    source-derived strings can never create a mention.
    """

    announcement = candidate["announcement"]
    service = candidate["service"]
    risk = candidate["risk"]
    explainability = candidate["explainability"]

    source_url = announcement["url"]
    try:
        validate_feed_url(source_url, approved_hosts=approved_source_hosts)
    except FeedUrlRejected as error:
        raise UnsafeSourceUrl(f"stored source URL failed the canonical HTTPS policy: {error}") from error

    max_environments = message_policy.get("max_potentially_relevant_environments_in_slack", 30)
    labels = _environment_labels(candidate, inventory)

    title = _truncate(announcement["title"], message_policy["max_title_characters"])
    summary = _truncate(announcement.get("summary", ""), message_policy["max_summary_characters"])
    reason = _truncate(explainability["reason"], message_policy["max_explanation_characters"])
    recommended_action = _truncate(candidate["recommended_action"], message_policy["max_recommended_action_characters"])

    title_link = f"<{source_url}|{_slack_escape(title)}>"
    header = _slack_escape(f"Potentially relevant AWS change candidate ({risk['priority']} priority)")
    if risk.get("priority") == "high" and usergroup_id:
        header = f"{header} <!subteam^{usergroup_id}>"
    service_and_risk = _slack_escape(f"{service['display_name']} \u00b7 {risk['risk_type']}")
    footer = f"{_candidate_timestamp(candidate)} \u00b7 {candidate['candidate_id']}"
    max_chars = message_policy.get("max_message_characters", 4000)

    def build(environment_summary: str) -> dict[str, Any]:
        """Assemble the payload for one environment summary.

        Called twice: once with an empty summary to measure everything the
        summary has to fit around, then once with the summary fitted to what
        is left. Building the real structure to measure it, rather than adding
        up the pieces by hand, means the measurement cannot drift from the
        payload when a block is added.
        """

        # The `mrkdwn` block carries only the two things that need Slack to
        # parse them: the source link, and service/risk names that come from
        # the release rather than from a feed. The announcement summary used to
        # ride along here. Escaping `&`, `<`, and `>` stopped it from forging a
        # link or a mention, but left `*bold*`, `_italics_`, backtick code
        # spans, and `>` quoting active, so feed text could still dictate how
        # the message looked. Chapter 04 asks for untrusted content in
        # `plain_text`, and the summary is untrusted content.
        blocks: list[dict[str, Any]] = [
            {"type": "context", "elements": [{"type": "mrkdwn", "text": header}]},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"{title_link}\n{service_and_risk}"}},
        ]

        # Every block below is `plain_text`, which Slack does not parse. The
        # text is deliberately not escaped: see `_slack_escape`.
        if summary:
            blocks.append({"type": "section", "text": {"type": "plain_text", "text": summary, "emoji": False}})
        blocks.append({"type": "section", "text": {"type": "plain_text", "text": reason, "emoji": False}})
        if environment_summary:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "plain_text",
                        "text": f"Potentially relevant: {environment_summary}",
                        "emoji": False,
                    },
                }
            )
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "plain_text",
                    "text": f"Recommended review action: {recommended_action}",
                    "emoji": False,
                },
            }
        )
        blocks.append({"type": "context", "elements": [{"type": "plain_text", "text": footer, "emoji": False}]})

        # Slack documents the top-level text as what screen readers announce
        # and what notifications and unsupported clients show, so it carries
        # the whole alert rather than a summary of it: the source URL, the
        # publication or observation time, and the candidate ID were all
        # missing, which made the accessible rendering the incomplete one.
        #
        # `mrkdwn: false` accompanies it because Slack parses this field by
        # default. Untrusted feed text reaches it — the title always, the
        # summary now — and escaping `&`, `<`, and `>` would leave `*bold*`
        # and backticks live, exactly as it did in the block this text mirrors.
        # With parsing off, nothing here needs escaping and none of it renders
        # as formatting.
        fallback_lines = [
            f"Potentially relevant AWS change candidate ({risk['priority']} priority)",
            title,
            f"{service['display_name']} \u00b7 {risk['risk_type']}",
        ]
        if summary:
            fallback_lines.append(summary)
        fallback_lines.extend(
            [
                f"Source: {source_url}",
                f"Reason: {reason}",
                f"Potentially relevant: {environment_summary}",
                f"Recommended review action: {recommended_action}",
                f"{_candidate_timestamp(candidate)} \u00b7 {candidate['candidate_id']}",
            ]
        )
        fallback = _truncate("\n".join(fallback_lines), max_chars)
        return {"text": fallback, "mrkdwn": False, "blocks": blocks}

    # Chapter 04: deterministic field truncation first, refusal only if the
    # message is still over. The environment summary is the field that yields,
    # because it is the only one sized by data rather than by a policy cap.
    # It is charged twice — once in the blocks and once in the fallback — so
    # the remaining budget is halved before it is offered.
    fixed_characters = sum(len(text) for text in _text_objects(build("")))
    budget = min(
        _MAX_ENVIRONMENT_SUMMARY_CHARACTERS,
        _MAX_TEXT_OBJECT_CHARACTERS - len("Potentially relevant: "),
        max(0, (max_chars - fixed_characters) // 2),
    )
    environment_summary = _fit_environment_summary(labels, maximum=max_environments, budget=budget)
    payload = build(environment_summary)

    # The backstop. Field truncation cannot rescue a policy whose per-field
    # caps already exceed the whole-message budget, and chapter 04 is explicit
    # that such a message is refused rather than silently shortened past its
    # identity, evidence, or source URL.
    for text in _block_texts(payload):
        if len(text) > _MAX_TEXT_OBJECT_CHARACTERS:
            raise MessageTooLarge(
                f"a rendered text object is {len(text)} characters; Slack caps one at {_MAX_TEXT_OBJECT_CHARACTERS}"
            )
    rendered_characters = sum(len(text) for text in _text_objects(payload))
    if rendered_characters > max_chars:
        raise MessageTooLarge(
            f"the rendered message is {rendered_characters} characters after truncation; "
            f"max_message_characters is {max_chars}"
        )
    return payload


def _block_texts(payload: Mapping[str, Any]) -> list[str]:
    """Every Block Kit text object in the payload.

    Both block shapes this renderer emits are walked: a `section` carries one
    `text` object and a `context` carries an `elements` list. Walking the
    structure rather than listing the strings at their construction sites means
    a block added later is measured without a second edit here.

    The top-level fallback is deliberately not included. It is a message field
    rather than a composition object, so Slack's 3000-character text-object
    limit does not apply to it — folding it in here made a 4000-character
    fallback, which `max_message_characters` explicitly permits, fail the
    wrong check with the wrong explanation.
    """

    texts: list[str] = []
    for block in payload["blocks"]:
        text_object = block.get("text")
        if isinstance(text_object, Mapping):
            texts.append(str(text_object["text"]))
        for element in block.get("elements", ()):
            if isinstance(element, Mapping) and "text" in element:
                texts.append(str(element["text"]))
    return texts


def _text_objects(payload: Mapping[str, Any]) -> list[str]:
    """Every rendered string a reader could see: the fallback and the blocks.

    This is the whole-message measure `max_message_characters` applies to.
    """

    return [str(payload["text"]), *_block_texts(payload)]


def validate_webhook_url(raw_url: str, *, approved_hosts: Sequence[str]) -> ValidatedUrl:
    """Apply chapter 04's incoming-webhook runtime controls to the secret value.

    The secret is the webhook URL, so it is validated at runtime rather than at
    publication: HTTPS, the default port, an approved lowercase hostname, no
    user info or fragment, a bounded path and query, and the expected Slack
    service path. Resolution and connection use the same anti-rebinding
    controls as feed acquisition in the adapter. Never log the URL.
    """

    try:
        validated = validate_feed_url(raw_url, approved_hosts=approved_hosts)
    except FeedUrlRejected as error:
        raise InvalidSlackWebhook(f"Slack webhook URL rejected: {error}") from error
    path = validated.path_with_query.split("?")[0]
    segments = path.strip("/").split("/")
    if len(segments) != _WEBHOOK_PATH_SEGMENTS or segments[0] != "services":
        raise InvalidSlackWebhook("Slack webhook URL does not carry the expected /services/... path")
    for segment in segments[1:]:
        if not segment or not segment.replace("-", "").isalnum():
            raise InvalidSlackWebhook(f"Slack webhook URL path contains an unexpected segment: {segment!r}")
    return validated


def _bounded_retry_after(response: SlackResponse, max_retry_after_seconds: int) -> int | None:
    """The `Retry-After` header, clamped, or `None` when it is not usable."""

    retry_after = response.retry_after_seconds
    if isinstance(retry_after, bool) or not isinstance(retry_after, int) or retry_after <= 0:
        return None
    return min(retry_after, max_retry_after_seconds)


def _classify(
    response: SlackResponse,
    *,
    max_retry_after_seconds: int,
) -> tuple[str, str, int | None]:
    """Map HTTP-level facts to `(state, response_class, bounded_retry_after)`.

    Chapter 04's outcome classes, derived here rather than accepted from the
    client. The `response_class` returned is the diagnostic label stored on the
    record, built from the observed facts so it cannot disagree with the state.

    The retry-safety rule is ADR-004's, applied literally. When no response
    arrived, an automatic retry needs proof that no request bytes were sent;
    `bytes_sent` is that proof and its absence means `delivery_unknown`, never
    a retry. When a response did arrive, the request definitely reached Slack
    and the status is a definite answer, so ambiguity does not arise: 429 and
    reviewed transient server statuses are retryable, other errors are
    terminal. Bot mode returns HTTP 200 with `ok: false`, so a body-level error
    under a 200 is classified rather than read as success.
    """

    if response.status_code is None:
        assert response.error_class is not None
        label = f"transport_{response.error_class.value}"
        if response.error_class is TransportError.MALFORMED_URL:
            # No socket was opened, so nothing was sent — but retrying cannot
            # help either. The URL comes from the credential or the release,
            # and both need a human. Retrying on the no-bytes proof alone
            # would spin this to the attempt ceiling and only then escalate.
            return FAILED_TERMINAL, label, None
        if response.error_class in _RETRY_SAFE_TRANSPORT_ERRORS and response.bytes_sent is False:
            # Both conditions required: a failure class that happens before the
            # request is written, and the client's positive assertion that
            # nothing went out. Either one alone is not ADR-004's proof.
            return FAILED_RETRYABLE, label, None
        return DELIVERY_UNKNOWN, label, None

    label = f"http_{response.status_code}"
    if response.status_code == _HTTP_OK:
        if response.slack_error is None:
            return POSTED, label, None
        label = f"slack_{response.slack_error}"
        if response.slack_error in _RETRYABLE_SLACK_ERRORS:
            return FAILED_RETRYABLE, label, _bounded_retry_after(response, max_retry_after_seconds)
        return FAILED_TERMINAL, label, None

    if response.status_code == _HTTP_TOO_MANY_REQUESTS:
        return FAILED_RETRYABLE, label, _bounded_retry_after(response, max_retry_after_seconds)
    if response.status_code in _RETRYABLE_STATUS_CODES:
        return FAILED_RETRYABLE, label, None
    return FAILED_TERMINAL, label, None


def _retry_delay(
    attempt: int,
    *,
    min_interval_seconds: int,
    max_retry_after_seconds: int,
    candidate_id: str,
) -> int:
    """Bounded exponential backoff with a hash-based spread.

    Chapter 04 requires a spread that does not change identity, so tests can
    reproduce the same delay for the same (candidate, attempt). The spread is
    drawn from a hash of the candidate and attempt count rather than from a
    random source.
    """

    capped: int = min(min_interval_seconds * (2 ** max(0, attempt - 1)), max_retry_after_seconds)
    window: int = max(1, max_retry_after_seconds // 20)
    digest = hashlib.sha256(f"{candidate_id}\0{attempt}".encode()).hexdigest()
    jitter: int = int(digest[:16], 16) % window
    return min(capped + jitter, max_retry_after_seconds)


def _slack_response_metadata(
    response: SlackResponse,
    *,
    response_class: str,
    retry_after_seconds: int | None,
) -> dict[str, Any]:
    """The bounded diagnostic record chapter 04 keeps for one Slack attempt.

    `response_class` is the worker's derived label rather than anything the
    client chose, so the stored diagnosis cannot disagree with the stored
    state. `bytes_sent` is recorded because it is the fact the retry decision
    turned on, and an operator reviewing a `delivery_unknown` needs to see it.
    No response body and no credential appears here.
    """

    metadata: dict[str, Any] = {
        "response_class": response_class,
        "latency_ms": response.latency_ms,
        "bytes_sent": response.bytes_sent,
    }
    if response.status_code is not None:
        metadata["status_code"] = response.status_code
    if retry_after_seconds is not None:
        metadata["retry_after_seconds"] = retry_after_seconds
    if response.message_ts is not None:
        metadata["message_ts"] = response.message_ts
    return metadata


_OUTCOME_METRIC: Mapping[str, Any] = {
    POSTED: lambda metrics: metrics.posted(),
    FAILED_RETRYABLE: lambda metrics: metrics.retryable(),
    FAILED_TERMINAL: lambda metrics: metrics.terminal(),
    DELIVERY_UNKNOWN: lambda metrics: metrics.unknown(),
}


def _lost_outcome_write(
    store: OutboxStore,
    candidate: str,
    metrics: WorkerMetrics,
    *,
    performed_network_call: bool,
) -> WorkerResult:
    """Decide the message's fate when a conditional outcome write is lost.

    The claim was superseded before the write, so this worker's view of the
    record is stale by definition. Acknowledging on that basis alone discards a
    message whose delivery may still be outstanding, on the strength of a write
    this worker knows it lost — so the durable record is reread and the answer
    comes from the store instead.

    An acknowledged record means some worker recorded an outcome and the
    message is finished. Anything else, a missing record included, is returned
    unprocessed so the work stays visible in `batchItemFailures`.

    `performed_network_call` distinguishes the two callers and is not a detail:
    the pre-call terminal paths (bad credential, unsafe URL, oversize message)
    reach here having contacted nothing, and reporting otherwise would put a
    Slack attempt in the operator's record that never happened.
    """

    current = store.get_delivery(candidate)
    if current is None:
        metrics.unprocessed()
        return WorkerResult(
            handled=False,
            state=None,
            performed_network_call=performed_network_call,
            reason="outcome write lost and the record has since disappeared",
        )
    if current.status in ACKNOWLEDGED_STATES:
        return WorkerResult(
            handled=True,
            state=current.status,
            performed_network_call=performed_network_call,
            reason=f"outcome write lost; the record is durably {current.status}",
        )
    metrics.unprocessed()
    return WorkerResult(
        handled=False,
        state=current.status,
        performed_network_call=performed_network_call,
        reason=f"outcome write lost; the record is {current.status} and still outstanding",
    )


def _approved_source_hosts(config: Mapping[str, Any]) -> tuple[str, ...]:
    """The hosts a candidate's source link may point at, from the same release.

    Chapter 04 asks for "the same canonical HTTPS policy" on source URLs. The
    acquisition allowlist lives in `deployment.yaml`, which the worker never
    reads, so the set is derived from the feed URLs in the release the
    candidate itself embeds.

    That set is equivalent to the deployment allowlist under the validated
    release contract, not merely a safe substitute for it: the cross-document
    validator requires every configured feed host to be allowed and every
    allowed host to be used by a configured feed, so the two are equal by
    construction on any release that validates. Deriving it here reaches the
    same set through the document the candidate carries, and needs no new
    config field duplicating the feed list.
    """

    hosts: set[str] = set()
    for feed in config.get("feeds", ()):
        hostname = urlsplit(str(feed["url"])).hostname
        if hostname:
            hosts.add(hostname.casefold())
    return tuple(sorted(hosts))


def _advance_pace(
    store: OutboxStore,
    destination_key: str,
    pace: DeliveryPace | None,
    now_ts: int,
    min_interval_seconds: int,
    bounded_retry_after: int | None,
    response_class: str,
) -> None:
    """Conditionally advance destination pacing after a Slack call.

    The write is conditional on the pace version; a lost race means another
    worker already advanced it, which is recorded but requires no action.
    """
    expected_version = None if pace is None else pace.version
    pacing_delay = bounded_retry_after if bounded_retry_after is not None else 0
    store.update_pace(
        destination_key,
        expected_version=expected_version,
        next_allowed_at=now_ts + max(min_interval_seconds, pacing_delay),
        last_response_class=response_class,
    )


def process_delivery(
    store: OutboxStore,
    release_store: ObjectStore,
    credentials: CredentialReader,
    sender: SlackSender,
    *,
    candidate: str,
    now: datetime,
    max_delivery_request_bytes: int,
    lease_duration_seconds: int,
    max_network_attempts: int,
    delivery_state_ttl_days: int,
    metrics: WorkerMetrics | None = None,
) -> WorkerResult:
    """Process one delivery record to one outcome, with at most one Slack call.

    Returns a `WorkerResult` the batch handler can act on: `handled` messages
    are acknowledged, and unhandled ones return in `batchItemFailures`. The
    record, release, and pace are read fresh on every attempt, and every write
    is conditional on the observed state version, so concurrent workers cannot
    overwrite one another.
    """

    metrics = metrics if metrics is not None else NullWorkerMetrics()
    now_ts = int(now.timestamp())

    record = store.get_delivery(candidate)
    if record is None:
        metrics.dropped_message()
        return WorkerResult(handled=True, state=None, reason="no delivery record")

    if record.status == POSTED:
        metrics.duplicate_posted()
        return WorkerResult(handled=True, state=POSTED, reason="duplicate of posted work")

    if record.status in (FAILED_TERMINAL, DELIVERY_UNKNOWN):
        return WorkerResult(handled=True, state=record.status, reason="message for terminal or unknown work")

    if record.status == _PENDING:
        metrics.unprocessed()
        return WorkerResult(handled=False, state=_PENDING, reason="record still awaits queue dispatch")

    if record.status == _SENDING:
        if record.lease_expires_at is not None and record.lease_expires_at <= now_ts:
            # ADR-004: a stale sending lease becomes delivery_unknown. The
            # reconciler normally performs this; a worker that observes it
            # first records the same transition so redelivery does not spin.
            assert record.attempt_id is not None
            if not store.record_outcome(
                candidate,
                expected_state_version=record.state_version,
                attempt_id=record.attempt_id,
                status=DELIVERY_UNKNOWN,
                network_attempt_count=record.network_attempt_count,
                next_action_at=None,
                slack_response=None,
                expires_at=None,
            ):
                # The same reread the other two lost-write paths perform. This
                # branch used to report `delivery_unknown` regardless, which is
                # the state it tried to write rather than the state that
                # exists: a worker that lost this race to one recording
                # `posted` reported an unknown outcome for a delivered message,
                # and would have sent an operator to inspect Slack for nothing.
                return _lost_outcome_write(store, candidate, metrics, performed_network_call=False)
            metrics.unknown()
            return WorkerResult(handled=True, state=DELIVERY_UNKNOWN, reason="expired sending lease")
        metrics.unprocessed()
        return WorkerResult(handled=False, state=_SENDING, reason="another worker holds the lease")

    if record.status != _QUEUED:
        return WorkerResult(handled=True, state=record.status, reason=f"unexpected state {record.status}")

    if record.next_action_at is not None and record.next_action_at > now_ts:
        metrics.unprocessed()
        return WorkerResult(handled=False, state=_QUEUED, reason="work not yet due")

    request = record.request
    try:
        validate_delivery_request(
            request,
            max_bytes=max_delivery_request_bytes,
            candidate_id=candidate,
            destination_key=record.destination_key,
        )
    except InvalidDeliveryRequest as error:
        # The dispatcher already validated the exact stored request, so a
        # failure here means the record or its embedded candidate changed in
        # place. Return the message unprocessed rather than make a Slack call;
        # persistent failures land in the FIFO DLQ for an operator.
        metrics.unprocessed()
        return WorkerResult(handled=False, state=_QUEUED, reason=f"invalid delivery request: {error}")

    embedded = request["candidate"]
    try:
        loaded = load_release_reference(
            release_store,
            embedded["release"],
            application_version=embedded["release"]["application_version"],
        )
    except (IncompatibleRelease, ReleaseIntegrityError) as error:
        # The release the candidate embeds cannot be verified or evaluated
        # right now. Fail closed without a Slack call; the message retries into
        # the DLQ rather than rendering unverified display data.
        metrics.unprocessed()
        return WorkerResult(handled=False, state=_QUEUED, reason=f"embedded release unusable: {error}")

    inventory = loaded.inventory
    route = inventory["slack"]["routes"].get(embedded["route_id"])
    if route is None:
        metrics.unprocessed()
        return WorkerResult(handled=False, state=_QUEUED, reason=f"route {embedded['route_id']} missing from inventory")
    if route["destination_key"] != record.destination_key:
        metrics.unprocessed()
        return WorkerResult(
            handled=False, state=_QUEUED, reason="record destination disagrees with the inventory route"
        )

    rate_control = inventory["slack"]["rate_control"]
    min_interval_seconds = rate_control["per_destination_min_interval_seconds"]
    timeout_seconds = rate_control["slack_request_timeout_seconds"]
    max_retry_after_seconds = rate_control["max_retry_after_seconds"]

    pace = store.get_pace(record.destination_key)
    if pace is not None and pace.next_allowed_at > now_ts:
        # ADR-015: do not claim work the destination cannot accept yet. Leave
        # the record retryable for the next allowed time and acknowledge this
        # message; the dispatcher creates a fresh queue delivery when due.
        if store.schedule_retry(
            candidate,
            expected_state_version=record.state_version,
            next_action_at=pace.next_allowed_at,
            slack_response=None,
        ):
            metrics.retryable()
            return WorkerResult(handled=True, state=FAILED_RETRYABLE, reason="destination pacing")
        return WorkerResult(handled=False, state=_QUEUED, reason="pacing claim lost")

    attempt_id = uuid.uuid4().hex
    lease_expires_at = now_ts + lease_duration_seconds
    if not store.claim_sending(
        candidate,
        expected_state_version=record.state_version,
        attempt_id=attempt_id,
        lease_expires_at=lease_expires_at,
    ):
        metrics.unprocessed()
        return WorkerResult(handled=False, state=_QUEUED, reason="sending claim lost")

    claimed_version = record.state_version + 1
    ttl_at = now_ts + int(delivery_state_ttl_days) * 86400

    def terminal_without_call(response_class: str, reason: str) -> WorkerResult:
        """Record a terminal outcome for a failure that made no Slack call.

        Chapter 06: "Given queue receives that perform no Slack call, the
        network-attempt counter does not change." The stored count is written
        back unchanged, so a render, credential, or webhook-policy failure
        cannot spend budget that ADR-004 reserves for actual network attempts.
        Each of these is a configuration or data correction anyway, and no
        number of retries fixes one.
        """

        if not store.record_outcome(
            candidate,
            expected_state_version=claimed_version,
            attempt_id=attempt_id,
            status=FAILED_TERMINAL,
            network_attempt_count=record.network_attempt_count,
            next_action_at=None,
            slack_response={"response_class": response_class},
            expires_at=ttl_at,
        ):
            # Same reread the post-call path performs. Acknowledging here on a
            # write this worker lost would drop a message whose record may
            # still be `sending` or otherwise outstanding.
            return _lost_outcome_write(store, candidate, metrics, performed_network_call=False)
        metrics.terminal()
        return WorkerResult(handled=True, state=FAILED_TERMINAL, reason=reason)

    # Chapter 04's candidate rules, against the release just verified. The
    # schema check above proves shape and the identity check proves the digests
    # are self-consistent, but neither covers the fields a reader actually
    # acts on: display name, priority, explanation, and recommended action sit
    # outside every digest by design, because they are projections of the
    # release rather than identity. Without this, a stored request edited in
    # place — every ID and release hash intact — rendered and posted whatever
    # it said, including a forged high-priority mention.
    try:
        validate_candidate_against_release(loaded.config, inventory, embedded)
    except CandidateSemanticsError as error:
        return terminal_without_call(
            "candidate_disagrees_with_release",
            f"stored candidate disagrees with its release: {error}",
        )

    usergroup_id = route.get("high_priority_usergroup_id")
    message_policy = loaded.config["message_policy"]
    try:
        payload = render_message(
            embedded,
            inventory,
            message_policy=message_policy,
            approved_source_hosts=_approved_source_hosts(loaded.config),
            usergroup_id=usergroup_id,
        )
    except UnsafeSourceUrl as error:
        return terminal_without_call("render_unsafe_source_url", f"unsafe source URL: {error}")
    except MessageTooLarge as error:
        return terminal_without_call("render_message_too_large", f"message exceeds the contract: {error}")
    if len(payload["blocks"]) > message_policy.get("max_blocks", 40):
        return terminal_without_call("render_too_many_blocks", "rendered message exceeds max_blocks")

    delivery_mode = inventory["slack"]["delivery_mode"]
    try:
        secret = credentials.read(
            route["credential_secret_id"] if delivery_mode == WEBHOOK else inventory["slack"]["bot_token_secret_id"]
        )
    except CredentialReadError as error:
        # ADR-004: a missing or unreadable credential is a configuration
        # correction, so the record becomes failed_terminal and an operator
        # fixes the secret container and replays.
        return terminal_without_call("credential_read_error", f"credential read failed: {error}")

    # The secret container is mutable deployment state that no schema governs,
    # so what came back is checked against what the release says this route
    # delivers with. A bot token posted to a webhook endpoint, or a webhook URL
    # sent as a bearer token, is a credential mix-up that must not reach Slack.
    if secret.kind != delivery_mode:
        return terminal_without_call(
            "credential_kind_mismatch",
            f"credential kind {secret.kind} does not match the inventory delivery mode {delivery_mode}",
        )

    if delivery_mode == WEBHOOK:
        # Chapter 04's incoming-webhook controls apply to the secret value
        # itself, and runtime is the only place they can apply: the URL is the
        # credential, so it exists in no release artifact to be validated at
        # publication. The rejection detail is deliberately dropped — it
        # embeds the URL, and chapter 04 says never log it.
        try:
            validate_webhook_url(secret.value, approved_hosts=inventory["slack"]["approved_webhook_hosts"])
        except InvalidSlackWebhook:
            return terminal_without_call(
                "webhook_url_rejected",
                "the stored webhook secret failed chapter 04's incoming-webhook controls",
            )

    network_attempt_count = record.network_attempt_count + 1
    try:
        metrics.delivery_attempted()
        response = sender.post(payload, credential=secret, timeout_seconds=timeout_seconds)
    except Exception:
        # The port raised instead of reporting. Nothing is known about whether
        # bytes reached Slack, and ADR-004 permits an automatic retry only on
        # proof that none did, so this is `delivery_unknown` for an operator.
        recorded = store.record_outcome(
            candidate,
            expected_state_version=claimed_version,
            attempt_id=attempt_id,
            status=DELIVERY_UNKNOWN,
            network_attempt_count=network_attempt_count,
            next_action_at=None,
            slack_response={"response_class": "sender_raised"},
            expires_at=None,
        )
        _advance_pace(store, record.destination_key, pace, now_ts, min_interval_seconds, None, "sender_raised")
        if not recorded:
            return _lost_outcome_write(store, candidate, metrics, performed_network_call=True)
        metrics.unknown()
        return WorkerResult(
            handled=True,
            state=DELIVERY_UNKNOWN,
            performed_network_call=True,
            reason="sender raised",
        )

    outcome_state, response_class, bounded_retry_after = _classify(
        response,
        max_retry_after_seconds=max_retry_after_seconds,
    )
    metadata = _slack_response_metadata(
        response,
        response_class=response_class,
        retry_after_seconds=bounded_retry_after,
    )

    reason: str | None = None
    if outcome_state == FAILED_RETRYABLE and network_attempt_count >= max_network_attempts:
        # Chapter 04 escalates rather than leaving work scheduled forever. The
        # record keeps the response class that exhausted the budget so an
        # operator can see what kept failing.
        metadata["attempts_exhausted"] = True
        outcome_state = FAILED_TERMINAL
        reason = "retryable responses exhausted the network-attempt budget"

    next_action_at: int | None = None
    if outcome_state == FAILED_RETRYABLE:
        next_action_at = now_ts + (
            bounded_retry_after
            if bounded_retry_after is not None
            else _retry_delay(
                network_attempt_count,
                min_interval_seconds=min_interval_seconds,
                max_retry_after_seconds=max_retry_after_seconds,
                candidate_id=candidate,
            )
        )

    # A TTL goes on resolved work only. `delivery_unknown` is resolved as far
    # as this worker is concerned but not as far as the operator is: ADR-004
    # requires review and a recorded replay decision, so the record must not
    # expire underneath that. `failed_retryable` is outstanding work and the
    # dispatcher still needs it.
    expires_at = ttl_at if outcome_state in (POSTED, FAILED_TERMINAL) else None

    recorded = store.record_outcome(
        candidate,
        expected_state_version=claimed_version,
        attempt_id=attempt_id,
        status=outcome_state,
        network_attempt_count=network_attempt_count,
        next_action_at=next_action_at,
        slack_response=metadata,
        expires_at=expires_at,
    )

    # Pacing advances whether or not the state write won. It records that this
    # destination was called, which is true regardless of who owns the record
    # now; skipping it on a lost write would let the next message call Slack
    # inside an interval this call already consumed. The update is itself
    # conditional on the observed pace version, so a worker that also advanced
    # it simply wins here and this one is a no-op.
    _advance_pace(
        store,
        record.destination_key,
        pace,
        now_ts,
        min_interval_seconds,
        bounded_retry_after,
        response_class,
    )

    if not recorded:
        return _lost_outcome_write(store, candidate, metrics, performed_network_call=True)

    _OUTCOME_METRIC[outcome_state](metrics)
    return WorkerResult(handled=True, state=outcome_state, performed_network_call=True, reason=reason)
