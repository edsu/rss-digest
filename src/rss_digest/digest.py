#!/usr/bin/env python3
"""Generate an RSS digest from FreshRSS articles using an LLM.

Model is selected with --model (default: anthropic/claude-sonnet-5). Any
litellm-supported model string works, e.g.:
  anthropic/claude-sonnet-5     (needs ANTHROPIC_API_KEY)
  openai/gpt-4o                 (needs OPENAI_API_KEY)
  openai/mistral-7b-instruct    (LM Studio: --model-url http://localhost:1234/v1)
  litellm_proxy/some-model      (LiteLLM gateway: --model-url URL, LITELLM_PROXY_API_KEY)

Time window defaults to 24 hours; override with --hours.

Settings come from the command line; only secrets (GREADER_PASSWORD and the
LLM provider's API key) are read from the environment, which can be filled
from a .env file in the current directory.
"""

import argparse
import asyncio
import html
import logging
import os
import re
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import litellm
from dotenv import load_dotenv
from markdown import markdown

from rss_digest.greader import Config, GReaderClient

DEFAULT_MODEL = "anthropic/claude-sonnet-5"
DEFAULT_HOURS = 24
MAX_ARTICLES = 2000
DEFAULT_SUMMARY_LENGTH = 1000
DEFAULT_HISTORY = 3  # ~9k tokens; story threads rarely span more than a few days

# Caps thinking *plus* visible text. Sonnet 5 thinks by default (Sonnet 4.6 did
# not), so the 4096 that comfortably fit a digest can now truncate one mid-sentence.

SYSTEM_PROMPT = """\
You are producing a digest of RSS articles.

Format rules:
- ≤25 articles: Reading queue. Group by theme under ### headings. List every \
article as a bullet with a markdown link. Include the feed name.
- >25 articles: Curated TL;DR. Themed prose under ### headings. Cover roughly \
⅓ of articles. Drop filler (sponsored posts, job listings, police blotters, \
sub-100-word link-outs). Dense single-feed clusters (6+ posts): pick 2–3 \
representatives, then "and N more from [feed name]".

Every article title you mention must be an inline markdown link. Be concise.\
"""

# Appended to whatever system prompt is in use, but only when there is history to
# send. The prohibitions matter more than the permission: without them the model
# reuses yesterday's section headings and invents continuity that isn't there.
HISTORY_GUIDANCE = """

The user prompt may include a <previous_digests> block holding recent digests. \
Use it ONLY to:
- avoid re-telling a story you already covered; if a topic recurs, say what is \
new about it and link today's article
- follow up on genuine developments in an ongoing thread
- keep a sense of variety in the daily reports to encourage serendipity.

Do NOT reuse their headings, ordering, or phrasing. Each digest finds its own \
shape from today's articles. If nothing connects, ignore them entirely.\
"""

# Appended, like HISTORY_GUIDANCE, only when a roundup was split into items.
# Without it the model tends to give the roundup a section of its own.
ROUNDUP_GUIDANCE = """

Some feed sections are headed "roundup": a post in which someone else \
recommends links (a daily best-of, a newsletter, a links blog), split into the \
links it recommends. Treat each link as an article in its own right: weigh it \
against everything else and put it under whatever theme it fits, not in a \
section for the roundup. When you use one, credit the roundup in passing, \
e.g. "(via Hacker News Daily)". Don't summarise the roundup itself.\
"""


def full_system_prompt(
    system_prompt: str, articles: list[dict], history: list[tuple[str, str]] | None
) -> str:
    """The system prompt plus whichever guidance this run's input calls for."""
    if history:
        system_prompt += HISTORY_GUIDANCE
    if any("roundup" in a for a in articles):
        system_prompt += ROUNDUP_GUIDANCE
    return system_prompt


def strip_html(s: str | None) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


# A post is a roundup when most of its text is link text: HN Daily, newsletters
# and links blogs sit at 0.3–0.85, while essays that cite heavily stay under 0.1
# however many links they have. Link count alone can't tell the two apart.
ROUNDUP_LINK_RATIO = 0.25
ROUNDUP_MIN_LINKS = 3
# In a roundup a link is the headline of its block, so it comes first: 0.7–1.0
# of links open their list item or heading, blurb or not. Prose that links a
# phrase per paragraph puts them mid-sentence (under 0.3), and splitting that
# would lose the post itself.
ROUNDUP_HEADLINE_SHARE = 0.5
# Newsletter boilerplate: a link whose text says nothing about where it goes
BOILERPLATE_LINK = re.compile(
    r"^(click here|read more|learn more|subscribe|sign up|here)\b|[→»]$", re.I
)
# Caps a link's context; an unclosed block would otherwise run to the end of the post.
ROUNDUP_CONTEXT_LENGTH = 500

