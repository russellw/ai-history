#!/usr/bin/env python3
"""
Search all four AI chat-history exports for a regular expression and write
every matching conversation out as its own Markdown file.

A conversation matches if the regex is found in its *title* or in the body
text of *any* message (question or answer).  Matching conversations are
written to an output directory as Markdown files named after the conversation
title (with ``-2``/``-3``/... suffixes to disambiguate duplicate titles).

Reliability
-----------
Rather than re-parse the generated ``conversations/*.txt`` files (which would
be fragile), this script reuses the *exact* parsing logic of the four
converters by importing them.  For each provider it loads the most recent
extracted download — the same data ``convert_*_history.py`` would convert — so
each emitted Markdown file contains all and exactly that one conversation.

Usage:
    python3 search_history.py "PATTERN"
    python3 search_history.py "PATTERN" -o results_dir
    python3 search_history.py -i "pattern"          # case-insensitive
"""

import os
import re
import sys
import argparse

# Import the converters so we reuse their parsing verbatim.  Ensure this
# script's directory is importable regardless of the current working dir.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import convert_claude_history as claude
import convert_chatgpt_history as chatgpt
import convert_grok_history as grok
import convert_qwen_history as qwen


# --------------------------------------------------------------------------
# Per-provider adapters.
#
# Each yields a normalized conversation dict:
#   {provider, title, created, updated, id, messages: [(role, ts, text), ...]}
# built entirely from the converter's own functions, so it is identical to
# what that converter would write to its .txt output.
# --------------------------------------------------------------------------
def latest_download_dir(mod, provider_dir):
    """Return the most recent dated download dir for a provider, or None."""
    if not os.path.isdir(provider_dir):
        return None
    downloads = mod.list_download_dirs(provider_dir)
    return downloads[-1] if downloads else None


def claude_conversations(dated_dir):
    merged = claude.load_download(dated_dir)
    for conv in claude.sort_conversations(list(merged.values())):
        messages = []
        for msg in conv.get("chat_messages", []):
            role = msg.get("sender", "unknown")
            ts = claude.fmt_time(msg.get("created_at", ""))
            text = claude.extract_text(msg.get("content", []), msg.get("text", ""))
            messages.append((role, ts, text))
        yield {
            "provider": "Claude",
            "title": conv.get("name") or "(untitled)",
            "created": claude.fmt_time(conv.get("created_at", "")),
            "updated": claude.fmt_time(conv.get("updated_at", "")),
            "id": conv.get("uuid", ""),
            "messages": messages,
        }


def chatgpt_conversations(dated_dir):
    merged = chatgpt.load_download(dated_dir)
    for conv in chatgpt.sort_conversations(list(merged.values())):
        messages = []
        chain = chatgpt.get_message_chain(conv.get("mapping", {}),
                                          conv.get("current_node", ""))
        for msg in chain:
            role = msg.get("author", {}).get("role", "unknown")
            if role == "system":
                continue
            ts = chatgpt.fmt_time(msg.get("create_time"))
            text = chatgpt.extract_text(msg.get("content", {}))
            messages.append((role, ts, text))
        yield {
            "provider": "ChatGPT",
            "title": conv.get("title") or "(untitled)",
            "created": chatgpt.fmt_time(conv.get("create_time")),
            "updated": chatgpt.fmt_time(conv.get("update_time")),
            "id": chatgpt.conv_id(conv),
            "messages": messages,
        }


def grok_conversations(dated_dir):
    merged = grok.load_download(dated_dir)
    for conv in grok.sort_conversations(list(merged.values())):
        meta = conv.get("conversation", {})
        messages = []
        for resp in grok.reconstruct_thread(conv):
            role = resp.get("sender") or "unknown"
            ts = grok.fmt_time(resp.get("create_time", ""))
            text = grok.strip_grok_tags(resp.get("message") or "").strip()
            messages.append((role, ts, text))
        yield {
            "provider": "Grok",
            "title": meta.get("title") or "(untitled)",
            "created": grok.fmt_time(meta.get("create_time", "")),
            "updated": grok.fmt_time(meta.get("modify_time", "")),
            "id": meta.get("id", ""),
            "messages": messages,
        }


