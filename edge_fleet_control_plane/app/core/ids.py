"""Short, sortable, prefixed identifiers.

We avoid leaking sequential primary keys to the UI/MQTT layer. A prefix
makes it easy to spot the entity type at a glance in logs and audit trails.
"""

from __future__ import annotations

import secrets


def gen_uid(prefix: str, nbytes: int = 6) -> str:
    return f"{prefix}_{secrets.token_hex(nbytes)}"


def gen_request_id() -> str:
    return f"req_{secrets.token_hex(6)}"
