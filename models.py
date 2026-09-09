from pydantic import BaseModel
from pydantic import Field

from config import settings


class TranslationRequest(BaseModel):
    # Bounded at the edge so oversized or empty bodies are rejected before
    # any upstream call is made.
    text: str = Field(min_length=1, max_length=settings.max_text_chars)


class TranslationResponse(BaseModel):
    translation: str


class ErrorResponse(BaseModel):
    """Every failure response uses this shape. It never carries internals."""

    error: str
    request_id: str
