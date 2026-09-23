"""Load a local ``.env`` file without adding a dependency.

A dozen lines of parsing are not worth a package. The rules are the usual
ones: ``KEY=VALUE`` per line, ``#`` comments and blank lines ignored, an
optional ``export`` prefix, surrounding quotes stripped. Two deliberate
choices: a variable already present in the environment is never overwritten
(a real shell export outranks a file on disk), and an empty value is skipped,
so an unfilled line copied from ``.env.example`` defines nothing.
"""

from __future__ import annotations

import os
from pathlib import Path


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse ``.env`` text into a mapping. Pure; touches nothing."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key and value:
            values[key] = value
    return values


def load_dotenv(path: str | Path = ".env") -> dict[str, str]:
    """Apply ``path`` to the environment. Returns only the keys actually set."""
    path = Path(path)
    if not path.is_file():
        return {}
    applied: dict[str, str] = {}
    for key, value in parse_dotenv(path.read_text(encoding="utf-8")).items():
        if key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied
