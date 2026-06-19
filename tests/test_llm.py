"""Tests for the LLM wrapper (app/services/llm.py).

generate_email was previously untested — the personalization tests mock it out
entirely (e.g. returning ("SKIP", "", 800)), so the real DECISION-line parsing
and length validation had no coverage. These tests exercise generate_email
directly with a fake OpenAI client (no network, no API key), so they pin the
parse + the SEND/SKIP length contract.
"""

import types

import pytest

import app.services.llm as llm
from app.config import settings
from app.services.llm import LLMError, generate_email


def _fake_client(content=None, *, raises=None):
    """A stand-in for the OpenAI client: client.chat.completions.create(...)
    returns an object shaped like a chat completion (choices[0].message.content),
    or raises if `raises` is given."""

    def create(**_kwargs):
        if raises is not None:
            raise raises
        message = types.SimpleNamespace(content=content)
        choice = types.SimpleNamespace(message=message)
        return types.SimpleNamespace(choices=[choice])

    completions = types.SimpleNamespace(create=create)
    chat = types.SimpleNamespace(completions=completions)
    return types.SimpleNamespace(chat=chat)


@pytest.fixture
def llm_key(monkeypatch):
    """generate_email refuses to run without an API key; supply a dummy one."""
    monkeypatch.setattr(settings, "openai_api_key", "test-openai-key")


def _patch_client(monkeypatch, **kwargs):
    monkeypatch.setattr(llm, "_get_client", lambda: _fake_client(**kwargs))


# ── Decision parsing ─────────────────────────────────────────────────────────

class TestDecisionParsing:
    def test_terse_skip_is_not_rejected_as_short(self, monkeypatch, llm_key):
        """Regression: a valid terse "DECISION: SKIP" (14 chars) must parse as a
        SKIP, not raise "unusably short".

        Before the fix the 20-char length floor ran *before* the DECISION line
        was parsed, so this raised LLMError. personalize_outbound has no guard
        around generate_email, so the raise propagated to
        process_outbound_message, whose except-clause falls back to a Jinja2
        template and SENDS the email — to the exact repeat customer the model
        chose to skip. The skip was silently defeated.
        """
        _patch_client(monkeypatch, content="DECISION: SKIP")
        decision, body, duration_ms = generate_email("sys", "usr")
        assert decision == "SKIP"
        assert body == ""
        assert duration_ms >= 0

    def test_skip_with_rationale_text(self, monkeypatch, llm_key):
        _patch_client(
            monkeypatch,
            content="DECISION: SKIP\nSame request resubmitted today; no new information.",
        )
        decision, body, _ = generate_email("sys", "usr")
        assert decision == "SKIP"
        assert "resubmitted" in body

    def test_send_strips_decision_line_from_body(self, monkeypatch, llm_key):
        content = (
            "DECISION: SEND\n"
            "Thanks for reaching out about your water heater — we can get someone "
            "out to you this week."
        )
        _patch_client(monkeypatch, content=content)
        decision, body, _ = generate_email("sys", "usr")
        assert decision == "SEND"
        assert body.startswith("Thanks for reaching out")
        assert "DECISION" not in body  # the decision line is not part of the email

    def test_decision_token_is_case_insensitive(self, monkeypatch, llm_key):
        _patch_client(
            monkeypatch,
            content="decision: skip\nlooks like a duplicate of an earlier request",
        )
        decision, _, _ = generate_email("sys", "usr")
        assert decision == "SKIP"

    def test_no_decision_prefix_defaults_to_send(self, monkeypatch, llm_key):
        content = "Happy to help with your project — let's set up a visit at your convenience!"
        _patch_client(monkeypatch, content=content)
        decision, body, _ = generate_email("sys", "usr")
        assert decision == "SEND"
        assert body == content

    def test_unrecognised_decision_token_defaults_to_send(self, monkeypatch, llm_key):
        # An unknown token must not silently suppress the email; default to SEND
        # and keep the full text as the body.
        content = "DECISION: MAYBE\nWe can definitely help with that repair, talk soon!"
        _patch_client(monkeypatch, content=content)
        decision, body, _ = generate_email("sys", "usr")
        assert decision == "SEND"
        assert body == content


# ── Error handling ───────────────────────────────────────────────────────────

class TestErrors:
    def test_short_send_body_raises(self, monkeypatch, llm_key):
        """A SEND whose actual email body is too short is an unusable generation
        and must raise (the worker then falls back to the Jinja2 template). The
        length check is on the body, not the raw response, so the
        "DECISION: SEND\\n" prefix cannot pad a tiny body over the threshold."""
        _patch_client(monkeypatch, content="DECISION: SEND\nThanks!")
        with pytest.raises(LLMError):
            generate_email("sys", "usr")

    def test_empty_output_raises(self, monkeypatch, llm_key):
        _patch_client(monkeypatch, content="")
        with pytest.raises(LLMError):
            generate_email("sys", "usr")

    def test_none_content_raises(self, monkeypatch, llm_key):
        _patch_client(monkeypatch, content=None)
        with pytest.raises(LLMError):
            generate_email("sys", "usr")

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.setattr(settings, "openai_api_key", "")
        with pytest.raises(LLMError):
            generate_email("sys", "usr")

    def test_api_exception_is_wrapped(self, monkeypatch, llm_key):
        _patch_client(monkeypatch, raises=RuntimeError("upstream 503"))
        with pytest.raises(LLMError):
            generate_email("sys", "usr")
