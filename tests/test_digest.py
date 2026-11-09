"""Tests for digest utility functions."""

import asyncio
from types import SimpleNamespace

import pytest

from rss_digest import digest as digest_mod
from rss_digest.digest import (
    HISTORY_GUIDANCE,
    ROUNDUP_GUIDANCE,
    build_prompt,
    content_report,
    fetch_articles,
    full_system_prompt,
    roundup_items,
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


def test_build_prompt_heads_roundup_items_with_their_roundup():
    item = {
        **ARTICLES[0],
        "roundup": {"title": "Daily HN", "url": "https://hn.example/1"},
    }
    prompt = build_prompt([item], 24)
    assert "## Tech Blog — roundup: [Daily HN](https://hn.example/1)" in prompt


# --- full_system_prompt ---


def test_full_system_prompt_adds_roundup_guidance_only_for_roundups():
    item = {**ARTICLES[0], "roundup": {"title": "R", "url": "u"}}
    assert ROUNDUP_GUIDANCE not in full_system_prompt("S", ARTICLES, None)
    assert full_system_prompt("S", [item], None) == "S" + ROUNDUP_GUIDANCE


def test_full_system_prompt_adds_history_guidance_only_with_history():
    assert HISTORY_GUIDANCE not in full_system_prompt("S", ARTICLES, [])
    assert HISTORY_GUIDANCE in full_system_prompt("S", ARTICLES, [("d", "t")])


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


def test_fetch_articles_splits_roundups_and_marks_each_post_once(monkeypatch):
    marked = []

    class FakeClient:
        def __init__(self, config):
            pass

        async def get_articles(self, **kwargs):
            return [
                Article(7, "Daily", HN_DAILY, ROUNDUP_URL, 2**40, "HN", False, False),
                Article(8, "Essay", "<p>Plain text</p>", "u", 2**40, "F", False, False),
            ]

        async def mark_as_read(self, ids):
            marked.extend(ids)

        async def aclose(self):
            pass

    monkeypatch.setattr(digest_mod, "GReaderClient", FakeClient)
    config = Config(GREADER_URL="u", GREADER_USERNAME="u", GREADER_PASSWORD="p")
    articles = asyncio.run(fetch_articles(config, 24, mark_read=True))
    assert [a["title"] for a in articles] == [
        "Asahi Linux progress report",
        "Why Electron apps are slow",
        "Apple's grey list of words",
        "Essay",
    ]
    assert marked == [7, 8]


# --- roundup_items ---


ROUNDUP_URL = "https://www.daemonology.net/hn-daily/2026-11-08.html"

# Shaped like a real Hacker News Daily post
HN_DAILY = """
<p>The 3 highest-rated articles on <a href="https://news.ycombinator.com/">Hacker
News</a> which have not appeared on any previous
<a href="https://www.daemonology.net/hn-daily/">Hacker News Daily</a> are:</p>
<ul>
<li><a href="https://asahilinux.org/report/">Asahi Linux progress report</a><br>
<a href="https://news.ycombinator.com/item?id=1">(comments)</a></li>
<li><a href="https://example.com/electron">Why Electron apps are slow</a><br>
<a href="https://news.ycombinator.com/item?id=2">(comments)</a></li>
<li><a href="https://example.org/grey-list">Apple's grey list of words</a><br>
<a href="https://news.ycombinator.com/item?id=3">(comments)</a></li>
</ul>
"""


def roundup(summary: str, url: str = ROUNDUP_URL) -> dict:
    return {
        "id": 1,
        "title": "Daily HN",
        "summary": summary,
        "url": url,
        "feed_name": "Hacker News Daily",
    }


def split(summary: str) -> list[dict]:
    items = roundup_items(roundup(summary))
    assert items is not None
    return items


def test_roundup_items_one_per_recommended_link():
    items = split(HN_DAILY)
    # comments, the roundup's own pages and front-page links are dropped
    assert [(i["title"], i["url"]) for i in items] == [
        ("Asahi Linux progress report", "https://asahilinux.org/report/"),
        ("Why Electron apps are slow", "https://example.com/electron"),
        ("Apple's grey list of words", "https://example.org/grey-list"),
    ]


def test_roundup_items_remember_their_roundup_and_feed():
    item = split(HN_DAILY)[0]
    assert item["roundup"] == {"title": "Daily HN", "url": ROUNDUP_URL}
    assert item["feed_name"] == "Hacker News Daily"
    assert item["id"] == 1


def test_roundup_items_take_context_from_their_block():
    html_ = "".join(
        f'<li><a href="https://x{n}.com/a">Story number {n}</a> — a blurb about '
        f"story {n}</li>"
        for n in range(3)
    )
    assert (
        split(f"<ul>{html_}</ul>")[0]["summary"]
        == "Story number 0 — a blurb about story 0"
    )


def test_roundup_items_drop_context_that_only_repeats_the_headline():
    assert split(HN_DAILY)[0]["summary"] == ""


def test_roundup_items_skip_boilerplate_links():
    html_ = HN_DAILY.replace(
        "</ul>",
        '<li><a href="https://sponsor.com/x">Click here</a></li>'
        '<li><a href="https://sponsor.com/y">Get the free roadmap →</a></li></ul>',
    )
    titles = [i["title"] for i in split(html_)]
    assert "Click here" not in titles
    assert "Get the free roadmap →" not in titles


def test_roundup_items_leave_heavily_cited_essays_alone():
    para = (
        "<p>Researchers have long argued about this, and the evidence remains "
        'mixed, as <a href="https://j.org/{n}">one study</a> found after years '
        "of careful fieldwork across many sites and seasons.</p>"
    )
    essay = "".join(para.format(n=n) for n in range(20))
    assert roundup_items(roundup(essay)) is None


def test_roundup_items_leave_prose_with_a_link_per_paragraph_alone():
    """Most of the text is link text, but each link is a phrase inside its
    paragraph, not a headline; splitting would lose the post."""
    post = "".join(
        f'<p>I chatted about <a href="https://x{n}.com/a">my new record and the '
        f"tour that followed it</a> with a writer last week, which was fun.</p>"
        for n in range(4)
    )
    assert roundup_items(roundup(post)) is None


def test_roundup_items_need_a_few_links():
    one = '<ul><li><a href="https://x.com/a">Only one story here</a></li></ul>'
    assert roundup_items(roundup(one)) is None


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
