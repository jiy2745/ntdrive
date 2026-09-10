"""Reject non-ASCII characters, and semicolons in Markdown prose, in repository files.

This is the enforcement hook for the writing rules in the PRD: everything except PRD.md is
English, documents carry no emoji, em dashes, decorative symbols or box-drawing characters, and
prose does not use semicolons as punctuation. Plain ASCII covers the first three rules. The
semicolon rule applies to Markdown text outside fenced code blocks and inline code spans, so
commands like `cmd; .echo done` stay legal.

Usage: python scripts/check_ascii.py FILE [FILE ...]
Exit code 1 when a violation is found. Excluded files come from [tool.check_ascii] exclude in
pyproject.toml plus the exclude pattern in .pre-commit-config.yaml.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INLINE_CODE = re.compile(r"`[^`]*`")


def excluded_files() -> set[Path]:
    """Read the exclude list from pyproject.toml."""
    pyproject = ROOT / "pyproject.toml"
    if not pyproject.exists():
        return set()
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    names = data.get("tool", {}).get("check_ascii", {}).get("exclude", [])
    return {(ROOT / name).resolve() for name in names}


def check_ascii(path: Path, raw: bytes) -> int:
    """Report lines with bytes above 0x7F."""
    bad = 0
    for lineno, line in enumerate(raw.splitlines(), start=1):
        for col, byte in enumerate(line, start=1):
            if byte > 0x7F:
                snippet = line.decode("utf-8", errors="replace").strip()
                print(f"{path}:{lineno}:{col}: non-ASCII character in: {snippet[:80]}")
                bad += 1
                break
    return bad


def check_markdown_semicolons(path: Path, text: str) -> int:
    """Report semicolons in Markdown prose (outside code fences and inline code)."""
    bad = 0
    in_fence = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence or stripped.startswith("    ") or line.startswith("\t"):
            continue
        prose = INLINE_CODE.sub("", line)
        if ";" in prose:
            print(f"{path}:{lineno}: semicolon in prose: {stripped[:80]}")
            bad += 1
    return bad


def main(argv: list[str]) -> int:
    """Check every file given on the command line."""
    skip = excluded_files()
    bad = 0
    for arg in argv:
        path = Path(arg)
        if path.resolve() in skip:
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        bad += check_ascii(path, raw)
        if path.suffix.lower() == ".md":
            bad += check_markdown_semicolons(path, raw.decode("utf-8", errors="replace"))
    if bad:
        print(f"{bad} violation(s). English, plain ASCII, and no semicolons in prose.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
