"""One output convention for the hand-written commands and the setup scripts.

Section headers are `== n/total title`. Result lines carry a fixed-width tag in column 3 (OK,
FAIL, WARN, INFO, or `..` for something still running), then the subject, then a detail after a
colon. A failure is followed by `fix:` lines that say exactly what to do. The last line of a run
is a verdict, ALL SET, DONE or NOT READY, and NOT READY is followed by numbered next steps.
Secrets never appear. scripts/setup-host.ps1 and scripts/setup-guest.ps1 print the same shapes.
"""

from __future__ import annotations

from collections.abc import Callable

import click

Out = Callable[[str], None]


def section(index: int, total: int, title: str, out: Out = click.echo) -> None:
    """`== n/total title`."""
    out(f"== {index}/{total} {title}")


def _line(tag: str, subject: str, detail: str, out: Out) -> None:
    text = f"  {tag:<5} {subject}"
    out(text + (f": {detail}" if detail else ""))


def ok(subject: str, detail: str = "", out: Out = click.echo) -> None:
    """A check or step that succeeded."""
    _line("OK", subject, detail, out)


def fail(subject: str, detail: str = "", out: Out = click.echo) -> None:
    """A check or step that failed. Follow it with fix()."""
    _line("FAIL", subject, detail, out)


def warn(subject: str, detail: str = "", out: Out = click.echo) -> None:
    """Something to look at that does not stop the run."""
    _line("WARN", subject, detail, out)


def info(subject: str, detail: str = "", out: Out = click.echo) -> None:
    """Context the reader needs, such as what a prompt is asking for."""
    _line("INFO", subject, detail, out)


def running(subject: str, detail: str = "", out: Out = click.echo) -> None:
    """A step that is in progress and may take a while."""
    _line("..", subject, detail, out)


def fix(text: str, out: Out = click.echo) -> None:
    """What to do about the FAIL line above."""
    out(f"        fix: {text}")


def verdict(word: str, text: str, out: Out = click.echo) -> None:
    """The last line: ALL SET, DONE or NOT READY."""
    out(f"{word}: {text}")


def next_steps(steps: list[str], out: Out = click.echo) -> None:
    """Numbered steps under a verdict."""
    out("  next:")
    for index, step in enumerate(steps, start=1):
        out(f"    {index}. {step}")
