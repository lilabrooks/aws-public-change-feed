"""AWS-backed `CredentialReader` adapters, one per accepted secret store.

Chapter 02 provisions "Secrets Manager secrets or SecureString parameters
referenced by exact identifier", chapter 05 requires secrets to "use Secrets
Manager or SSM SecureString with their configured encryption", and
`deployment.schema.json` accepts both `secrets_manager` and
`ssm_parameter_store`. `infra/central` already branches on that value for both
the container and the IAM action, so both stores are deployable today and both
get an adapter here. No accepted decision defers either one.

Which store a deployment uses is a composition-root choice, so neither the
`CredentialReader` port nor the worker names a store: the worker asks for an
identifier and receives a `SlackCredential`.

Three properties are load-bearing and each is enforced here rather than trusted
from a caller.

The stored content is the credential, whole. Nothing in the repository defines a
structured stored-secret format — no schema, no example, no validator rule — so
reading one key out of a JSON document would be inventing a contract as a side
effect of writing an adapter. Surrounding whitespace is removed and the
remainder is opaque. That single normalization is not a format: a trailing
newline is what `aws ssm put-parameter` and a shell heredoc leave behind, and
without it a correct webhook URL fails `validate_uri_spelling` and a correct bot
token becomes an invalid HTTP header value, both reported as a malformed
credential the operator can see nothing wrong with.

The kind is configured, never sniffed. An adapter is constructed for the
delivery mode the release declares, and it labels every value it returns with
that mode. Guessing from the value's shape would make a webhook URL stored in
the bot-token container look like a webhook credential, and the worker's
`secret.kind != delivery_mode` check — the one that stops a token being posted
to a hook endpoint — would then agree with the mistake instead of catching it.

Errors carry a condition, not a payload. Chapter 05 excludes secret values,
webhook URLs, tokens, provider messages, and response bodies from durable
errors and logs, and permits "bounded error codes". Every raise below is built
from the store name, the condition, and at most the AWS error code; the
identifier is omitted because the worker's own reason already names the route,
and no branch interpolates a value, a message, or a body.
"""

from __future__ import annotations

from typing import Any

from .credentials import (
    BOT_TOKEN,
    WEBHOOK,
    CredentialAccessDenied,
    CredentialEmpty,
    CredentialError,
    CredentialNotFound,
    CredentialUnavailable,
    CredentialUnreadable,
    SlackCredential,
)

__all__ = [
    "AwsClient",
    "MAX_ERROR_CODE_CHARACTERS",
    "SECRETS_MANAGER",
    "SECURE_STRING",
    "SSM_PARAMETER_STORE",
    "SecretsManagerCredentialReader",
    "SsmParameterCredentialReader",
    "UNKNOWN_ERROR_CODE",
    "credential_reader_for",
]

SECRETS_MANAGER = "secrets_manager"
SSM_PARAMETER_STORE = "ssm_parameter_store"

_CONFIGURED_KINDS = frozenset({WEBHOOK, BOT_TOKEN})

# Chapter 02 bounds a configured identifier at 512 characters in both the
# deployment and inventory schemas. A longer one cannot have come from a
# validated release, so it is refused before an API call rather than sent.
MAX_SECRET_ID_CHARACTERS = 512

# A Slack bot token and a webhook URL are both far below this. The bound exists
# so a container holding something else entirely — a pasted certificate, a
# whole JSON document — is refused as unreadable instead of being carried into
# a header or a URL validator.
MAX_CREDENTIAL_CHARACTERS = 8192

# An AWS error code is a short CamelCase identifier. The bound is enforced
# rather than described: an unbounded provider string would reach a durable
# response class and an operator's log, which is exactly what chapter 05's
# "bounded error codes" excludes.
MAX_ERROR_CODE_CHARACTERS = 64
UNKNOWN_ERROR_CODE = "Unknown"

# `botocore` raises one exception class and distinguishes conditions by an error
# code in the response, so the mapping is by code.
#
# These two sets are the *permanent* allowlist, and being an allowlist is the
# design. A code nobody has reviewed is transient, so an unfamiliar failure
# preserves the delivery on a bounded delay instead of discarding it. Getting
# that wrong in the transient direction leaves work scheduled; getting it wrong
# in the permanent direction destroys an alert, and only the second is
# unrecoverable.
_NOT_FOUND_CODES = frozenset(
    {
        "ResourceNotFoundException",
        "ParameterNotFound",
        "ParameterVersionNotFound",
    }
)
_DENIED_CODES = frozenset(
    {
        "AccessDeniedException",
        "AccessDenied",
        "UnauthorizedOperation",
        "KMSAccessDeniedException",
        "AccessDeniedError",
        "DecryptionFailure",
        "InvalidKeyId",
        "KMSInvalidStateException",
        "ValidationException",
        "InvalidParameterException",
        "InvalidRequestException",
        "InvalidResourceException",
        "InvalidResourceStateException",
    }
)

