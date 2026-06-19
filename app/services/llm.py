"""LLM wrapper for email generation via OpenAI Chat Completions.

Pattern follows tuvaLLM: sync OpenAI client, single-turn chat completion.
"""

import logging
import time

from openai import OpenAI

from app.config import settings

log = logging.getLogger(__name__)

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=settings.openai_api_key)
    return _client


class LLMError(Exception):
    """Raised when the LLM call fails or returns unusable output."""


def generate_email(
    system_prompt: str,
    user_prompt: str,
    timeout: float | None = None,
) -> tuple[str, str, int]:
    """Call the LLM and return (decision, body_text, duration_ms).

    decision is "SEND" or "SKIP".
    body_text is the generated email body for a SEND. For a SKIP it is whatever
    rationale text (if any) the model emitted after the DECISION line and is not
    used by the caller (a skipped lead produces no email).
    duration_ms is wall-clock time for the API call.

    Raises LLMError on failure (no API key, empty output, or an unusably short
    SEND body).
    """
    if not settings.openai_api_key:
        raise LLMError("No OPENAI_API_KEY configured")

    client = _get_client()
    model = settings.openai_model
    t0 = time.monotonic()

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_completion_tokens=600,
            timeout=timeout or settings.openai_timeout,
        )
    except Exception as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        log.error("LLM call failed after %dms: %s", duration_ms, exc)
        raise LLMError(f"OpenAI API error: {exc}") from exc

    duration_ms = int((time.monotonic() - t0) * 1000)
    raw = (response.choices[0].message.content or "").strip()

    if not raw:
        raise LLMError("LLM returned empty output")

    # Parse the DECISION line BEFORE judging length. A terse but perfectly valid
    # "DECISION: SKIP" (14 chars) is the correct output when the model decides
    # not to email this lead. Judging the whole response against a length floor
    # first would reject it as "unusably short" and raise — and the caller
    # (personalize_outbound -> process_outbound_message) treats that raise as a
    # generation failure and FALLS BACK to a Jinja2 email, sending the very
    # repeat customer the model chose to skip. So decide first, length-check the
    # body second.
    decision = "SEND"
    body = raw
    if raw.upper().startswith("DECISION:"):
        first_line, _, rest = raw.partition("\n")
        token = first_line.split(":", 1)[1].strip().upper()
        if token in ("SEND", "SKIP"):
            decision = token
            body = rest.strip()
        else:
            log.warning("Unrecognised DECISION token %r — defaulting to SEND", token)

    # Only a SEND becomes an email, so only a SEND needs a usable body. This
    # guards against a truncated/garbage generation that would otherwise be
    # emailed verbatim; a SKIP carries no email body and is exempt. The check is
    # on `body` (the email text), not `raw` — the "DECISION: SEND\n" prefix is
    # not part of what gets sent.
    if decision == "SEND" and len(body) < 20:
        raise LLMError(f"LLM returned unusably short SEND body ({len(body)} chars)")

    log.info("LLM call: model=%s, decision=%s, duration=%dms, body_len=%d",
             model, decision, duration_ms, len(body))
    return decision, body, duration_ms
