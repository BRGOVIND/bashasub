# BhashaSub
https://bashasub.onrender.com

Built BhashaSub as an educational backend project to help students understand how modern AI APIs are integrated into real-world applications. It demonstrates the complete flow of a FastAPI service—from accepting user input and validating requests to communicating with the Gemini API, handling rate limits with retry logic, and returning translated text through a simple web interface. The goal was to make concepts like asynchronous API calls, environment-based configuration, structured logging, and production-ready backend patterns easier to learn through a working example.

It works if someone provides a valid Gemini API key 😶😶

## Features

- FastAPI REST API
- Integration with the Gemini API
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

```bash
pip install -r requirements.txt

uvicorn main:app --reload
