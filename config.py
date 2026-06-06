from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    gemini_api_key: str

    max_retries: int = 3
    timeout_seconds: int = 30

    class Config:
        env_file = ".env"


settings = Settings()