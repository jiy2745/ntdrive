"""JSONL audit log of every tool call with secrets masked."""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

SECRET_KEY = re.compile(r"(password|passwd|secret|token|key)$", re.IGNORECASE)


def mask_secrets(value: Any) -> Any:
    """Return a copy with values under secret-looking keys replaced by ***."""
    if isinstance(value, dict):
        return {
            k: ("***" if SECRET_KEY.search(str(k)) and v not in (None, "") else mask_secrets(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [mask_secrets(v) for v in value]
    return value


class AuditLog:
    """Append-only JSONL writer. Thread-safe because kd/term reader threads may log."""

    def __init__(self, path: Path, t_plus: Any) -> None:
        self.path = path
        self._t_plus = t_plus
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        tool: str,
        args: dict[str, Any],
        *,
        caller: str,
        ok: bool,
        elapsed_ms: float,
        result: Any = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """Write one line. Results are truncated to keep the file readable."""
        entry: dict[str, Any] = {
            "ts": time.time(),
            "t_plus": self._t_plus(),
            "tool": tool,
            "caller": caller,
            "args": mask_secrets(args),
            "ok": ok,
            "elapsed_ms": round(elapsed_ms, 1),
        }
        if error is not None:
            entry["error"] = error
        elif result is not None:
            text = json.dumps(mask_secrets(result), ensure_ascii=True, default=str)
            entry["result"] = text if len(text) <= 2000 else text[:2000] + "...(truncated)"
        line = json.dumps(entry, ensure_ascii=True, default=str)
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
