#!/usr/bin/env python3
"""
Convert Claude chat history JSON to a grep-friendly text format.

Usage:
    python3 convert_history.py                        # outputs to conversations.txt
    python3 convert_history.py -o output.txt          # custom output file
    python3 convert_history.py --stdout               # print to stdout
    python3 convert_history.py -i other.json          # custom input file
"""

import json
import sys
import argparse
from datetime import datetime, timezone


def fmt_time(ts: str) -> str:
    """Format ISO timestamp to a compact local-ish form: 2025-02-10 15:00"""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts


def extract_text(content_blocks: list, text_fallback: str) -> str:
    """
    Extract human-readable text from a list of content blocks.
    Falls back to the top-level 'text' field if no blocks yield text.
    """
    if not content_blocks:
        return text_fallback or ""

    parts = []
    for block in content_blocks:
        btype = block.get("type", "")
        if btype == "text":
            t = block.get("text", "").strip()
            if t:
                parts.append(t)
        elif btype == "tool_use":
            name = block.get("name", "unknown_tool")
            inp = block.get("input", {})
            parts.append(f"[TOOL_USE: {name}]")
            if inp:
                try:
                    parts.append(json.dumps(inp, indent=2))
                except (TypeError, ValueError):
                    parts.append(str(inp))
        elif btype == "tool_result":
            sub = block.get("content", [])
            result_parts = []
            if isinstance(sub, list):
                for sb in sub:
                    if sb.get("type") == "text":
                        t = sb.get("text", "").strip()
                        if t:
                            result_parts.append(t)
            elif isinstance(sub, str):
                result_parts.append(sub)
            if result_parts:
                parts.append(f"[TOOL_RESULT]\n" + "\n".join(result_parts))
            else:
                parts.append("[TOOL_RESULT: (empty)]")

    return "\n\n".join(parts) if parts else (text_fallback or "")


def convert(input_path: str, output_file):
    with open(input_path, encoding="utf-8") as f:
        conversations = json.load(f)

    total = len(conversations)
    for i, conv in enumerate(conversations, 1):
        name = conv.get("name") or "(untitled)"
        created = fmt_time(conv.get("created_at", ""))
        updated = fmt_time(conv.get("updated_at", ""))
        uuid = conv.get("uuid", "")

        # Conversation header — single line, easy to grep as an anchor
        output_file.write(
            f"=== [{created}] {name} ===\n"
        )
        output_file.write(f"    uuid: {uuid}  updated: {updated}\n\n")

        messages = conv.get("chat_messages", [])
        for msg in messages:
            sender = msg.get("sender", "unknown").upper()
            ts = fmt_time(msg.get("created_at", ""))
            text = extract_text(msg.get("content", []), msg.get("text", ""))

            output_file.write(f"{sender} [{ts}]:\n")
            if text:
                # Indent each line so the sender label stands out
                indented = "\n".join("  " + line for line in text.splitlines())
                output_file.write(indented + "\n")
            else:
                output_file.write("  (no text content)\n")
            output_file.write("\n")

        output_file.write("\n")

        if i % 100 == 0:
            print(f"  {i}/{total} conversations processed...", file=sys.stderr)

    print(f"Done. {total} conversations written.", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Convert Claude history JSON to grep-friendly text.")
    parser.add_argument("-i", "--input", default="Claude/conversations.json", help="Input JSON file (default: Claude/conversations.json)")
    parser.add_argument("-o", "--output", default="conversations.txt", help="Output text file (default: conversations.txt)")
    parser.add_argument("--stdout", action="store_true", help="Write to stdout instead of a file")
    args = parser.parse_args()

    if args.stdout:
        convert(args.input, sys.stdout)
    else:
        print(f"Converting {args.input} -> {args.output} ...", file=sys.stderr)
        with open(args.output, "w", encoding="utf-8") as out:
            convert(args.input, out)
        print(f"Output written to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
