#!/usr/bin/env python3
"""Generate an RSS digest from FreshRSS articles using an LLM.

Model is selected via DIGEST_MODEL env var (default: anthropic/claude-sonnet-4-6).
Any litellm-supported model string works, e.g.:
  anthropic/claude-sonnet-4-6   (needs ANTHROPIC_API_KEY)
  openai/gpt-4o                 (needs OPENAI_API_KEY)
  openai/mistral-7b-instruct    (LM Studio: also set OPENAI_BASE_URL=http://localhost:1234/v1)

Time window defaults to 24 hours; override with DIGEST_HOURS.
"""

import argparse
import asyncio
import html
import logging
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import litellm

from rss_digest.greader import Config, GReaderClient

ROOT = Path(__file__).parent.parent.parent  # project root, where .env lives
DEFAULT_MODEL = "anthropic/claude-sonnet-4-6"
DEFAULT_HOURS = 24
DEFAULT_API_PATH = "/api/greader.php"
MAX_ARTICLES = 2000
MAX_SUMMARY_LENGTH = 300

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


def strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


async def fetch_articles(config: Config, hours: int, mark_read: bool = False) -> list[dict]:
    client = GReaderClient(config)
    cutoff = int(time.time()) - hours * 3600
    try:
        articles = await client.get_articles(limit=MAX_ARTICLES, since_timestamp=cutoff)
        result = [
            {**a.to_dict(), "summary": strip_html(a.summary)[:MAX_SUMMARY_LENGTH]}
            for a in articles
            if a.published >= cutoff
        ]
        if mark_read and result:
            await client.mark_as_read([a["id"] for a in result])
        return result
    finally:
        await client.aclose()


def build_prompt(articles: list[dict], hours: int) -> str:
    by_feed: dict[str, list[dict]] = defaultdict(list)
    for a in articles:
        by_feed[a["feed_name"]].append(a)

    lines = [f"{len(articles)} unread articles from the last {hours} hours:\n"]
    for feed_name, feed_articles in sorted(by_feed.items()):
        lines.append(f"\n## {feed_name}")
        for a in feed_articles:
            line = f"- [{a['title']}]({a['url']})"
            if a["summary"]:
                line += f" — {a['summary']}"
            lines.append(line)
    return "\n".join(lines)


def summarize(articles: list[dict], hours: int, model: str, system_prompt: str = SYSTEM_PROMPT) -> str:
    response = litellm.completion(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": build_prompt(articles, hours)},
        ],
        max_tokens=4096,
    )
    return response.choices[0].message.content



def to_html(md_text: str, title: str) -> str:
    import markdown as md_lib
    body = md_lib.markdown(md_text)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ max-width: 800px; margin: 2em auto; font-family: sans-serif; line-height: 1.6; }}
  a {{ color: #0066cc; }}
  h3 {{ margin-top: 1.5em; }}
</style>
</head>
<body>
{body}
</body>
</html>"""


async def main() -> None:
    parser = argparse.ArgumentParser(description="Generate an RSS digest")
    parser.add_argument("--html", action="store_true", help="Output HTML instead of Markdown")
    parser.add_argument("--hours", type=int, default=DEFAULT_HOURS, metavar="N",
                        help=f"Hours of articles to include (default: {DEFAULT_HOURS})")
    parser.add_argument("--model", default=DEFAULT_MODEL, metavar="MODEL",
                        help=f"LiteLLM model string (default: {DEFAULT_MODEL})")
    parser.add_argument("--url", default=os.environ.get("GREADER_URL"), metavar="URL",
                        help="GReader service base URL (default: $GREADER_URL)")
    parser.add_argument("--username", default=os.environ.get("GREADER_USERNAME"), metavar="USER",
                        help="GReader username (default: $GREADER_USERNAME)")
    parser.add_argument("--password", default=os.environ.get("GREADER_PASSWORD"), metavar="PASS",
                        help="GReader password (default: $GREADER_PASSWORD)")
    parser.add_argument("--api-path", default=os.environ.get("GREADER_API_PATH", DEFAULT_API_PATH),
                        metavar="PATH", help=f"GReader API path (default: {DEFAULT_API_PATH})")
    parser.add_argument("--system-prompt-file", type=Path, metavar="FILE",
                        help="File containing a replacement system prompt")
    parser.add_argument("--mark-read", action="store_true",
                        help="Mark fetched articles as read in the feed reader")
    parser.add_argument("--output", type=Path, metavar="PATH",
                        help="Output file path (default: ./digest-YYYY-MM-DD.md/html)")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress progress messages (errors are always shown)")
    parser.add_argument("--print-prompt", action="store_true",
                        help="Print the system prompt and user prompt then exit (no LLM call)")
    parser.add_argument("--log-file", type=Path, metavar="FILE",
                        help="Append log output to a file")
    args = parser.parse_args()

    if args.log_file:
        logging.basicConfig(filename=args.log_file, level=logging.WARNING,
                            format="%(asctime)s %(name)s %(levelname)s %(message)s")

    missing = [name for name, val in [
        ("--url / $GREADER_URL", args.url),
        ("--username / $GREADER_USERNAME", args.username),
        ("--password / $GREADER_PASSWORD", args.password),
    ] if not val]
    if missing:
        for m in missing:
            print(f"error: missing required value: {m}", file=sys.stderr)
        sys.exit(1)

    config = Config(
        _env_file=str(ROOT / ".env"),
        url=args.url,
        username=args.username,
        password=args.password,
        api_path=args.api_path,
    )

    system_prompt = SYSTEM_PROMPT
    if args.system_prompt_file:
        system_prompt = args.system_prompt_file.read_text()

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
    log(f"Got {len(articles)} articles")

    if not articles:
        log("No unread articles found.")
        return

    if args.print_prompt:
        print("=== SYSTEM PROMPT ===")
        print(system_prompt)
        print("\n=== USER PROMPT ===")
        print(build_prompt(articles, hours))
        return

    log(f"Summarizing with {model}...")
    try:
        digest = summarize(articles, hours, model, system_prompt)
    except Exception as e:
        msg = str(e).lower()
        if any(w in msg for w in ("credit", "billing", "quota", "insufficient", "payment")):
            print("error: out of API credits — check your account at console.anthropic.com", file=sys.stderr)
        else:
            print(f"error: LLM call failed: {e}", file=sys.stderr)
        sys.exit(1)

    title = f"RSS Digest — {date}"
    site_count = len({a["feed_name"] for a in articles})
    stats = f"*{len(articles)} articles · {site_count} sites*"
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
