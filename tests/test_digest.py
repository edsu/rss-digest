"""Tests for digest utility functions."""

from rss_digest.digest import build_prompt, strip_html, to_html


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
    {"feed_name": "Tech Blog", "title": "New Python Release", "url": "https://a.com/1", "summary": "Python 4 is out"},
    {"feed_name": "Tech Blog", "title": "AI News", "url": "https://a.com/2", "summary": ""},
    {"feed_name": "Local News", "title": "Road Closed", "url": "https://b.com/1", "summary": "Main St closed"},
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
    assert "<h1>Title</h1>" in result
    assert "Some content." in result


def test_to_html_links():
    result = to_html("[Click here](https://example.com)", "Test")
    assert 'href="https://example.com"' in result
