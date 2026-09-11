# Redstone AI gateway

The AI layer gives the rest of Redstone one way to reach a language model,
independent of which provider is underneath. The future coding agent will depend
only on `AIGateway` and the neutral request/response types — never on Gemini,
OpenAI, Groq or httpx.

> **Status: Phase 2B.** Implemented and tested against mocked HTTP. No live
> provider call is made anywhere in the test suite, and no real key exists in
> the repository.

## Shape

```
caller ──► AIGateway.generate(AIRequest, provider?, model?, byok_key?, base_url?)
                │
                ├─ resolve credential   (BYOK request key ▸ else server key)
                ├─ resolve + validate base URL (SSRF checks; BYOK host allowlist)
                ├─ request-size guard
                ├─ registry.get_provider(name)          ← fixed allowlist
                └─ retry loop (bounded, jittered)
                        │
                        └─ AIProvider.generate(...)
                                └─ post_json(...)   ← timeout, no redirects,
                                                       response-size cap
                │
                ▼
        AIResponse         or raises RedstoneAIError
```

Only `AIGateway` and the neutral types are exported from `redstone.ai`. Provider
classes are not, so nothing downstream can grow a dependency on a specific one.

## Providers

| Name | Adapter | Auth | Endpoint |
|---|---|---|---|
| `gemini` | `GeminiProvider` | `x-goog-api-key` header | `…/models/{model}:generateContent` |
| `openai-compatible` | `OpenAICompatibleProvider` | `Authorization: Bearer` | `{base_url}/chat/completions` |

`openai-compatible` is driven entirely by `(base_url, model, key)`, so Groq,
OpenRouter and any other OpenAI-shaped API are configuration, not code. Provider
names resolve from a fixed dict — there is no dynamic import, so a name can
never cause an arbitrary module to load.

## Credential modes

Two modes, never mixed silently:

- **Server-configured** — `AI_API_KEY` from the environment, held in `AIConfig`.
- **Request BYOK** — a key supplied on a single `generate()` call. It takes
  precedence over the server key and exists only for that call.

A BYOK request may target only providers in `BYOK_PROVIDERS` and, for
`openai-compatible`, only hosts in `BYOK_ALLOWED_HOSTS`.

## Credential lifecycle

```
request ──► Credential(value, REQUEST_BYOK) ──► provider Authorization header
                                                        │
                                                 request completes
                                                        │
                                          Credential goes out of scope
```

The key is wrapped in `Credential`, which:
- masks its value in `repr`/`str` (`value=<hidden>`),
- refuses to pickle, copy or deep-copy (`TypeError`),
- disables equality/hashing so it cannot become a revealing dict key,
- exposes the raw value only through an explicit `reveal()`, called at exactly
  one place per adapter — building the auth header.

It is **never** written to the workspace, a snapshot, a changeset, an event, a
log, a URL, a response, or any persistent store. There is no credential store.

## Configuration

| Variable | Meaning | Default |
|---|---|---|
| `AI_PROVIDER` | `gemini` or `openai-compatible` | `gemini` |
| `AI_API_KEY` | server-configured key | *(unset)* |
| `AI_MODEL` | model id | `gemini-2.0-flash` |
| `AI_BASE_URL` | override endpoint (validated) | provider default |
| `AI_REQUEST_TIMEOUT` | per-request timeout, seconds | 60 |
| `AI_MAX_RETRIES` | retries after the first attempt | 2 |
| `MAX_AI_REQUEST_SIZE` | prompt payload ceiling, bytes | 1 MiB |
| `MAX_AI_RESPONSE_SIZE` | provider response ceiling, bytes | 8 MiB |

## Retry behaviour

Bounded: `1 + max_retries` attempts. Exponential backoff with full jitter.
Retried only on `RATE_LIMITED`, `TIMEOUT`, `NETWORK_ERROR`,
`PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`. **Never** retried on authentication,
forbidden, invalid-request, invalid-model or too-large — retrying those only
burns the user's quota.

## Error normalisation

Every failure becomes a `RedstoneAIError` with a code from a fixed allowlist
(`errors.AIErrorCode`) and a Redstone-authored `safe_message`. Upstream status
codes, provider response bodies and exception strings never reach the caller.
`to_dict()` yields `{error_code, message, retryable, request_id}`.

## SSRF protection

`ssrf.validate_base_url` requires `https`, rejects credentials in the URL, and
rejects any host that is or resolves to a private, loopback, link-local (incl.
`169.254.169.254`), reserved, multicast or unspecified address. `localhost` and
IP literals are rejected without DNS. Redirects are disabled on the request, so
an auth header can never follow a 3xx to another host. BYOK additionally
requires the host be in `BYOK_ALLOWED_HOSTS`.

## Limits

- **Request size** — checked before any network call; oversized requests never
  reach a provider.
- **Response size** — read incrementally and aborted past the cap, so a hostile
  or broken provider cannot exhaust memory.
- **Timeout** — every request has one; there is no unbounded call.

## Testing

`tests/redstone/test_ai_gateway.py` (63 tests) uses `httpx.MockTransport`: the
real request-building, serialisation and header code runs, nothing leaves the
process, and a fake secret (`TEST_SECRET_…`) is asserted to appear only in the
upstream auth header — not in the URL, body, response, error, logs, or any
retained gateway state.