# The element a link sits in supplies its context: its list item, paragraph or
# heading, rather than the whole post.
BLOCK_TAGS = {
    "p", "li", "dd", "dt", "td", "div", "blockquote",
    "h1", "h2", "h3", "h4", "h5", "h6",
}  # fmt: skip


def collapse(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


class LinkScanner(HTMLParser):
    """Collects each link with its text and the text of the block around it."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.blocks: list[list[str]] = []
        # (href, text, index of the block it sits in, or None)
        self.links: list[tuple[str, list[str], int | None]] = []
        self._open: list[tuple[str, int]] = []
        self._link: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in BLOCK_TAGS:
            # <p> and <li> are often left unclosed; a new one ends the last
            if tag in ("p", "li") and self._open and self._open[-1][0] == tag:
                self._open.pop()
            self.blocks.append([])
            self._open.append((tag, len(self.blocks) - 1))
        elif tag == "a":
            self._link = []
            block = self._open[-1][1] if self._open else None
            self.links.append((dict(attrs).get("href") or "", self._link, block))
        elif tag == "br":
            self.handle_data(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._link = None
        elif tag in BLOCK_TAGS:
            for i in range(len(self._open) - 1, -1, -1):
                if self._open[i][0] == tag:
                    del self._open[i:]
                    break

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        if self._link is not None:
            self._link.append(data)
        for _, i in self._open:
            self.blocks[i].append(data)


def roundup_items(article: dict) -> list[dict] | None:
    """Split a roundup post into one item per link it recommends, else None.

    Stripped to text, a roundup is a run of headlines with no URLs, which the
    model can neither link nor weigh against other articles. As items they
    compete on their own merits. Each keeps the post's id and feed, so marking
    read and the per-feed rules still work, and records the post it came from.
    """
    scanner = LinkScanner()
    scanner.feed(article["summary"] or "")
    scanner.close()
    total = len(collapse("".join(scanner.text)))
    linked = sum(len(collapse("".join(text))) for _, text, _ in scanner.links)
    if not total or linked / total < ROUNDUP_LINK_RATIO:
        return None

    own_host = urlsplit(article["url"]).netloc
    seen = set()
    items = []
    headlines = 0
    for href, text, block in scanner.links:
        title = collapse("".join(text))
        parts = urlsplit(href)
        if (
            parts.scheme not in ("http", "https")
            # the roundup's own tags, archives and navigation
            or parts.netloc == own_host
            # "via Hacker News"-style links to a site's front page
            or parts.path in ("", "/")
            # "(comments)", "[pdf]", bare URLs, image-only links
            or len(title.split()) < 2
            or title.startswith(("(", "["))
            or BOILERPLATE_LINK.search(title)
            or href in seen
        ):
            continue
        seen.add(href)
        context = collapse("".join(scanner.blocks[block])) if block is not None else ""
        headlines += context.startswith(title) or not context
        # A block that is just the headline adds nothing
        if len(context) - len(title) < 20:
            context = ""
        items.append(
            {
                **article,
                "title": title,
                "url": href,
                "summary": context[:ROUNDUP_CONTEXT_LENGTH],
                "roundup": {"title": article["title"], "url": article["url"]},
            }
        )
    if (
        len(items) < ROUNDUP_MIN_LINKS
        or headlines / len(items) < ROUNDUP_HEADLINE_SHARE
    ):
        return None
    return items


async def fetch_articles(
    config: Config, hours: int, mark_read: bool = False
) -> list[dict]:
    client = GReaderClient(config)
    cutoff = int(time.time()) - hours * 3600
    try:
        articles = await client.get_articles(limit=MAX_ARTICLES, since_timestamp=cutoff)
        result = []
        for a in articles:
            if a.published < cutoff:
                continue
            article = a.to_dict()
            items = roundup_items(article)
            result.extend(items or [{**article, "summary": strip_html(a.summary)}])
        if mark_read and result:
            # int() because these articles are plain dicts, so the id's type is
            # lost on the way through to_dict(); a set because a roundup's items
            # all carry the id of the post they came from
            await client.mark_as_read(sorted({int(a["id"]) for a in result}))
        return result
    finally:
        await client.aclose()


def post_count(articles: list[dict]) -> int:
    """How many posts came from the reader, counting a split roundup once."""
    return len({a["id"] for a in articles})


def truncate_summaries(articles: list[dict], length: int) -> list[dict]:
    return [{**a, "summary": a["summary"][:length]} for a in articles]


def content_report(articles: list[dict]) -> str:
    """Per-feed summary lengths, thinnest first, to show which feeds send only teasers.

    Lengths are in characters after stripping HTML, before --summary-length cuts
    them, so they show what a longer summary could actually draw on.
    """
    by_feed: dict[str, list[int]] = defaultdict(list)
    for a in articles:
        by_feed[a["feed_name"]].append(len(a["summary"]))
    rows = sorted(by_feed.items(), key=lambda kv: statistics.median(kv[1]))
    lines = [
        "Summary length per feed (characters, before --summary-length):",
        f"{'feed':<45} {'n':>4} {'median':>7} {'max':>7}",
    ]
    for feed, lengths in rows:
        lines.append(
            f"{feed[:45]:<45} {len(lengths):>4} "
            f"{int(statistics.median(lengths)):>7} {max(lengths):>7}"
        )
    return "\n".join(lines)


DATE_IN_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})")

# Header, stats line and footer that to_html/main add around the LLM's own text.
# Each digest is relabelled with its date on the way into the prompt, so this is
# all noise — and "Generated by <model>" in particular is worth not feeding back.
NOISE_LINE = re.compile(
    r"^(?:RSS Digest\b.*|\d+ articles · \d+ sites|Generated by\b.*|-{3,})$"
)


def digest_to_text(raw: str) -> str:
    """Reduce an archived digest to plain markdown, accepting either .md or .html.

    Links are preserved as markdown so the model can tell which URLs it has
    already covered, rather than just which headlines.
    """
    if "<html" in raw[:200].lower():
        body = re.search(r"<body[^>]*>(.*)</body>", raw, re.S)
        s = body.group(1) if body else raw
        s = re.sub(
            r"""<a\s[^>]*href=["']([^"']+)["'][^>]*>(.*?)</a>""",
            r"[\2](\1)",
            s,
            flags=re.S,
        )
        s = re.sub(
            r"<h([1-6])[^>]*>(.*?)</h\1>",
            lambda m: f"\n{'#' * int(m.group(1))} {m.group(2)}\n",
            s,
            flags=re.S,
        )
        s = re.sub(r"<li[^>]*>", "- ", s)
        s = re.sub(r"</(?:p|li|div|blockquote|ul|ol)>", "\n", s)
        s = html.unescape(re.sub(r"<[^>]+>", "", s))
        raw = re.sub(r"[ \t]+", " ", s)

    kept = [ln for ln in raw.splitlines() if not NOISE_LINE.match(ln.strip(" *_#"))]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def digest_date(path: Path) -> str:
    """Date to file a digest under: from its name if present, else when it was created.

    A date in the filename is preferred because it survives copying — moving a
    digest between volumes, or restoring it from a sync service, resets the file's
    creation time and would silently misdate it. st_birthtime is the real creation
    time on macOS/BSD; elsewhere fall back to the modification time.
    """
    m = DATE_IN_NAME.search(path.stem)
    if m:
        return m.group(1)
    st = path.stat()
    stamp = getattr(st, "st_birthtime", st.st_mtime)
    return datetime.fromtimestamp(stamp).strftime("%Y-%m-%d")


def load_history(archive_dir: Path, count: int, today: str) -> list[tuple[str, str]]:
    """Return the most recent `count` archived digests as (date, markdown) pairs.

    Any .md or .html file in the directory counts, whatever it's named. Today's own
    digest is skipped so that re-running on the same day doesn't feed the digest
    back to itself.
    """
    if count < 1 or not archive_dir.is_dir():
        return []
    dated = []
    for p in sorted(archive_dir.iterdir()):
        if p.suffix not in (".md", ".html") or not p.is_file():
            continue
        date = digest_date(p)
        if date != today:
            # mtime breaks ties so same-date files keep a stable order
            dated.append((date, p.stat().st_mtime, p))
    dated.sort()  # ISO dates sort chronologically
    return [(d, digest_to_text(p.read_text())) for d, _, p in dated[-count:]]


def build_prompt(
    articles: list[dict], hours: int, history: list[tuple[str, str]] | None = None
) -> str:
    by_feed: dict[str, list[dict]] = defaultdict(list)
    for a in articles:
        heading = a["feed_name"]
        # Its own heading per roundup, so the model can credit it by name
        if "roundup" in a:
            r = a["roundup"]
            heading += f" — roundup: [{r['title']}]({r['url']})"
        by_feed[heading].append(a)

    lines = []
    if history:
        # A tag rather than a "# Digest for <date>" heading: models copied the
        # heading into the new digest, dated as the day after the last one.
        lines.append("<previous_digests>")
        for date, text in history:
            lines.append(f'\n<digest date="{date}">\n{text.strip()}\n</digest>')
        lines.append("</previous_digests>\n")

    # Counts roundup links one by one, unlike the digest header: the format rules
    # key off this number, and what the model has to cover is the entries below.
    lines.append(f"{len(articles)} unread articles from the last {hours} hours:\n")
    for heading, feed_articles in sorted(by_feed.items()):
        lines.append(f"\n## {heading}")
        for a in feed_articles:
            line = f"- [{a['title']}]({a['url']})"
            if a["summary"]:
                line += f" — {a['summary']}"
            lines.append(line)
    return "\n".join(lines)


def summarize(
    articles: list[dict],
    hours: int,
    model: str,
    system_prompt: str = SYSTEM_PROMPT,
    history: list[tuple[str, str]] | None = None,
    api_base: str | None = None,
) -> str:
    return complete(
        model,
        full_system_prompt(system_prompt, articles, history),
        build_prompt(articles, hours, history),
        api_base,
    )


def complete(
    model: str, system_prompt: str, user_prompt: str, api_base: str | None = None
) -> str:
    """Send one system + user prompt to the model and return its text."""
    response = litellm.completion(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        api_base=api_base,
    )
    choice = response.choices[0]
    content = choice.message.content
    # Reasoning models can spend their whole output limit thinking and return
    # no text; writing that out would produce an empty digest.
    if not content or not content.strip():
        usage = getattr(response, "usage", None)
        tokens = getattr(usage, "completion_tokens", "unknown")
        raise RuntimeError(
            f"model returned no text (finish_reason={choice.finish_reason}, "
            f"completion_tokens={tokens})"
        )
    return content


def to_html(md_text: str, title: str) -> str:
    body = markdown(md_text, extensions=["footnotes"])
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ max-width: 800px; margin: 2em auto; padding: 0 1em; font-family: sans-serif; line-height: 1.6; overflow-wrap: break-word; }}
  a {{ color: #0066cc; }}
  h3 {{ margin-top: 1.5em; }}
</style>
</head>
<body>
{body}
</body>
</html>"""


async def main() -> None:
    # Before anything reads the environment: GREADER_PASSWORD below, and the
    # LLM provider's API key inside litellm. Variables already set in the
    # environment win over the file.
    load_dotenv(Path(".env"))

    parser = argparse.ArgumentParser(
        description="Generate an RSS digest",
        epilog="Secrets come from the environment (or ./.env): GREADER_PASSWORD "
        "and the LLM provider's API key, e.g. ANTHROPIC_API_KEY.",
    )

    greader = parser.add_argument_group("feed reader (GReader API)")
    greader.add_argument(
        "--greader-url",
        required=True,
        metavar="URL",
        help="GReader API endpoint, e.g. https://freshrss.example.com/api/greader.php",
    )
    greader.add_argument(
        "--greader-username",
        required=True,
        metavar="USER",
        help="GReader username",
    )
    greader.add_argument(
        "--mark-read",
        action="store_true",
        help="Mark fetched articles as read in the feed reader",
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
        "--prompt-file",
        type=Path,
        metavar="FILE",
        help="File containing a replacement system prompt",
    )
    llm.add_argument(
        "--print-prompt",
        action="store_true",
        help="Print the system prompt and user prompt then exit (no LLM call)",
    )

    digest = parser.add_argument_group("digest")
    digest.add_argument(
        "--hours",
        type=int,
        default=DEFAULT_HOURS,
        metavar="N",
        help=f"Hours of articles to include (default: {DEFAULT_HOURS})",
    )
    digest.add_argument(
        "--archive-dir",
        type=Path,
        metavar="DIR",
        help="Directory of past digests to send as context, so the LLM "
        "can follow ongoing threads and avoid repeating itself. "
        "Reads files named YYYY-MM-DD.md/.html or "
        "digest-YYYY-MM-DD.md; never writes to it",
    )
    digest.add_argument(
        "--history",
        type=int,
        default=None,
        metavar="N",
        help=f"How many digests from --archive-dir to send "
        f"(default: {DEFAULT_HISTORY})",
    )
    digest.add_argument(
        "--summary-length",
        type=int,
        default=DEFAULT_SUMMARY_LENGTH,
        metavar="N",
        help="Characters of each article's text to send to the LLM; feeds that "
        f"publish full text give it more to work with (default: "
        f"{DEFAULT_SUMMARY_LENGTH})",
    )
    digest.add_argument(
        "--html", action="store_true", help="Output HTML instead of Markdown"
    )
    digest.add_argument(
        "--output",
        type=Path,
        metavar="PATH",
        help="Output file path (default: ./digest-YYYY-MM-DD.md/html)",
    )
    digest.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress messages (errors are always shown)",
    )
    digest.add_argument(
        "--verbose",
        action="store_true",
        help="Report how much text each feed provides, to spot feeds that "
        "send only a teaser",
    )
    digest.add_argument(
        "--log-file", type=Path, metavar="FILE", help="Append log output to a file"
    )
    args = parser.parse_args()

    if args.log_file:
        logging.basicConfig(
            filename=args.log_file,
            level=logging.WARNING,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )

    # Only from the environment, so it never shows up in shell history or ps.
    password = os.environ.get("GREADER_PASSWORD")

    if not password:
        print("error: missing required value: $GREADER_PASSWORD", file=sys.stderr)
        sys.exit(1)

    if args.summary_length < 0:
        print("error: --summary-length must be 0 or more", file=sys.stderr)
        sys.exit(1)

    if args.history is not None and args.history > 0 and not args.archive_dir:
        print(
            "error: --history needs --archive-dir DIR to read digests from",
            file=sys.stderr,
        )
        sys.exit(1)

    # An explicit --archive-dir asserts the directory exists; a typo there would
    # otherwise just silently produce no history.
    if args.archive_dir and not args.archive_dir.is_dir():
        print(
            f"error: --archive-dir not a directory: {args.archive_dir}", file=sys.stderr
        )
        sys.exit(1)

    config = Config(
        url=args.greader_url,
        username=args.greader_username,
        password=password,
    )

    system_prompt = SYSTEM_PROMPT
    if args.prompt_file:
        system_prompt = args.prompt_file.read_text()

    hours = args.hours
    model = args.model
    date = datetime.now().strftime("%Y-%m-%d")
    ext = "html" if args.html else "md"
    output = args.output or Path(f"digest-{date}.{ext}")

    def log(msg: str) -> None:
        if not args.quiet:
            print(msg, file=sys.stderr)

    log(f"Fetching articles (last {hours}h)...")
    articles = await fetch_articles(config, hours, mark_read=args.mark_read)
    log(f"Got {post_count(articles)} articles")
    roundup_links = [a for a in articles if "roundup" in a]
    if roundup_links:
        log(
            f"Split {post_count(roundup_links)} roundup(s) into "
            f"{len(roundup_links)} links"
        )

    if not articles:
        log("No unread articles found.")
        return

    # Shown even with --quiet: asking for it is more specific than suppressing it.
    if args.verbose:
        print(content_report(articles), file=sys.stderr)
    articles = truncate_summaries(articles, args.summary_length)

    history = []
    if args.archive_dir:
        count = DEFAULT_HISTORY if args.history is None else args.history
        history = load_history(args.archive_dir, count, date)
        log(f"Including {len(history)} previous digest(s) from {args.archive_dir}")

    if args.print_prompt:
        print("=== SYSTEM PROMPT ===")
        print(full_system_prompt(system_prompt, articles, history))
        print("\n=== USER PROMPT ===")
        print(build_prompt(articles, hours, history))
        return

    log(f"Summarizing with {model}...")
    try:
        digest = summarize(
            articles,
            hours,
            model,
            system_prompt,
            history,
            api_base=args.model_url,
        )
    except Exception as e:
        msg = str(e).lower()
        if any(
            w in msg for w in ("credit", "billing", "quota", "insufficient", "payment")
        ):
            print(
                "error: out of API credits — check your account with the "
                f"provider for {model}: {e}",
                file=sys.stderr,
            )
        else:
            print(f"error: LLM call failed: {e}", file=sys.stderr)
        sys.exit(1)

    title = f"RSS Digest — {date}"
    site_count = len({a["feed_name"] for a in articles})
    stats = f"*{post_count(articles)} articles · {site_count} sites*"
    header = f"# {title}\n\n{stats}\n"
    footnote = f"\n---\n*Generated by `{model}`*"
    if args.html:
        content = to_html(f"{header}\n{digest}\n{footnote}", title)
    else:
        content = f"{header}\n{digest}\n{footnote}\n"
    output.write_text(content)
    log(f"Written to {output}")


def main_sync() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