def qwen_conversations(dated_dir):
    merged = qwen.load_download(dated_dir)
    for conv in qwen.sort_conversations(list(merged.values())):
        messages = []
        for msg in qwen.active_thread(conv):
            role = msg.get("role") or "unknown"
            ts = qwen.fmt_time(msg.get("timestamp", ""))
            text = qwen.message_text(msg)
            messages.append((role, ts, text))
        yield {
            "provider": "Qwen",
            "title": conv.get("title") or "(untitled)",
            "created": qwen.fmt_time(conv.get("created_at", "")),
            "updated": qwen.fmt_time(conv.get("updated_at", "")),
            "id": conv.get("id", ""),
            "messages": messages,
        }


PROVIDERS = [
    ("Claude", claude, "Claude", claude_conversations),
    ("ChatGPT", chatgpt, "ChatGPT", chatgpt_conversations),
    ("Grok", grok, "Grok", grok_conversations),
    ("Qwen", qwen, "Qwen", qwen_conversations),
]


# --------------------------------------------------------------------------
# Matching, filenames, and Markdown output
# --------------------------------------------------------------------------
def conversation_matches(conv, pattern) -> bool:
    """True if the regex hits the title or any message body."""
    if pattern.search(conv["title"]):
        return True
    for _role, _ts, text in conv["messages"]:
        if text and pattern.search(text):
            return True
    return False


# Characters illegal in filenames on Windows (and awkward on any OS).
_BAD_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_title(title: str) -> str:
    """Turn a conversation title into a safe Markdown filename stem."""
    name = _BAD_CHARS_RE.sub(" ", title)
    name = re.sub(r"\s+", " ", name).strip()
    name = name.strip(". ")  # Windows dislikes trailing dots/spaces
    if len(name) > 120:
        name = name[:120].rstrip()
    return name or "untitled"


def unique_stem(stem: str, used: set, out_dir: str) -> str:
    """Return stem, or stem-2/stem-3/... avoiding collisions (case-insensitive)."""
    candidate = stem
    n = 2
    while (candidate.lower() in used
           or os.path.exists(os.path.join(out_dir, candidate + ".md"))):
        candidate = f"{stem}-{n}"
        n += 1
    used.add(candidate.lower())
    return candidate


def write_markdown(conv: dict, path: str):
    """Write one normalized conversation to a Markdown file (UTF-8, LF)."""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"# {conv['title']}\n\n")
        f.write(f"- **Provider:** {conv['provider']}\n")
        if conv["created"]:
            f.write(f"- **Created:** {conv['created']}\n")
        if conv["updated"]:
            f.write(f"- **Updated:** {conv['updated']}\n")
        if conv["id"]:
            f.write(f"- **ID:** {conv['id']}\n")
        f.write("\n")

        for role, ts, text in conv["messages"]:
            header = role.upper()
            if ts:
                header += f"  [{ts}]"
            f.write(f"## {header}\n\n")
            f.write((text if text else "*(no text content)*") + "\n\n")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Search all four AI history exports and write matching "
                    "conversations as Markdown files.")
    parser.add_argument("pattern", help="Regular expression to search for.")
    parser.add_argument("-o", "--output-dir", default="search_results",
                        help="Directory for the Markdown output "
                             "(default: search_results).")
    parser.add_argument("-i", "--ignore-case", action="store_true",
                        help="Case-insensitive matching.")
    parser.add_argument("--base-dir", default=".",
                        help="Directory holding the provider folders "
                             "(default: current directory).")
    args = parser.parse_args()

    try:
        flags = re.IGNORECASE if args.ignore_case else 0
        pattern = re.compile(args.pattern, flags)
    except re.error as e:
        print(f"Error: invalid regular expression: {e}", file=sys.stderr)
        return 1

    os.makedirs(args.output_dir, exist_ok=True)

    used = set()
    total = 0
    per_provider = {}

    for label, mod, subdir, generator in PROVIDERS:
        provider_dir = os.path.join(args.base_dir, subdir)
        dated_dir = latest_download_dir(mod, provider_dir)
        if dated_dir is None:
            print(f"{label}: no download found (skipping).", file=sys.stderr)
            per_provider[label] = 0
            continue

        print(f"{label}: searching {os.path.basename(dated_dir)} ...",
              file=sys.stderr)
        count = 0
        for conv in generator(dated_dir):
            if not conversation_matches(conv, pattern):
                continue
            stem = unique_stem(sanitize_title(conv["title"]), used, args.output_dir)
            write_markdown(conv, os.path.join(args.output_dir, stem + ".md"))
            count += 1
        per_provider[label] = count
        total += count
        print(f"{label}: {count} match(es).", file=sys.stderr)

    print(f"\nWrote {total} matching conversation(s) to {args.output_dir}/",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
