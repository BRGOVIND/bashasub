from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Supplied via environment (Render dashboard) or a local .env file.
    # Never hardcode this and never send it anywhere except the
    # x-goog-api-key request header.
    gemini_api_key: str

    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_model: str = "gemini-2.0-flash"

    max_retries: int = 3
    timeout_seconds: int = 30
    max_text_chars: int = 20000

    # Opt-in debug switch. When true, translation input is written to the
    # application log. Leave false in production: subtitle and message text
    # is user content and does not belong in normal operational logs.
    # Enabling this never causes credentials to be logged.
    log_request_content: bool = False

    class Config:
        env_file = ".env"


settings = Settings()
