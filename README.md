# ai-history

Convert AI chat history exports into a **grep-friendly plain-text format** — one
file per provider. Supports **Claude**, **ChatGPT**, **Grok**, and **Qwen**.

Each provider's export has a completely different shape (different file layouts,
message trees, timestamp encodings). These scripts normalize all of them into
the same simple, greppable transcript so you can search your whole history with
`grep`, `rg`, an editor, or any text tool.

## Requirements

- Python 3 (standard library only — no dependencies, no virtualenv needed)

## Quick start

1. Download your export from the provider (see [Getting your export](#getting-your-export)).
2. Drop the downloaded file(s) into the matching provider directory in this repo:
   `Claude/`, `ChatGPT/`, `Grok/`, or `Qwen/`.
3. Run the matching converter:

   ```sh
   python3 convert_claude_history.py
   python3 convert_chatgpt_history.py
   python3 convert_grok_history.py
   python3 convert_qwen_history.py
   ```

The transcript is written to `conversations/<Provider>.txt`.

## Getting your export

| Provider | What you download | Where to put it |
| --- | --- | --- |
| **Claude** | One or more `data-*.zip` batch files plus `manifest-*.json` | `Claude/` |
| **ChatGPT** | A single export `.zip` (name embeds the export date) | `ChatGPT/` |
| **Grok** | A single `<uuid>.zip` | `Grok/` |
| **Qwen** | A single `chat-export-<epoch-ms>.json` | `Qwen/` |

You can drop the files in exactly as downloaded — the scripts handle unzipping,
merging multi-part exports, and organizing them. The provider data directories
are gitignored, so your private history never gets committed.

## How it works

All four converters share the same pipeline. When you run one, it:

1. **Determines the download's date** (from the manifest, the zip filename, the
   zip's internal timestamps, or the export filename — whatever that provider
   offers, falling back to the file's mtime).
2. **Archives the download** into a dated subdirectory `<Provider>/<YYYY-MM-DD>/`
   (zips are also extracted there), so every download is kept and a re-download
   never clobbers the previous one.
3. **Merges** that download's conversations, keyed by conversation id.
4. **Superset safeguard:** before overwriting the output, it checks that the
   latest download is a *superset* of the most recent previous one — no
   conversations dropped and none with fewer messages. If something looks like a
   truncated or incomplete download, it **refuses to overwrite** and exits with
   code `2`. Pass `--force` to override.
5. **Writes** `conversations/<Provider>.txt`, conversations sorted by creation
   time.

Re-running with no new download present simply re-converts the latest archived
download.

## Output format

Every converter produces the same format. Each conversation starts with a
single-line header (easy to use as a grep anchor):

```
=== [YYYY-MM-DD HH:MM] Title ===
    id: <conversation id>  updated: YYYY-MM-DD HH:MM

USER [YYYY-MM-DD HH:MM]:
  indented message body

ASSISTANT [YYYY-MM-DD HH:MM]:
  indented message body
```

Files are written as UTF-8 with UNIX line endings.

## Options

All converters accept the same flags:

| Flag | Description |
| --- | --- |
| `-o, --output PATH` | Output file (default: `conversations/<Provider>.txt`) |
| `--stdout` | Write to stdout instead of a file |
| `--force` | Overwrite the output even if the superset check fails |
| `--<provider>-dir DIR` | Override the input directory (e.g. `--claude-dir`, `--chatgpt-dir`, `--grok-dir`, `--qwen-dir`) |

## Repository layout

```
convert_claude_history.py    Claude  (Claude/data-*.zip)              -> conversations/Claude.txt
convert_chatgpt_history.py   ChatGPT (ChatGPT/*.zip)                  -> conversations/ChatGPT.txt
convert_grok_history.py      Grok    (Grok/<uuid>.zip)               -> conversations/Grok.txt
convert_qwen_history.py      Qwen    (Qwen/chat-export-*.json)        -> conversations/Qwen.txt
Claude/ ChatGPT/ Grok/ Qwen/ provider export data (gitignored)
conversations/               generated transcripts (gitignored)
```

Scripts live in the repository root so they aren't gitignored alongside their
data directories. Provider-specific format details are documented in
[`CLAUDE.md`](CLAUDE.md).

## License

[MIT](LICENSE) © Russell Wallace
