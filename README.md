<div align="center">
<pre>
══════════════════════════
    R S S  D I G E S T
══════════════════════════
</pre>
</div>

Generates a digest from your self-hosted RSS reader using your choice of LLM. Instead of scrolling through hundreds of unread articles, you get a single readable summary grouped by theme — delivered as a Markdown or HTML file.

## What you need

- A self-hosted RSS reader that supports the [GReader API](#compatible-readers) (FreshRSS, Miniflux, etc.)
- An API key for an AI service (Anthropic, OpenAI, or a local model — see [Choosing a model](#choosing-a-model))
- [uv](https://docs.astral.sh/uv/) or [pipx](https://pipx.pypa.io/) for installation

## Installation

```bash
# with uv
uv tool install rss-digest

# with pipx
pipx install rss-digest
```

## Quick start

```bash
# Set your credentials
export GREADER_URL=https://freshrss.example.com
export GREADER_USERNAME=yourname
export GREADER_PASSWORD=yourpassword
export ANTHROPIC_API_KEY=sk-ant-...

# Generate a digest of the last 24 hours
rss-digest
```

The digest is written to `~/Desktop/digest-YYYY-MM-DD.md`. Pass `--html` to get an HTML file instead.

## Configuration

All options can be set on the command line. Credentials fall back to environment variables (or a `.env` file in the project root) if not provided.

| Option | Env var | Default |
|--------|---------|---------|
| `--url URL` | `GREADER_URL` | *(required)* |
| `--username USER` | `GREADER_USERNAME` | *(required)* |
| `--password PASS` | `GREADER_PASSWORD` | *(required)* |
| `--api-path PATH` | `GREADER_API_PATH` | `/api/greader.php` |
| `--hours N` | | `24` |
| `--model MODEL` | | `anthropic/claude-sonnet-4-6` |
| `--html` | | off |
| `--system-prompt-file FILE` | | built-in prompt |
| `--mark-read` | | off |
| `--output PATH` | | `./digest-YYYY-MM-DD.md` |
| `--quiet` | | off |
| `--log-file FILE` | | off |

```bash
# Last 48 hours, HTML output
rss-digest --hours 48 --html

# Use a different model
rss-digest --model openai/gpt-4o

# Pass credentials on the command line (not recommended for shared machines)
rss-digest --url https://freshrss.example.com --username alice --password s3cr3t
```

## Choosing a model

`rss-digest` uses [LiteLLM](https://docs.litellm.ai/), so any model it supports works. Set the `--model` flag to a LiteLLM model string and export the corresponding API key.

**Anthropic (default)**
```bash
export ANTHROPIC_API_KEY=sk-ant-...
rss-digest --model anthropic/claude-sonnet-4-6
```

**OpenAI**
```bash
export OPENAI_API_KEY=sk-...
rss-digest --model openai/gpt-4o
```

**Local model via [LM Studio](https://lmstudio.ai)**

LM Studio lets you run models on your own machine with no API key or usage costs.

1. Download and open LM Studio, load a model (Mistral, Llama, Gemma, etc.)
2. Start the local server: **Local Server** tab → **Start Server**
3. Copy the [model identifier] shown in the UI (e.g. `google/gemma-4-12b-qat`)
4. Invoke `rss-digest` with the `--model` parameter, but prefixing the model
   identifier with `openai/`, e.g. `openai/google/gemma-4-12b-qat`.

```bash
export OPENAI_BASE_URL=http://localhost:1234/v1
export OPENAI_API_KEY=lm-studio

rss-digest --model openai/google/openai/google/gemma-4-12b-qat
```

The `OPENAI_API_KEY` value is ignored by LM Studio but required by the LiteLLM library — set it to anything. The model name after `openai/` must match the identifier shown in LM Studio's server tab.

For digest-length tasks, a 7B–14B instruction-tuned model works well. Larger context windows (32k+) are helpful if you have many unread articles.

## Customizing the prompt

The built-in prompt decides how articles are grouped and how much to include. For ≤25 articles it lists everything; for larger batches it writes a curated prose summary.

```
You are producing a digest of RSS articles.

Format rules:
- ≤25 articles: Reading queue. Group by theme under ### headings. List every
  article as a bullet with a markdown link. Include the feed name.
- >25 articles: Curated TL;DR. Themed prose under ### headings. Cover roughly
  ⅓ of articles. Drop filler (sponsored posts, job listings, police blotters,
  sub-100-word link-outs). Dense single-feed clusters (6+ posts): pick 2–3
  representatives, then "and N more from [feed name]".

Every article title you mention must be an inline markdown link. Be concise.
```

To use your own prompt, write it to a text file and pass it with `--system-prompt-file`:

```bash
rss-digest --system-prompt-file my-prompt.txt
```

Example prompt that keeps things very short:

```
You are summarizing RSS articles for a busy reader.
Write a single short paragraph (5–8 sentences) covering the most important stories.
Do not use bullet points or headings. Every article title must be a markdown link.
```

The user message sent to the model is always a structured list of articles grouped by feed, so your prompt only needs to describe the desired output format.

The [`prompts/`](prompts/) directory contains example prompts contributed by users. To try one:

```bash
rss-digest --system-prompt-file prompts/demote-ai-news.txt
```

Please send prompts you like as PRs and I will add them to the `prompts/` directory!

### Inspecting the prompt

To see the exact system prompt and user message that would be sent to the model — without making any LLM call — use `--print-prompt`:

```bash
rss-digest --print-prompt
```

This is useful for iterating on a custom prompt, checking which articles were fetched, or piping the content to another tool.

## Example output

See [example.md](example.md) for a real digest. A short excerpt:

```markdown
# RSS Digest — 2026-06-10

*137 articles · 53 sites*

### Science & Environment

NASA [announced the all-male Artemis III crew](https://www.nytimes.com/2026/06/09/science/space/artemis-iii-astronauts-nasa-crew.html) and faces questions about [whether a 2028 moon landing is realistic](https://www.nytimes.com/2026/06/09/science/space/nasa-artemis-moon-landing.html) given dependence on Musk and Bezos. Indonesia's landslides [wiped out over 5% of the endangered orangutan population](https://www.nytimes.com/2026/06/10/climate/indonesia-floods-organgutans.html). And scientists [accidentally found that crowds worldwide spontaneously walk counterclockwise](https://www.404media.co/scientists-just-accidentally-discovered-a-strange-hidden-rule-of-human-nature/).

### Culture & Ideas

3 Quarks Daily examines [what AGI means for the future of physics](https://3quarksdaily.com/3quarksdaily/2026/06/adam-brown-agi-the-future-of-physics.html), and Dina Nayeri writes on [Iran's stolen revolution and the failures of diaspora narrative](https://3quarksdaily.com/3quarksdaily/2026/06/stolen-revolution.html). Iron & Wine's Sam Beam [reflects on always developing and changing as an artist](https://thecreativeindependent.com/people/musician-sam-beam-iron-and-wine-on-always-developing-and-changing/). A fascinating science/humanities crossover: [ELIZA and what a 1966 chatbot's horrified inventor tells us about modern voice AI](https://cafebedouin.org/2026/06/10/eliza-learned-to-breathe/).
```

## Running on a schedule

To get a fresh digest every morning, add a crontab entry. Because cron runs with a minimal environment, use the full path to the binary and set credentials directly in the crontab.

```bash
# find the full path first
which rss-digest
```

Then open your crontab with `crontab -e` and add:

```crontab
GREADER_URL=https://freshrss.example.com
GREADER_USERNAME=yourname
GREADER_PASSWORD=yourpassword
ANTHROPIC_API_KEY=sk-ant-...

# Run at 6am every day, write an HTML digest to a specific folder
0 6 * * * /Users/yourname/.local/bin/rss-digest --html --quiet --log-file /tmp/rss-digest.log --output /Users/yourname/Documents/digest.html
```

Crontab env-var lines apply to all jobs below them in the file, so you only need to set them once.

## Compatible readers

Any RSS reader that implements the GReader API should work:

| Reader | Notes |
|--------|-------|
| [FreshRSS](https://freshrss.org) | Enable the GReader API in Settings → Authentication |
| [Miniflux](https://miniflux.app) | Enable Google Reader API in Settings |
| [Tiny Tiny RSS](https://tt-rss.org) | Requires the [News+/GReader plugin](https://github.com/hrk/tt-rss-newsplus-plugin) |
| [The Old Reader](https://theoldreader.com) | Hosted service with GReader-compatible API |

The default `--api-path` (`/api/greader.php`) is correct for FreshRSS. Other readers may use a different path — check their documentation.
