import os
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from logger import get_logger
from models import ErrorResponse
from models import TranslationRequest
from models import TranslationResponse
from services.translator import TranslationClientError
from services.translator import TranslationUnavailableError
from services.translator import redact
from services.translator import translate_text

logger = get_logger("api")

app = FastAPI(
    title="BhashaSub"
)

app.mount(
    "/static",
    StaticFiles(directory="static"),
    name="static"
)

templates = Jinja2Templates(
    directory="templates"
)


def _asset_version() -> str:
    """Cache-busting token derived from the newest static file.

    StaticFiles sends an ETag but no Cache-Control, so browsers fall back to
    heuristic caching and can serve a stale stylesheet after a deploy. Putting
    the token in the asset URL makes a changed file a different URL.
    """
    newest = 0.0
    for root, _, files in os.walk("static"):
        for name in files:
            newest = max(newest, os.path.getmtime(os.path.join(root, name)))
    return str(int(newest))


ASSET_VERSION = _asset_version()


def request_id_of(request: Request) -> str:
    return getattr(request.state, "request_id", "-")


def error_response(request: Request, status_code: int, message: str) -> JSONResponse:
    """Build the single error shape used by every failure path.

    `message` must always be a string this application authored. Upstream
    and exception text never reaches the client.
    """
    request_id = request_id_of(request)

    return JSONResponse(
        status_code=status_code,
        content={"error": message, "request_id": request_id},
        headers={"X-Request-ID": request_id},
    )


@app.middleware("http")
async def attach_request_id(request: Request, call_next):
    request.state.request_id = uuid.uuid4().hex[:12]

    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id

    return response


@app.exception_handler(RequestValidationError)
async def handle_invalid_request(request: Request, exc: RequestValidationError):
    # The default handler echoes the offending input back to the caller.
    # Only the field names are logged, never the values.
    fields = [".".join(str(part) for part in error.get("loc", ())) for error in exc.errors()]

    logger.info(
        f"invalid_request request_id={request_id_of(request)} fields={','.join(fields) or '-'}"
    )

    return error_response(
        request,
        400,
        "Invalid request. Provide non-empty text within the allowed size limit.",
    )


@app.exception_handler(TranslationClientError)
async def handle_client_error(request: Request, exc: TranslationClientError):
    logger.info(f"client_error request_id={request_id_of(request)}")

    return error_response(request, 400, redact(str(exc)))


@app.exception_handler(TranslationUnavailableError)
async def handle_unavailable(request: Request, exc: TranslationUnavailableError):
    logger.warning(f"service_unavailable request_id={request_id_of(request)}")

    return error_response(request, 503, "Translation service temporarily unavailable")


@app.exception_handler(Exception)
async def handle_unexpected(request: Request, exc: Exception):
    # Type and redacted detail server-side; nothing specific to the caller.
    logger.error(
        f"unhandled_error request_id={request_id_of(request)} "
        f"type={type(exc).__name__} detail={redact(str(exc))}"
    )

    return error_response(request, 500, "Internal server error")


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"asset_version": ASSET_VERSION},
    )


@app.post(
    "/translate",
    response_model=TranslationResponse,
    responses={
        400: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
async def translate(
    payload: TranslationRequest,
    request: Request,
):

    translated = await translate_text(
        payload.text,
        request_id=request_id_of(request),
    )

    return {
        "translation": translated
    }
