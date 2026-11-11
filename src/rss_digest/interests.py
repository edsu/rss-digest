#!/usr/bin/env python3
"""Compile a reader's interests from a bookmarks file using an LLM.

Reads a Netscape bookmarks export (bookmarks.html, as written by browsers,
Pinboard and others) and prints a short bulleted list of interest areas, ready
to paste into a --prompt-file for rss-digest.

The whole file is far too large to send, so it is boiled down first: how often
each tag is used, which tags appear together (the clusters are often closer to
an interest than any single tag), the most recent titles under the top tags,
and a random spread of other titles so that interests that were never tagged
well can still show up.
"""

import argparse
import itertools
import random
import sys
import time
from collections import Counter
from collections.abc import Container
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from dotenv import load_dotenv

from rss_digest.digest import DEFAULT_MODEL, complete

DEFAULT_YEARS = 10
DEFAULT_TOP_TAGS = 150
DEFAULT_TOP_PAIRS = 100
DEFAULT_PER_TAG = 3
DEFAULT_SPREAD = 200
# The list is pasted into the digest's system prompt and sent with every daily
# run, so it should stay small: a dozen one-line bullets is a few hundred tokens.
DEFAULT_COUNT = 10

# Fixed so that --print-prompt shows the same sample the model would get.
SAMPLE_SEED = 0

SYSTEM_PROMPT = """\
You are compiling a list of one reader's interests from their bookmarks. The \
list will be pasted into the instructions for a daily digest of their RSS \
feeds, where it shapes which articles get featured, so it must be short.

You are given how often each tag was used, which tags were used together, and \
a sample of bookmark titles. Tags that appear together often describe one \
interest better than either does alone. Use the titles to work out what a \
tag actually covers, and to find interests the tags miss.

Ignore tags that describe a bookmark's format or the reader's workflow \
rather than its subject (e.g. videos, podcasts, toread, delete, single \
letters).

Reply with only a Markdown bulleted list of about {count} interest areas, \
most significant first. Each bullet is one line: a few lowercase subject \
phrases separated by commas, specific enough to tell articles apart (e.g. \
"music: americana, ambient, field recording, jazz"). No headings, \
introduction or commentary.\
"""


@dataclass
class Bookmark:
    url: str
    title: str
    added: int  # Unix time from ADD_DATE, 0 if missing
    tags: list[str]


class _BookmarkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.bookmarks: list[Bookmark] = []
        self._current: Bookmark | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        a = dict(attrs)
        added = a.get("add_date") or ""
        tags = (t.strip() for t in (a.get("tags") or "").split(","))
        self._current = Bookmark(
            url=a.get("href") or "",
            title="",
            added=int(added) if added.isdigit() else 0,
            tags=list(dict.fromkeys(t for t in tags if t)),
        )

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current is not None:
            self._current.title = " ".join(self._current.title.split())
            self.bookmarks.append(self._current)
            self._current = None

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current.title += data


def parse_bookmarks(text: str) -> list[Bookmark]:
    parser = _BookmarkParser()
    parser.feed(text)
    parser.close()
    return parser.bookmarks


def since(bookmarks: list[Bookmark], years: float, now: float) -> list[Bookmark]:
    cutoff = now - years * 365.25 * 86400
    return [b for b in bookmarks if b.added >= cutoff]


def tag_counts(bookmarks: list[Bookmark], ignore: Container[str] = ()) -> Counter:
    return Counter(t for b in bookmarks for t in b.tags if t not in ignore)


def pair_counts(bookmarks: list[Bookmark], ignore: Container[str] = ()) -> Counter:
    counts: Counter = Counter()
    for b in bookmarks:
        tags = sorted(t for t in b.tags if t not in ignore)
        counts.update(itertools.combinations(tags, 2))
    return counts


def sample_titles(
    bookmarks: list[Bookmark], tags: list[str], per_tag: int, spread: int
) -> tuple[dict[str, list[str]], list[str]]:
    """The most recent titles under each tag, plus a random spread of others."""
    newest_first = sorted(bookmarks, key=lambda b: b.added, reverse=True)
    shown: set[str] = set()
    by_tag: dict[str, list[str]] = {}
    for tag in tags:
        titles = [b.title for b in newest_first if tag in b.tags and b.title]
        by_tag[tag] = titles[:per_tag]
        shown.update(by_tag[tag])
    rest = list(dict.fromkeys(b.title for b in bookmarks if b.title))
    rest = [t for t in rest if t not in shown]
    sample = random.Random(SAMPLE_SEED).sample(rest, min(spread, len(rest)))
    return by_tag, sample


def build_prompt(
    bookmarks: list[Bookmark],
    years: float,
    top_tags: int,
    top_pairs: int,
    per_tag: int,
    spread: int,
    ignore: Container[str] = (),
) -> str:
    tags = tag_counts(bookmarks, ignore).most_common(top_tags)
    pairs = pair_counts(bookmarks, ignore).most_common(top_pairs)
    by_tag, others = sample_titles(bookmarks, [t for t, _ in tags], per_tag, spread)

    lines = [f"{len(bookmarks)} bookmarks from the last {years:g} years.", ""]
    lines.append("## Tags, most used first")
    lines.append(", ".join(f"{t} ({n})" for t, n in tags))
    lines.append("")
    lines.append("## Tags used together")
    lines.append(", ".join(f"{a} + {b} ({n})" for (a, b), n in pairs))
    lines.append("")
    lines.append("## Recent titles by tag")
    for tag, titles in by_tag.items():
        if titles:
            lines.append(f"- {tag}: " + " | ".join(titles))
    lines.append("")
    lines.append("## Other titles")
    lines.extend(f"- {t}" for t in others)
    return "\n".join(lines)


