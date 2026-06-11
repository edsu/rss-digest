# Prompts

These are example prompts you can use by overriding the default with `--system-prompt-file` when running rss-digest.

```bash
rss-digest --system-prompt-file prompts/demote-ai-news.txt
```

* **demote-ai-news.txt**: Adjusts for AI news dominating the digest — pushes it
  lower so that humanities and academic research get more prominence.

* **gemma-4.txt**: Tuned for smaller local models (e.g. Gemma 4). Gives
  preference to arts, humanities, and research content while still including
  technical posts. Adds an "Odds n' Ends" section for unusual items.
