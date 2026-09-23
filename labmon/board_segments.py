"""Keep a truthful editor and time for each newline-delimited announcement paragraph."""
from __future__ import annotations

from difflib import SequenceMatcher


def initial_segments(text: str, updated_at: str | None = None):
    """Migrate old whole-board text without inventing per-paragraph authors."""
    if not text:
        return []
    return [{"text": line, "updated_at": updated_at, "updated_by": None, "legacy": True}
            for line in text.split("\n")]


def reconcile_segments(previous: list[dict], text: str, editor: str, updated_at: str):
    """Preserve metadata on unchanged lines; mark inserted/edited lines now."""
    old_lines = [segment["text"] for segment in previous]
    new_lines = text.split("\n") if text else []
    if len(old_lines) * len(new_lines) > 250_000:
        prefix = 0
        while prefix < min(len(old_lines), len(new_lines)) and old_lines[prefix] == new_lines[prefix]:
            prefix += 1
        suffix = 0
        while (suffix < min(len(old_lines), len(new_lines)) - prefix and
               old_lines[-suffix - 1] == new_lines[-suffix - 1]):
            suffix += 1
        changed = [{"text": line, "updated_at": updated_at, "updated_by": editor, "legacy": False}
                   for line in new_lines[prefix:len(new_lines) - suffix if suffix else len(new_lines)]]
        result = [dict(segment) for segment in previous[:prefix]] + changed
        if suffix:
            result.extend(dict(segment) for segment in previous[-suffix:])
        return result
    result = []
    for operation, old_start, old_end, new_start, new_end in SequenceMatcher(
            None, old_lines, new_lines, autojunk=False).get_opcodes():
        if operation == "equal":
            result.extend(dict(segment) for segment in previous[old_start:old_end])
        elif operation in {"replace", "insert"}:
            result.extend({"text": line, "updated_at": updated_at,
                           "updated_by": editor, "legacy": False}
                          for line in new_lines[new_start:new_end])
    return result
