from fastapi import FastAPI

from models import (
    TranslationRequest,
    TranslationResponse
)

from services.translator import translate_text

from logger import get_logger

logger = get_logger("main")

app = FastAPI(
    title="BhashaSub",
    description="AI Translation Service",
    version="1.0.0"
)


@app.get("/")
async def root():

    logger.info("Root endpoint called")

    return {
        "message": "BhashaSub API Running"
    }


@app.get("/health")
async def health():

    logger.info("Health check called")

    return {
        "status": "healthy"
    }


@app.post(
    "/translate",
    response_model=TranslationResponse
)
async def translate(
    request: TranslationRequest
):

    logger.info("Translation endpoint called")

    translated_text = await translate_text(
        request.text
    )

    return TranslationResponse(
        translation=translated_text
    )