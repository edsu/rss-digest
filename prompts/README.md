# Prompts

These are example prompts you can use by overriding the default with `--system-prompt-file` when running rss-digest.

```bash
rss-digest --system-prompt-file prompts/demote-ai-news.txt
```

* **edsu**: My evolving prompt that focuses on things I like, and how I want the
  digest to look.

* **gemma-4.txt**: Simplified for gemini/gemma-4-e4b-qat running locally in LM
  Studio, which seems to not include links when the instructions are sufficiently complicated.
