# BhashaSub

An async FastAPI-based AI translation service built with Python.

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
