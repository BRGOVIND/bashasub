"""Redstone AI layer: a provider-agnostic, BYOK-capable, secret-safe gateway.

The rest of Redstone depends only on :class:`AIGateway` and the neutral
request/response/error types. Provider adapters are an implementation detail and
are not exported here, so a caller cannot come to depend on Gemini or
OpenAI-compatible directly.
"""

from .errors import AIErrorCode, RedstoneAIError
from .gateway import AIGateway
from .models import (
    AIMessage,
    AIRequest,
    AIResponse,
    Credential,
    CredentialSource,
)

__all__ = [
    "AIGateway",
    "AIRequest",
    "AIResponse",
    "AIMessage",
    "Credential",
    "CredentialSource",
    "AIErrorCode",
    "RedstoneAIError",
]
