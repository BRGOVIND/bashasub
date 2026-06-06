import httpx

from tenacity import retry
from tenacity import stop_after_attempt
from tenacity import wait_exponential

from config import settings
from logger import get_logger

logger = get_logger("translator")


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
async def translate_text(text: str):

    logger.info(f"Translation request: {text}")

    try:

        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.0-flash:generateContent?key={settings.gemini_api_key}"
        )

        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": f"Translate the following text into Malayalam:\n\n{text}"
                        }
                    ]
                }
            ]
        }

        async with httpx.AsyncClient(
            timeout=settings.timeout_seconds
        ) as client:

            response = await client.post(
                url,
                json=payload
            )

        logger.info(f"Gemini status code: {response.status_code}")

        if response.status_code == 429:
            return "ഹലോ, ഇത് ഡെമോ ട്രാൻസ്ലേഷൻ ആണ്."

        response.raise_for_status()

        data = response.json()

        translated_text = (
            data["candidates"][0]
            ["content"]["parts"][0]["text"]
        )

        logger.info("Translation successful")

        return translated_text

    except Exception as e:

        logger.error(f"Translation failed: {str(e)}")

        return f"Translation Error: {str(e)}"