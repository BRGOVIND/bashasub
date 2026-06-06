# BhashaSub
https://bashasub.onrender.com

Built BhashaSub as a backend engineering project to learn production Python patterns. It's a FastAPI service that takes text input, calls the Gemini API to translate it into Indian languages, and returns the result through a browser UI. I focused on things like async communication, retry logic for rate-limited APIs, structured logging, and Pydantic validation — essentially the same patterns used in production AI pipelines.
It works if someone pays for API keys 😶😶

## Features

- FastAPI REST API
- Async HTTP requests using HTTPX
- Environment-based configuration
- Structured logging
- Retry logic using Tenacity
- Pydantic request validation
- Swagger/OpenAPI documentation
- Pytest testing

## Tech Stack

- Python
- FastAPI
- HTTPX
- Pydantic
- Tenacity
- Pytest

## Run Locally

pip install -r requirements.txt

uvicorn main:app --reload

Open:

http://127.0.0.1:8000/docs
