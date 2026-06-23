# ai_chat_archive 🧵

A single Python script that collects exported chat JSON files from various AI chat services and weaves them into one searchable, offline HTML archive.

No server, no database, no account, no internet connection required. Just a script and a browser.

---

## ✨ What it does

- 📂 Recursively scans a folder (and all subfolders) for `.json` files
- 🔍 Auto-detects which AI service each file came from, even when field names differ wildly between exports
- 🧩 Normalizes everything into one consistent shape: chat title, model, messages, roles, timestamps, attachments
- 🛠️ Auto-repairs mildly broken JSON (trailing commas, stray characters) so messy exports don't get silently dropped
- 🌐 Generates **one self-contained `.html` file** with every chat, a sidebar, and built-in search — works by just double-clicking it, no Python needed afterward
![main page](example\1.png)

## 🔒 Privacy

- **100% local.** Nothing is uploaded anywhere. The script never makes a network request.
- **Fully open source.** Every line is in the one `.py` file — read it, audit it, fork it.
- The output `.html` is also fully self-contained (no CDN scripts, no external fonts, no tracking) — open it on a USB stick, an air-gapped machine, ten years from now, it'll still work.

## 🤖 Currently supported chat exports

| Service | Notes |
|---|---|
| ChatGPT | both single-conversation and multi-conversation export formats |
| Claude | Anthropic's standard conversation export |
| Gemini / Bard | `role` + `parts` message format |
| DeepSeek | including reasoning/`<think>` blocks, shown collapsed |
| Qwen | including reasoning summaries |
| Grok | xAI's export format |
| Generic fallback | any reasonably-shaped `{messages: [{role, content}]}` JSON, even from services not explicitly supported |

If your export doesn't match any known format, the script will still try a best-effort generic parse rather than just giving up (see `DEFAULT_ON_UNKNOWN` below).

---

## 🚀 Quick start

```bash
python ai_chat_archive.py
```

That's it — by default it scans the current folder and writes `chat_archive.html` next to the script. Open that file in any browser.

```bash
# scan a specific folder
python ai_chat_archive.py ./my_chat_exports

# custom output filename
python ai_chat_archive.py -o my_archive.html

# see what got detected for each file
python ai_chat_archive.py -v
```

---

## ⚙️ Configuration: CLI flags vs. config variables

There are two ways to control the script's behavior, and they work together:

1. **Config variables** at the very top of `ai_chat_archive.py` — edit these if you always want the same behavior and don't want to type flags every time.
2. **CLI flags** — passed when running the script. **A CLI flag always overrides the matching config variable** for that run, everything else falls back to the config default.

```python
# ── inside ai_chat_archive.py ──
DEFAULT_DIRECTORY: str  = ""                 # "" = current folder
DEFAULT_OUTPUT: str     = "chat_archive.html"
DEFAULT_VERBOSE: bool   = False
DEFAULT_TOOLTIPS: bool  = False
DEFAULT_ON_UNKNOWN: str = "try"              # "try" | "ignore"
```

| Config variable | CLI flag | What it does |
|---|---|---|
| `DEFAULT_DIRECTORY` | `directory` (positional) | Folder to scan recursively. Empty string = current working directory. |
| `DEFAULT_OUTPUT` | `-o`, `--output` | Filename of the generated HTML archive. |
| `DEFAULT_VERBOSE` | `-v`, `--verbose` | Prints which parser matched each file and how many messages it extracted — useful for debugging a format that isn't being picked up correctly. |
| `DEFAULT_TOOLTIPS` | `--tooltips` | Adds a hover tooltip on every message showing its raw JSON fields (truncated to ~50 chars for long values). Handy for inspecting exports, off by default to keep the HTML lean. |
| `DEFAULT_ON_UNKNOWN` | `--on-unknown {try,ignore}` | `try` (default): unrecognized JSON files get a best-effort generic parse instead of being skipped. `ignore`: unrecognized files are silently skipped. |

**Example:** if you set `DEFAULT_TOOLTIPS = True` in the config but run with no flags, tooltips will be on. If you then run `python ai_chat_archive.py --tooltips` it's already on either way — but the flag lets you flip it on for one run without editing the file, e.g. `python ai_chat_archive.py -v --on-unknown ignore` for a quick noisy/strict pass.

---

## 🔎 Search features in the generated HTML

The output archive has a sidebar (all chats, filterable by title) and a toolbar above the messages with:

- **Search modes** — `Plain` (substring), `Regex` (full JS regex), `Fuzzy` (typo-tolerant matching)
- **Search scope** — `All` messages, `User` only, `AI` only, or `Titles` only
- Match navigation (▲ ▼ or <kbd>Enter</kbd> / <kbd>Shift+Enter</kbd>), live highlight count, jump-to-chat sidebar sync
- 🌙 / ☀️ theme toggle (light by default), remembered across reloads

All of this runs in plain JavaScript embedded in the HTML — no frameworks, no build step, no internet required to use the search.

---

## 🧩 Adding support for a new AI service

You usually don't need to write real parsing logic — just describe **where things live** in the JSON using a small path syntax, and the base parser classes handle traversal, role normalization, timestamps, and attachments automatically.

Path syntax (colon-separated):
- `"key"` → step into `dict[key]`
- `"*"` → expand the current level (a list yields all its items, a dict yields all its `.values()`)

```python
class MyNewAIParser(PathParser):
    name = "mynewai"
    chats_path     = "conversations:*"     # where to find each chat
    messages_path  = "turns:*"             # where to find each message, relative to a chat
    title_fields   = ["title", "subject"]
    role_fields    = ["role", "speaker"]
    content_fields = ["content", "text"]
    time_fields    = ["timestamp", "created"]
    model_fields   = ["model"]
    id_fields      = ["id"]
```

Then register it by adding `MyNewAIParser()` to `FormatRegistry.PARSERS`. Done.

For tree-structured exports where messages are linked via parent/children pointers instead of a flat list (like ChatGPT or DeepSeek), subclass `TreeParser` instead — see `ChatGPTParser` / `DeepSeekParser` in the script for real working examples, including how to override message-extraction logic for unusual content shapes (e.g. DeepSeek's `fragments` list with `REQUEST`/`THINK`/`RESPONSE` types).

The full guide with more detail lives as a comment block directly above the parser classes in `ai_chat_archive.py`.

---

## 📋 Requirements

- Python 3.8+ (standard library only — no `pip install` needed)
- Any modern browser to open the resulting HTML (Chrome, Firefox, Edge, Safari)
- Works identically on Windows, macOS, and Linux

## ⚠️ Known limitations

- Branching conversations (where you edited a message and got multiple AI responses) are flattened to the most recent branch — older branches aren't shown.
- Very large archives (thousands of chats) will produce a correspondingly large HTML file; the in-browser search is fast but the initial page load scales with total content size.
- Image *attachments* are listed by filename but not rendered inline (most exports don't embed the actual image bytes, only a reference/filename).
