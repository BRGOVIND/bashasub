"""Typed AI request, response and credential.

The credential is the sensitive part. It is a small wrapper whose only job is
to hold a key value while making it as hard as possible for that value to leak
by accident: its repr/str are masked, it refuses to pickle or copy, and the raw
value is reachable only through an explicit ``reveal()`` call at the one place
that needs it — the provider's Authorization header.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = ["CredentialSource", "Credential", "AIMessage", "AIRequest", "AIResponse"]


class CredentialSource(str, Enum):
    """Where a credential came from. The two modes are never mixed silently."""

    SERVER_CONFIGURED = "server_configured"   # from AI_API_KEY
    REQUEST_BYOK = "request_byok"             # supplied on this request only


class Credential:
    """A provider API key that resists leaking.

    Not a dataclass on purpose: dataclasses generate a repr that prints every
    field. Here repr/str are masked, equality and hashing are disabled (so it
    cannot become a dict key that shows up in a dump), and serialisation is
    refused so it can never be pickled into a snapshot, a cache or a log.
    """

    __slots__ = ("_value", "source")

    def __init__(self, value: str, source: CredentialSource) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("credential value must be a non-empty string")
        object.__setattr__(self, "_value", value)
        object.__setattr__(self, "source", source)

    def reveal(self) -> str:
        """Return the raw key. Call only where the provider request needs it."""
        return self._value

    @property
    def is_byok(self) -> bool:
        return self.source is CredentialSource.REQUEST_BYOK

    def __repr__(self) -> str:
        return f"Credential(source={self.source.value}, value=<hidden>)"

    __str__ = __repr__

    # Refuse the ways a value silently escapes.
    def __reduce__(self):
        raise TypeError("Credential is not serialisable")

    def __getstate__(self):
        raise TypeError("Credential is not serialisable")

    def __copy__(self):
        raise TypeError("Credential is not copyable")

    def __deepcopy__(self, memo):
        raise TypeError("Credential is not copyable")

    def __eq__(self, other):
        return self is other

    __hash__ = None


@dataclass(frozen=True, slots=True)
class AIMessage:
    role: str      # "system" | "user" | "assistant"
    content: str


@dataclass(frozen=True, slots=True)
class AIRequest:
    """A provider-independent generation request.

    Provider-specific translation happens inside each adapter; nothing
    provider-shaped leaks into this type. There is deliberately no field for
    arbitrary passthrough options, so a caller cannot smuggle a raw base_url or
    auth override through the gateway.
    """

    messages: tuple[AIMessage, ...]
    model: str | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    request_id: str = "-"

    @classmethod
    def from_prompt(cls, prompt: str, *, system: str | None = None, **kwargs) -> AIRequest:
        messages = []
        if system:
            messages.append(AIMessage("system", system))
        messages.append(AIMessage("user", prompt))
        return cls(messages=tuple(messages), **kwargs)

    def approximate_size(self) -> int:
        """Byte size of the message content, for the request-size guard."""
        return sum(len(m.content.encode("utf-8")) for m in self.messages)


@dataclass(frozen=True, slots=True)
class AIResponse:
    """A normalised provider response.

    Usage fields are populated only when the provider actually reports them;
    they are never fabricated. There is no field that could carry the raw
    provider body or the credential.
    """

    text: str
    provider: str
    model: str
    request_id: str = "-"
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    finish_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "provider": self.provider,
            "model": self.model,
            "request_id": self.request_id,
            "usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.total_tokens,
            },
            "finish_reason": self.finish_reason,
        }
