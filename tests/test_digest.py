"""Tests for digest utility functions."""

import asyncio
from types import SimpleNamespace

import pytest

from rss_digest import digest as digest_mod
from rss_digest.digest import (
    build_prompt,
    content_report,
    fetch_articles,
    strip_html,
    summarize,
    to_html,
    truncate_summaries,
)
from rss_digest.greader import Article, Config


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


# --- fetch_articles ---


def test_fetch_articles_keeps_full_summary(monkeypatch):
    """Truncation happens later, so --verbose can report the full lengths."""
    long_text = "word " * 1000

    class FakeClient:
        def __init__(self, config):
            pass

        async def get_articles(self, **kwargs):
            return [
                Article(1, "T", f"<p>{long_text}</p>", "u", 2**40, "F", False, False)
            ]

        async def aclose(self):
            pass

    monkeypatch.setattr(digest_mod, "GReaderClient", FakeClient)
    config = Config(GREADER_URL="u", GREADER_USERNAME="u", GREADER_PASSWORD="p")
    articles = asyncio.run(fetch_articles(config, 24))
    assert articles[0]["summary"] == long_text.strip()


# --- truncate_summaries ---


def test_truncate_summaries_cuts_long_summaries():
    [short, empty, other] = truncate_summaries(ARTICLES, 6)
    assert short["summary"] == "Python"
    assert empty["summary"] == ""
    assert other["summary"] == "Main S"


def test_truncate_summaries_leaves_the_input_alone():
    truncate_summaries(ARTICLES, 6)
    assert ARTICLES[0]["summary"] == "Python 4 is out"


def test_truncate_summaries_zero_drops_them():
    assert all(a["summary"] == "" for a in truncate_summaries(ARTICLES, 0))


# --- content_report ---


def test_content_report_lists_each_feed():
    report = content_report(ARTICLES)
    rows = report.splitlines()[2:]
    assert [r.split()[0] for r in rows] == ["Tech", "Local"]


def test_content_report_shows_count_median_and_max():
    report = content_report(ARTICLES)
    tech = next(r for r in report.splitlines() if r.startswith("Tech Blog"))
    # summaries of 15 and 0 characters
    assert tech.split()[-3:] == ["2", "7", "15"]


def test_content_report_puts_thinnest_feeds_first():
    articles = [
        {"feed_name": "Full", "summary": "x" * 5000},
        {"feed_name": "Teaser", "summary": "x" * 200},
        {"feed_name": "Empty", "summary": ""},
    ]
    rows = content_report(articles).splitlines()[2:]
    assert [r.split()[0] for r in rows] == ["Empty", "Teaser", "Full"]


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
