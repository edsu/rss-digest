"""Tests for digest utility functions."""

from types import SimpleNamespace

import pytest

from rss_digest import digest as digest_mod
from rss_digest.digest import build_prompt, strip_html, summarize, to_html


# --- strip_html ---


def test_strip_html_removes_tags():
    assert strip_html("<b>hello</b>") == "hello"


def test_strip_html_unescapes_entities():
    assert strip_html("a &amp; b") == "a & b"


def test_strip_html_collapses_whitespace():
    assert strip_html("<p>foo  \n  bar</p>") == "foo bar"


def test_strip_html_none():
    assert strip_html(None) == ""


def test_strip_html_empty():
    assert strip_html("") == ""


# --- build_prompt ---


ARTICLES = [
    {
        "feed_name": "Tech Blog",
        "title": "New Python Release",
        "url": "https://a.com/1",
        "summary": "Python 4 is out",
    },
    {
        "feed_name": "Tech Blog",
        "title": "AI News",
        "url": "https://a.com/2",
        "summary": "",
    },
    {
        "feed_name": "Local News",
        "title": "Road Closed",
        "url": "https://b.com/1",
        "summary": "Main St closed",
    },
]


def test_build_prompt_article_count():
    prompt = build_prompt(ARTICLES, 24)
    assert "3 unread articles" in prompt


def test_build_prompt_feed_headings():
    prompt = build_prompt(ARTICLES, 24)
    assert "## Tech Blog" in prompt
    assert "## Local News" in prompt


def test_build_prompt_article_links():
    prompt = build_prompt(ARTICLES, 24)
    assert "[New Python Release](https://a.com/1)" in prompt


def test_build_prompt_includes_summary():
    prompt = build_prompt(ARTICLES, 24)
    assert "Python 4 is out" in prompt


def test_build_prompt_omits_empty_summary():
    prompt = build_prompt(ARTICLES, 24)
    assert "AI News" in prompt
    # blank summary should not add a " — " line
    assert "[AI News](https://a.com/2) —" not in prompt


def test_build_prompt_hours():
    prompt = build_prompt(ARTICLES, 48)
    assert "48 hours" in prompt


# --- to_html ---


def test_to_html_structure():
    result = to_html("# Title\n\nSome content.", "My Title")
    assert "<!DOCTYPE html>" in result
    assert "<title>My Title</title>" in result
    assert 'name="viewport" content="width=device-width' in result
    assert "<h1>Title</h1>" in result
    assert "Some content." in result


def test_to_html_links():
    result = to_html("[Click here](https://example.com)", "Test")
    assert 'href="https://example.com"' in result


# --- summarize ---


def stub_completion(monkeypatch, content, finish_reason="stop"):
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content), finish_reason=finish_reason
            )
        ],
        usage=SimpleNamespace(completion_tokens=16000),
    )
    monkeypatch.setattr(digest_mod.litellm, "completion", lambda **kw: response)


def test_summarize_returns_content(monkeypatch):
    stub_completion(monkeypatch, "### News")
    assert summarize([], 24, "m") == "### News"


@pytest.mark.parametrize("content", [None, "", "  \n"])
def test_summarize_rejects_empty_reply(monkeypatch, content):
    """A reasoning model that runs out of tokens returns no text; that must
    fail loudly rather than write an empty digest."""
    stub_completion(monkeypatch, content, finish_reason="length")
    with pytest.raises(RuntimeError, match="finish_reason=length"):
        summarize([], 24, "m")
