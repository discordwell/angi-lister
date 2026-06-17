"""Tests for intro-email rendering (app/services/email.py::render_intro_email).

Covers the tenant ``intro_template`` snippet path, which historically had three
defects (all fixed here and locked in by these tests):

1. The HTML email double-escaped lead fields ("Tom & Jerry" -> "Tom &amp;amp; Jerry")
   because the snippet was rendered once with autoescape and then re-escaped by the
   outer autoescaping template.
2. The plain-text email was HTML-escaped end-to-end (the shared Jinja env had
   ``autoescape=True`` globally, which wrongly applied to the .txt template), so a
   text email read "Co &amp; Sons" instead of "Co & Sons".
3. The HTML email did not wrap the snippet's blank-line paragraphs in <p>/<br>, so
   multi-paragraph intros collapsed into one block.

A fourth, related defect — the wrapper greeting was duplicated when a custom
template (which carries its own greeting) was present — is covered by
``test_no_double_greeting``.

render_intro_email only reads attributes off the Lead/Tenant, so these tests build
detached model instances without touching the database.
"""

from app.models import Lead, Tenant
from app.services.email import render_intro_email


def _tenant(intro_template=None, **kw):
    defaults = dict(
        name="Acme & Sons HVAC",
        slug="acme",
        brand_color="#2563eb",
        phone="(555) 010-2030",
        intro_template=intro_template,
    )
    defaults.update(kw)
    return Tenant(**defaults)


def _lead(**kw):
    defaults = dict(
        correlation_id="corr-1",
        al_account_id="100001",
        first_name="Tom & Jerry",
        last_name="Smith",
        email="tom@example.com",
        phone="5551234567",
        category="AC & Heating",
        description="My furnace is making a <weird> noise & smells.",
        raw_payload={},
    )
    defaults.update(kw)
    return Lead(**defaults)


SEEDED_STYLE_TEMPLATE = (
    "Hi {{ first_name }},\n\n"
    "Thanks for reaching out about {{ category }}! "
    "We have received your request and are on it.\n\n"
    "A technician will be in touch shortly."
)


class TestCustomBodyHtml:
    def test_lead_fields_not_double_escaped(self):
        """Special chars in lead fields are escaped exactly once in the HTML email."""
        html, _ = render_intro_email(_lead(), _tenant(SEEDED_STYLE_TEMPLATE))
        # Single escape present, double escape absent.
        assert "Tom &amp; Jerry" in html
        assert "AC &amp; Heating" in html
        assert "&amp;amp;" not in html

    def test_paragraphs_wrapped(self):
        """Blank-line-separated paragraphs become distinct <p> blocks in HTML."""
        html, _ = render_intro_email(_lead(), _tenant(SEEDED_STYLE_TEMPLATE))
        # The three paragraphs of the snippet each get their own <p>.
        assert "<p>Hi Tom &amp; Jerry,</p>" in html
        assert "<p>A technician will be in touch shortly.</p>" in html

    def test_single_newline_becomes_br(self):
        html, _ = render_intro_email(
            _lead(), _tenant("Line one\nLine two\n\nNew paragraph")
        )
        assert "<p>Line one<br>Line two</p>" in html
        assert "<p>New paragraph</p>" in html

    def test_crlf_in_lead_field_splits_paragraphs(self):
        """CRLF carried in by a webhook field still splits into <p> with no stray \\r."""
        # A consumer-typed multi-line Description arrives with Windows line endings.
        ld = _lead(description="Para one.\r\n\r\nPara two.")
        html, _ = render_intro_email(ld, _tenant("{{ description }}"))
        assert "<p>Para one.</p>" in html
        assert "<p>Para two.</p>" in html
        assert "\r" not in html

    def test_malicious_lead_field_is_escaped(self):
        """XSS guard: webhook-supplied lead data cannot inject markup into the HTML.

        The same body_html is previewed in the console's lead-detail iframe, so an
        unescaped script tag here would be a stored-XSS sink.
        """
        evil = _lead(
            first_name="<script>alert('xss')</script>",
            category="<img src=x onerror=alert(1)>",
        )
        html, _ = render_intro_email(evil, _tenant("Hi {{ first_name }} — {{ category }}"))
        assert "<script>" not in html
        assert "<img src=x" not in html
        assert "&lt;script&gt;" in html
        assert "&lt;img src=x onerror=alert(1)&gt;" in html


class TestCustomBodyText:
    def test_text_email_is_not_html_escaped(self):
        """The plain-text email keeps raw characters — no HTML entities anywhere."""
        _, text = render_intro_email(_lead(), _tenant(SEEDED_STYLE_TEMPLATE))
        assert "Tom & Jerry" in text
        assert "AC & Heating" in text
        assert "&amp;" not in text
        # The wrapper's own fields (tenant name) must be raw too.
        assert "Acme & Sons HVAC" in text

    def test_text_email_preserves_paragraph_breaks(self):
        _, text = render_intro_email(_lead(), _tenant(SEEDED_STYLE_TEMPLATE))
        assert "Hi Tom & Jerry,\n\nThanks for reaching out" in text

    def test_text_email_does_not_wrap_in_html_tags(self):
        _, text = render_intro_email(_lead(), _tenant(SEEDED_STYLE_TEMPLATE))
        assert "<p>" not in text
        assert "<br>" not in text


class TestGreeting:
    def test_no_double_greeting(self):
        """A custom template carries its own greeting; the wrapper must not add one."""
        html, text = render_intro_email(
            _lead(first_name="Dana"), _tenant("Hi {{ first_name }},\n\nWelcome aboard.")
        )
        assert html.count("Hi Dana,") == 1
        assert text.count("Hi Dana,") == 1


class TestDefaultBody:
    def test_default_html_when_no_template(self):
        """With no intro_template, the baked-in default body and styled greeting show."""
        html, _ = render_intro_email(_lead(first_name="Dana", category="HVAC"), _tenant(None))
        assert 'class="greeting">Hi Dana,' in html
        assert "Thanks for reaching out about HVAC" in html

    def test_default_text_when_no_template(self):
        _, text = render_intro_email(_lead(first_name="Dana", category="HVAC"), _tenant(None))
        assert text.count("Hi Dana,") == 1
        assert "Thanks for reaching out about HVAC" in text
        assert "&amp;" not in text

    def test_default_html_escapes_lead_fields(self):
        """The default-body branch still escapes webhook-supplied fields in HTML."""
        html, _ = render_intro_email(
            _lead(description="<b>boom</b> & co", category="x & y"), _tenant(None)
        )
        assert "<b>boom</b>" not in html
        assert "&lt;b&gt;boom&lt;/b&gt;" in html


class TestRenderFailureFallback:
    def test_broken_template_falls_back_to_default(self):
        """A template that raises at render time falls back to the default body."""
        # Undefined filter -> render raises -> snippet returns (None, None).
        html, text = render_intro_email(
            _lead(first_name="Dana"), _tenant("Hi {{ first_name | no_such_filter }}")
        )
        assert "Thanks for reaching out" in html
        assert "Thanks for reaching out" in text
        assert 'class="greeting">Hi Dana,' in html