# SSM stores three parameter types and only one is encrypted. Chapter 05 requires
# a SecureString with its configured encryption, so a `String` or `StringList`
# holding a Slack credential is a container that was never protected — a
# permanent configuration fault, not a value to use because it happened to parse.
SECURE_STRING = "SecureString"


# A boto3 client is generated at runtime from a service model and has no static
# type without `boto3-stubs`, which this repository does not pin. `Any` says that
# plainly; the operation each adapter needs is checked in its constructor, which
# is a real check rather than a type that only looks like one.
AwsClient = Any


def _error_code(error: Exception) -> str:
    """The bounded AWS error code, or `Unknown`. Never the message.

    `botocore.exceptions.ClientError` carries `response["Error"]["Code"]`. It is
    read defensively rather than by type, so this module does not import
    `botocore` and the adapters stay unit-testable against a fake client that
    raises a stand-in.

    Bounded means enforced. The returned value reaches a durable response class
    and an operator's log, so anything that is not a short ASCII alphanumeric
    identifier — oversized, punctuated, empty, or not a string — becomes
    `Unknown` rather than being carried through at whatever length the provider
    chose. `Unknown` is also the classifier's transient default, so a code this
    rejects preserves the delivery.

    `botocore` also raises connection and timeout errors that carry no response
    at all; those reach here as `Unknown` and are transient, which is correct.
    """

    response = getattr(error, "response", None)
    if isinstance(response, dict):
        detail = response.get("Error")
        if isinstance(detail, dict):
            code = detail.get("Code")
            if (
                isinstance(code, str)
                and 0 < len(code) <= MAX_ERROR_CODE_CHARACTERS
                and code.isascii()
                and code.isalnum()
            ):
                return code
    return UNKNOWN_ERROR_CODE


class _BaseReader:
    """Shared construction checks and value normalization.

    Constructor validation runs before any AWS call, so a misconfigured
    composition root fails at wiring time rather than on the first delivery
    with a credential-shaped error the operator has to trace back.
    """

    store: str = ""
    _client_method: str = ""

    def __init__(self, client: AwsClient, *, kind: str) -> None:
        if client is None:
            raise ValueError(f"{self.store} credential reader needs a client")
        if kind not in _CONFIGURED_KINDS:
            raise ValueError(f"credential kind must be one of {sorted(_CONFIGURED_KINDS)}, not {kind!r}")
        if not callable(getattr(client, self._client_method, None)):
            # Checked here so a swapped client surfaces as a wiring error
            # instead of an `AttributeError` mid-delivery, where the worker
            # would classify it through the generic read-failure path.
            raise ValueError(f"{self.store} credential reader needs a client exposing {self._client_method}()")
        self._client = client
        self._kind = kind

    @property
    def kind(self) -> str:
        return self._kind

    def _require_identifier(self, secret_id: str) -> str:
        if not isinstance(secret_id, str) or not secret_id.strip():
            raise CredentialNotFound(f"{self.store}: no credential identifier was supplied")
        if len(secret_id) > MAX_SECRET_ID_CHARACTERS:
            raise CredentialNotFound(f"{self.store}: credential identifier exceeds the configured bound")
        return secret_id

    def _credential(self, value: object) -> SlackCredential:
        """Turn a stored value into a labelled credential, or refuse it."""

        if value is None:
            raise CredentialEmpty(f"{self.store}: the credential container holds no text value")
        if not isinstance(value, str):
            # A Secrets Manager binary secret arrives as `SecretBinary` and
            # never as `SecretString`, and an SSM `StringList` decodes to a
            # string. Anything non-string here is a container this adapter
            # cannot interpret, and guessing an encoding would be inventing a
            # format.
            raise CredentialUnreadable(f"{self.store}: the credential value is not text")
        normalized = value.strip()
        if not normalized:
            raise CredentialEmpty(f"{self.store}: the credential container holds an empty value")
        if len(normalized) > MAX_CREDENTIAL_CHARACTERS:
            raise CredentialUnreadable(f"{self.store}: the credential value exceeds the supported bound")
        if any(character in normalized for character in "\r\n"):
            # A newline inside the value survives `strip` and would split an
            # HTTP header or a request line. Refusing it here is the only place
            # that covers both modes.
            raise CredentialUnreadable(f"{self.store}: the credential value contains a line break")
        return SlackCredential(self._kind, normalized)

    def _mapped_error(self, error: Exception) -> CredentialError:
        """Build the port's error for an SDK failure. Returns rather than raises.

        Returning matters. Raising inside an `except` block sets `__context__`
        on the new exception even with `from None`, which only sets
        `__suppress_context__` and so only stops the *standard* traceback
        renderer from printing it. The provider exception, whose message can
        quote the request and the container, would still hang off the object for
        anything that walks the chain — a structured log formatter, an error
        reporter, a test helper. The caller raises this outside the handler, so
        `__cause__` and `__context__` are both empty and there is nothing to
        walk.

        The two permanent sets are consulted first and everything else is
        transient. That order is the policy: only a reviewed code may destroy
        deliverable work, and an unclassified failure — including one carrying no
        response at all, which is what a connection error or a read timeout looks
        like here — preserves it for a bounded retry.
        """

        code = _error_code(error)
        if code in _NOT_FOUND_CODES:
            return CredentialNotFound(f"{self.store}: no credential exists for the configured identifier")
        if code in _DENIED_CODES:
            return CredentialAccessDenied(f"{self.store}: reading the configured identifier was denied")
        return CredentialUnavailable(f"{self.store}: read did not complete, error code {code}")


