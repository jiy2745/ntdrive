"""Print the README tool table from the tool registry, or write it into a file.

    uv run python scripts/tools_table.py                    # print the table
    uv run python scripts/tools_table.py --write README.md  # replace the block between the markers

The block sits between `<!-- tools:start -->` and `<!-- tools:end -->`. A unit test compares the
README block with the registry, so the table cannot drift from the tool definitions.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ntdrive.core.registry import load_builtin_tools

START = "<!-- tools:start -->\n"
END = "<!-- tools:end -->"


def replace_block(text: str, table: str) -> str:
    """`text` with the table between the markers replaced by `table`."""
    head, rest = text.split(START, 1)
    _old, tail = rest.split(END, 1)
    return head + START + table + END + tail


def main(argv: list[str]) -> int:
    """Entry point."""
    table = load_builtin_tools().markdown_table()
    if "--write" in argv:
        path = Path(argv[argv.index("--write") + 1])
        path.write_text(
            replace_block(path.read_text(encoding="utf-8"), table), encoding="utf-8", newline="\n"
        )
        print(f"{path}: tool table written")
        return 0
    sys.stdout.write(table)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
