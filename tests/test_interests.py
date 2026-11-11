"""Tests for compiling interests from a bookmarks file."""

from types import SimpleNamespace

import pytest

from rss_digest import digest as digest_mod
from rss_digest.interests import (
    Bookmark,
    build_prompt,
    compile_interests,
    pair_counts,
    parse_bookmarks,
    sample_titles,
    since,
    tag_counts,
)

DAY = 86400
NOW = 2_000_000_000

BOOKMARKS_HTML = f"""\
<!DOCTYPE NETSCAPE-Bookmark-file-1><META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8"><TITLE>Bookmarks</TITLE><H1>Bookmarks</H1><DL>
<DT><A HREF="https://a.example/" ADD_DATE="{NOW - DAY}" TAGS="web-archives,preservation">Saving the Web &amp; You</A>
<DD>A description that should be ignored
<DT><A HREF="https://b.example/" ADD_DATE="{NOW - 2 * DAY}" TAGS="web-archives,data">Crawling at scale</A>
<DT><H3>A folder</H3>
<DL><p>
<DT><A HREF="https://c.example/" ADD_DATE="{NOW - 3 * DAY}">Untagged field recordings</A>
</DL><p>
</DL>
"""


def bm(title, tags, added=NOW, url="https://x.example/"):
    return Bookmark(url=url, title=title, added=added, tags=tags)


# --- parse_bookmarks ---


def test_parse_bookmarks_reads_links_dates_and_tags():
    bookmarks = parse_bookmarks(BOOKMARKS_HTML)
    assert bookmarks == [
        bm(
            "Saving the Web & You",
            ["web-archives", "preservation"],
            NOW - DAY,
            "https://a.example/",
        ),
        bm(
            "Crawling at scale",
            ["web-archives", "data"],
            NOW - 2 * DAY,
            "https://b.example/",
        ),
        bm("Untagged field recordings", [], NOW - 3 * DAY, "https://c.example/"),
    ]


def test_parse_bookmarks_tolerates_missing_date():
    html = '<DT><A HREF="https://x.example/" TAGS="a">No date</A>'
    assert parse_bookmarks(html) == [bm("No date", ["a"], 0)]


def test_parse_bookmarks_drops_duplicate_and_blank_tags():
    html = '<DT><A HREF="u" ADD_DATE="1" TAGS="a,,a, b">t</A>'
    assert parse_bookmarks(html)[0].tags == ["a", "b"]


# --- since ---


def test_since_keeps_bookmarks_inside_window():
    old = bm("old", [], NOW - 400 * DAY)
    new = bm("new", [], NOW - 100 * DAY)
    assert since([old, new], years=1, now=NOW) == [new]


# --- tag_counts / pair_counts ---


def test_tag_counts_skips_ignored_tags():
    bookmarks = [bm("a", ["data", "t"]), bm("b", ["data", "web"])]
    counts = tag_counts(bookmarks, ignore={"t"})
    assert counts.most_common() == [("data", 2), ("web", 1)]


def test_pair_counts_are_unordered_and_skip_ignored_tags():
    bookmarks = [
        bm("a", ["web", "data", "t"]),
        bm("b", ["data", "web"]),
        bm("c", ["data"]),
    ]
    counts = pair_counts(bookmarks, ignore={"t"})
    assert counts.most_common() == [(("data", "web"), 2)]


# --- sample_titles ---


def test_sample_titles_takes_most_recent_titles_per_tag():
    bookmarks = [
        bm("older", ["music"], NOW - 2 * DAY),
        bm("newest", ["music"], NOW),
        bm("middle", ["music"], NOW - DAY),
    ]
    by_tag, spread = sample_titles(bookmarks, ["music"], per_tag=2, spread=0)
    assert by_tag == {"music": ["newest", "middle"]}
    assert spread == []


def test_sample_titles_spread_excludes_titles_already_shown():
    bookmarks = [
        bm("tagged", ["music"]),
        bm("untagged one", []),
        bm("untagged two", []),
    ]
    by_tag, spread = sample_titles(bookmarks, ["music"], per_tag=5, spread=10)
    assert by_tag == {"music": ["tagged"]}
    assert sorted(spread) == ["untagged one", "untagged two"]


def test_sample_titles_spread_is_repeatable():
    bookmarks = [bm(f"t{i}", []) for i in range(50)]
    first = sample_titles(bookmarks, [], per_tag=0, spread=10)
    second = sample_titles(bookmarks, [], per_tag=0, spread=10)
    assert first == second
    assert len(first[1]) == 10


# --- build_prompt ---


def test_build_prompt_includes_tags_pairs_and_titles():
    bookmarks = parse_bookmarks(BOOKMARKS_HTML)
    prompt = build_prompt(
        bookmarks, years=10, top_tags=10, top_pairs=10, per_tag=5, spread=10
    )
    assert "3 bookmarks from the last 10 years" in prompt
    assert "web-archives (2)" in prompt
    assert "data + web-archives (1)" in prompt
    assert "Saving the Web & You" in prompt
    assert "Untagged field recordings" in prompt


# --- compile_interests ---


def test_compile_interests_returns_model_text(monkeypatch):
    calls = []
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="- web archives"),
                finish_reason="stop",
            )
        ]
    )

    def fake_completion(**kw):
        calls.append(kw)
        return response

    monkeypatch.setattr(digest_mod.litellm, "completion", fake_completion)
    assert compile_interests("user prompt", "some/model") == "- web archives"
    assert calls[0]["model"] == "some/model"
    assert calls[0]["messages"][1]["content"] == "user prompt"


def test_compile_interests_rejects_empty_reply(monkeypatch):
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=""), finish_reason="length")
        ]
    )
    monkeypatch.setattr(digest_mod.litellm, "completion", lambda **kw: response)
    with pytest.raises(RuntimeError, match="no text"):
        compile_interests("user prompt", "some/model")


def test_compile_interests_asks_for_requested_count(monkeypatch):
    calls = []
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="- x"), finish_reason="stop"
            )
        ]
    )
    monkeypatch.setattr(
        digest_mod.litellm, "completion", lambda **kw: calls.append(kw) or response
    )
    compile_interests("user prompt", "some/model", count=7)
    assert "about 7 interest areas" in calls[0]["messages"][0]["content"]
