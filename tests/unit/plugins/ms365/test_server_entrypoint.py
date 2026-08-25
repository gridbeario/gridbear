"""The MCP server must survive being launched the way the provider launches it.

provider.get_server_config spawns it as a bare script (``python .../server.py``),
so the repo root is not on sys.path. Importing it as a package — which every
other test does — cannot catch a failure in that mode.
"""

import subprocess
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parents[4] / "plugins" / "ms365" / "server.py"


def _run_as_script(timeout=20):
    """Spawn server.py exactly as the provider does; return its stderr."""
    proc = subprocess.Popen(
        [sys.executable, str(SERVER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(SERVER.parent),
    )
    try:
        _, err = proc.communicate(input="", timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, err = proc.communicate()
    return err


def test_server_script_resolves_its_sibling_imports():
    err = _run_as_script()
    assert "ModuleNotFoundError" not in err, err
    assert "No module named 'plugins'" not in err, err
