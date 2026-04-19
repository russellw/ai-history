#!/usr/bin/env python3
"""
Convert ChatGPT chat history JSON to a grep-friendly text format.

Usage:
    python3 convert_chatgpt_history.py                        # outputs to conversations/ChatGPT.txt
    python3 convert_chatgpt_history.py -o output.txt          # custom output file
    python3 convert_chatgpt_history.py --stdout               # print to stdout
    python3 convert_chatgpt_history.py -d other/dir           # custom input directory
"""

import json
import sys
import argparse
import glob
import os
from datetime import datetime, timezone


def fmt_time(ts) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError, OSError):
        return str(ts)


def extract_text(content: dict) -> str:
    if not content:
        return ""
    ctype = content.get("content_type", "text")
    parts = content.get("parts", [])

    if ctype == "text":
        texts = [p for p in parts if isinstance(p, str) and p.strip()]
        return "\n\n".join(texts)

    if ctype == "multimodal_text":
        result_parts = []
        for p in parts:
            if isinstance(p, str) and p.strip():
                result_parts.append(p.strip())
            elif isinstance(p, dict):
                dalle = p.get("metadata", {}).get("dalle")
                if dalle and dalle.get("prompt"):
                    result_parts.append(f"[IMAGE: {dalle['prompt']}]")
                else:
                    ptr = p.get("asset_pointer", "")
                    result_parts.append(f"[IMAGE: {ptr}]")
        return "\n\n".join(result_parts)

    if ctype == "code":
        lang = content.get("language", "")
        text = content.get("text", "").strip()
        label = f"[CODE: {lang}]" if lang and lang != "unknown" else "[CODE]"
        return f"{label}\n{text}" if text else label

    if ctype == "execution_output":
        text = content.get("text", "").strip()
        return f"[CODE_OUTPUT]\n{text}" if text else "[CODE_OUTPUT: (empty)]"

    if ctype == "tether_quote":
        domain = content.get("domain", "")
        text = content.get("text", "").strip()
        header = f"[WEB_QUOTE: {domain}]" if domain else "[WEB_QUOTE]"
        return f"{header}\n{text}" if text else header

    if ctype == "tether_browsing_display":
        result = content.get("result", "").strip()
        return f"[BROWSING_RESULT]\n{result}" if result else "[BROWSING_RESULT: (empty)]"

    if ctype == "system_error":
        name = content.get("name", "")
        text = content.get("text", "").strip()
        header = f"[SYSTEM_ERROR: {name}]" if name else "[SYSTEM_ERROR]"
        return f"{header}\n{text}" if text else header

    # Fallback: dump as JSON
    return f"[{ctype.upper()}]\n{json.dumps(content, indent=2)}"


def get_message_chain(mapping: dict, current_node: str) -> list:
    """Walk parent links from current_node to root, then reverse to get chronological order."""
    chain = []
    node_id = current_node
    seen = set()
    while node_id and node_id not in seen:
        seen.add(node_id)
        node = mapping.get(node_id)
        if not node:
            break
        msg = node.get("message")
        if msg:
            chain.append(msg)
        node_id = node.get("parent")
    chain.reverse()
    return chain


def convert_file(input_path: str, output_file, seen_ids: set):
    with open(input_path, encoding="utf-8") as f:
        conversations = json.load(f)

    written = 0
    for conv in conversations:
        conv_id = conv.get("id") or conv.get("conversation_id", "")
        if conv_id in seen_ids:
            continue
        seen_ids.add(conv_id)

        title = conv.get("title") or "(untitled)"
        created = fmt_time(conv.get("create_time"))
        updated = fmt_time(conv.get("update_time"))

        output_file.write(f"=== [{created}] {title} ===\n")
        output_file.write(f"    id: {conv_id}  updated: {updated}\n\n")

        mapping = conv.get("mapping", {})
        current_node = conv.get("current_node", "")
        messages = get_message_chain(mapping, current_node)

        for msg in messages:
            role = msg.get("author", {}).get("role", "unknown")
            if role == "system":
                continue
            ts = fmt_time(msg.get("create_time"))
            sender = role.upper()
            text = extract_text(msg.get("content", {}))

            output_file.write(f"{sender} [{ts}]:\n")
            if text:
                indented = "\n".join("  " + line for line in text.splitlines())
                output_file.write(indented + "\n")
            else:
                output_file.write("  (no text content)\n")
            output_file.write("\n")

        output_file.write("\n")
        written += 1

    return written


def convert(input_dir: str, output_file):
    pattern = os.path.join(input_dir, "conversations-*.json")
    paths = sorted(glob.glob(pattern))
    if not paths:
        print(f"No conversations-*.json files found in {input_dir}", file=sys.stderr)
        sys.exit(1)

    seen_ids = set()
    total = 0
    for path in paths:
        n = convert_file(path, output_file, seen_ids)
        total += n
        print(f"  {os.path.basename(path)}: {n} conversations", file=sys.stderr)

    print(f"Done. {total} conversations written.", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Convert ChatGPT history JSON to grep-friendly text.")
    parser.add_argument("-d", "--dir", default="ChatGPT", help="Input directory containing conversations-*.json files (default: ChatGPT)")
    parser.add_argument("-o", "--output", default="conversations/ChatGPT.txt", help="Output text file (default: conversations/ChatGPT.txt)")
    parser.add_argument("--stdout", action="store_true", help="Write to stdout instead of a file")
    args = parser.parse_args()

    if args.stdout:
        convert(args.dir, sys.stdout)
    else:
        print(f"Converting {args.dir}/conversations-*.json -> {args.output} ...", file=sys.stderr)
        with open(args.output, "w", encoding="utf-8", newline="\n") as out:
            convert(args.dir, out)
        print(f"Output written to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
