from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from models import TranslationRequest
from services.translator import translate_text

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


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):

    return templates.TemplateResponse(
        request=request,
        name="index.html"
    )


@app.post("/translate")
async def translate(
    request: TranslationRequest
):

    translated = await translate_text(
        request.text
    )

    return {
        "translation": translated
    }