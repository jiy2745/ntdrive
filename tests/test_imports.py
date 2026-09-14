"""The stateless clients must not drag the daemon stack (paramiko, aiohttp) into every command."""

import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
PROGRAM = (
    "import sys, ntdrive.cli.main, ntdrive.mcp.server; "
    "from ntdrive.core.registry import load_builtin_tools; load_builtin_tools(); "
    "heavy = {'paramiko', 'aiohttp', 'ntdrive.core.service'} & set(sys.modules); "
    "sys.exit('imported ' + ', '.join(sorted(heavy)) if heavy else 0)"
)


def test_cli_and_mcp_imports_stay_light() -> None:
    # A fresh interpreter, because pytest itself has already imported the service.
    result = subprocess.run(
        [sys.executable, "-c", PROGRAM],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env={**os.environ, "PYTHONPATH": str(SRC)},
    )
    assert result.returncode == 0, result.stderr or result.stdout
