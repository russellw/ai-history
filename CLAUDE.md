Because of the Windows/Python code page nonsense, all Python code that opens any data file for reading or writing, must always explicitly specify encoding as UTF-8
Every program that opens any text file for output, must specify UNIX line endings

# Project overview

This repo converts AI chat history exports into a grep-friendly plain text format, one file per provider.

# Directory structure

- `convert_claude_history.py` — converts Claude export (`Claude/conversations.json`) → `conversations/Claude.txt`
- `convert_chatgpt_history.py` — converts ChatGPT export (`ChatGPT/conversations-*.json`) → `conversations/ChatGPT.txt`
- `Claude/` — Claude export data (gitignored)
- `ChatGPT/` — ChatGPT export data (gitignored); multiple numbered JSON files plus supporting assets
- `conversations/` — output directory (gitignored)

# Scripts live in the root to avoid being gitignored along with their data directories

# Output format (both converters)

Each conversation starts with a header line:
    === [YYYY-MM-DD HH:MM] Title ===
        uuid/id: ...  updated: ...

Then each message:
    ROLE [YYYY-MM-DD HH:MM]:
      indented body text

# Provider-specific notes

Claude: flat list of conversations in one JSON file; messages are a flat list with `sender` field; timestamps are ISO strings; content is a list of typed blocks (`text`, `tool_use`, `tool_result`).

ChatGPT: conversations spread across multiple numbered JSON files (`conversations-000.json` etc.); messages stored as a tree (`mapping` dict), walked from `current_node` via parent links; timestamps are Unix epoch floats; `system` role messages are skipped; content types include `text`, `code`, `execution_output`, `tether_quote`, `tether_browsing_display`, `multimodal_text`, `system_error`.