class SecretsManagerCredentialReader(_BaseReader):
    """Reads a Slack credential from Secrets Manager `GetSecretValue`.

    The exact identifier the worker passes is sent as `SecretId`; no prefix is
    added and no version or stage is selected, so the container's current value
    is what a rotation makes live.
    """

    store = SECRETS_MANAGER
    _client_method = "get_secret_value"

    def read(self, secret_id: str) -> SlackCredential:
        identifier = self._require_identifier(secret_id)
        failure: CredentialError | None = None
        response: Any = None
        try:
            response = self._client.get_secret_value(SecretId=identifier)
        except Exception as error:  # noqa: BLE001 - mapped to the port's taxonomy
            failure = self._mapped_error(error)
        # Raised outside the handler so no provider exception is attached.
        if failure is not None:
            raise failure
        if not isinstance(response, dict):
            raise CredentialUnreadable(f"{self.store}: the response was not a mapping")
        if "SecretString" not in response and "SecretBinary" in response:
            # Named separately from the generic non-text path because the fix
            # is specific: the container was created as a binary secret and has
            # to be rewritten as a string.
            raise CredentialUnreadable(f"{self.store}: the credential is stored as binary and cannot be used")
        return self._credential(response.get("SecretString"))


class SsmParameterCredentialReader(_BaseReader):
    """Reads a Slack credential from an SSM `SecureString`, decrypted.

    `WithDecryption=True` is always sent. Chapter 05 requires a SecureString
    with its configured encryption, and without decryption the call returns the
    ciphertext, which would reach the webhook validator or an authorization
    header as an opaque wrong value rather than as a read failure.

    The returned `Type` is then required to be `SecureString`. Requesting
    decryption does not get it: SSM ignores `WithDecryption` for a plaintext
    `String` or `StringList` and returns the value happily, so without this check
    a Slack credential stored unencrypted reads back correctly and posts, and the
    encryption chapter 05 requires is silently absent. The parameter's own `Type`
    is the independent fact, so it is what decides.
    """

    store = SSM_PARAMETER_STORE
    _client_method = "get_parameter"

    def read(self, secret_id: str) -> SlackCredential:
        identifier = self._require_identifier(secret_id)
        failure: CredentialError | None = None
        response: Any = None
        try:
            response = self._client.get_parameter(Name=identifier, WithDecryption=True)
        except Exception as error:  # noqa: BLE001 - mapped to the port's taxonomy
            failure = self._mapped_error(error)
        # Raised outside the handler so no provider exception is attached.
        if failure is not None:
            raise failure
        if not isinstance(response, dict):
            raise CredentialUnreadable(f"{self.store}: the response was not a mapping")
        parameter = response.get("Parameter")
        if not isinstance(parameter, dict):
            raise CredentialUnreadable(f"{self.store}: the response carried no parameter")
        parameter_type = parameter.get("Type")
        if parameter_type != SECURE_STRING:
            # Permanent: an operator has to recreate the parameter as a
            # SecureString. The observed type is named because it is deployment
            # metadata rather than credential content, and it is the one fact
            # that tells the operator what to change.
            observed = parameter_type if isinstance(parameter_type, str) and parameter_type.isalnum() else "<missing>"
            raise CredentialUnreadable(
                f"{self.store}: the credential parameter must be a {SECURE_STRING}, not {observed}"
            )
        return self._credential(parameter.get("Value"))


def credential_reader_for(store: str, client: AwsClient, *, kind: str) -> _BaseReader:
    """Build the reader for a deployment's `secret_store` value.

    The mapping lives here rather than in the future composition root so that
    adding a store means adding an adapter and one entry, and so the accepted
    enum and the implemented set can be compared in one place by a test.
    """

    readers = {
        SECRETS_MANAGER: SecretsManagerCredentialReader,
        SSM_PARAMETER_STORE: SsmParameterCredentialReader,
    }
    reader = readers.get(store)
    if reader is None:
        raise ValueError(f"unsupported secret_store: {store!r}")
    return reader(client, kind=kind)