def compile_interests(
    user_prompt: str,
    model: str,
    count: int = DEFAULT_COUNT,
    api_base: str | None = None,
) -> str:
    return complete(model, SYSTEM_PROMPT.format(count=count), user_prompt, api_base)


def main() -> None:
    # Before litellm reads the provider's API key from the environment.
    load_dotenv(Path(".env"))

    parser = argparse.ArgumentParser(
        description="Compile a list of interests from a bookmarks file, to "
        "paste into an rss-digest --prompt-file",
        epilog="The LLM provider's API key comes from the environment (or "
        "./.env), e.g. ANTHROPIC_API_KEY.",
    )
    parser.add_argument(
        "bookmarks", type=Path, help="Netscape-format bookmarks.html export"
    )

    llm = parser.add_argument_group("LLM")
    llm.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        metavar="MODEL",
        help=f"LiteLLM model string (default: {DEFAULT_MODEL})",
    )
    llm.add_argument(
        "--model-url",
        metavar="URL",
        help="Base URL of an OpenAI-compatible API or LiteLLM gateway "
        "(default: the provider's own endpoint)",
    )
    llm.add_argument(
        "--print-prompt",
        action="store_true",
        help="Print the system prompt and user prompt then exit (no LLM call)",
    )

    sample = parser.add_argument_group("bookmarks")
    sample.add_argument(
        "--years",
        type=float,
        default=DEFAULT_YEARS,
        metavar="N",
        help=f"Only use bookmarks added in the last N years (default: {DEFAULT_YEARS})",
    )
    sample.add_argument(
        "--ignore-tags",
        default="",
        metavar="TAGS",
        help="Comma-separated tags to leave out, e.g. toread,delete",
    )
    sample.add_argument(
        "--top-tags",
        type=int,
        default=DEFAULT_TOP_TAGS,
        metavar="N",
        help=f"How many of the most used tags to send (default: {DEFAULT_TOP_TAGS})",
    )
    sample.add_argument(
        "--top-pairs",
        type=int,
        default=DEFAULT_TOP_PAIRS,
        metavar="N",
        help="How many of the most common tag pairs to send "
        f"(default: {DEFAULT_TOP_PAIRS})",
    )
    sample.add_argument(
        "--titles-per-tag",
        type=int,
        default=DEFAULT_PER_TAG,
        metavar="N",
        help=f"Recent titles to send for each top tag (default: {DEFAULT_PER_TAG})",
    )
    sample.add_argument(
        "--random-titles",
        type=int,
        default=DEFAULT_SPREAD,
        metavar="N",
        help=f"Other titles to send, chosen at random (default: {DEFAULT_SPREAD})",
    )
    sample.add_argument(
        "--count",
        type=int,
        default=DEFAULT_COUNT,
        metavar="N",
        help="Roughly how many interests to list; the list goes into every "
        f"digest's prompt, so keep it short (default: {DEFAULT_COUNT})",
    )
    args = parser.parse_args()

    for name in ("top_tags", "top_pairs", "titles_per_tag", "random_titles"):
        if getattr(args, name) < 0:
            flag = "--" + name.replace("_", "-")
            print(f"error: {flag} must be 0 or more", file=sys.stderr)
            sys.exit(1)
    if args.count < 1:
        print("error: --count must be 1 or more", file=sys.stderr)
        sys.exit(1)

    try:
        text = args.bookmarks.read_text(encoding="utf-8")
    except OSError as e:
        print(f"error: can't read {args.bookmarks}: {e}", file=sys.stderr)
        sys.exit(1)

    bookmarks = since(parse_bookmarks(text), args.years, time.time())
    if not bookmarks:
        print(
            f"error: no bookmarks from the last {args.years:g} years in "
            f"{args.bookmarks}",
            file=sys.stderr,
        )
        sys.exit(1)

    ignore = {t.strip() for t in args.ignore_tags.split(",") if t.strip()}
    user_prompt = build_prompt(
        bookmarks,
        args.years,
        args.top_tags,
        args.top_pairs,
        args.titles_per_tag,
        args.random_titles,
        ignore,
    )

    if args.print_prompt:
        print("=== SYSTEM PROMPT ===")
        print(SYSTEM_PROMPT.format(count=args.count))
        print("\n=== USER PROMPT ===")
        print(user_prompt)
        return

    print(f"Compiling interests with {args.model}...", file=sys.stderr)
    try:
        interests = compile_interests(
            user_prompt, args.model, args.count, args.model_url
        )
    except Exception as e:
        print(f"error: LLM call failed: {e}", file=sys.stderr)
        sys.exit(1)
    print(interests)


if __name__ == "__main__":
    main()
