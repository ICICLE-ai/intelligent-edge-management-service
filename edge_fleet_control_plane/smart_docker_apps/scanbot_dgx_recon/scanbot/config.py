"""Load the shared TOML config (default: ./config.toml or $SCANBOT_CONFIG)."""
import os
from pathlib import Path


def load_config(path=None):
    import tomllib  # here, so the client runs with --url/--token on Python < 3.11 too
    path = Path(path or os.environ.get("SCANBOT_CONFIG", "config.toml")).expanduser()
    if not path.exists():
        raise SystemExit(f"Config not found: {path} (copy config.example.toml to config.toml)")
    with open(path, "rb") as f:
        return tomllib.load(f)
