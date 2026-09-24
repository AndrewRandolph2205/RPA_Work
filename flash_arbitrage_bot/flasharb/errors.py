"""Short, key-free descriptions of RPC errors for logs."""

from __future__ import annotations

import re
from typing import Optional

# RPC URLs usually end in the API key: https://host/v2/<key>. Keep the first 4
# characters of the last path segment so different keys are still tellable apart.
_URL_KEY = re.compile(r"((?:https?|wss?)://\S*/)([^\s/]{4})[^\s/]+")


def mask_secrets(text: str) -> str:
    return _URL_KEY.sub(r"\1\2...", text)


def http_status(exc: BaseException) -> Optional[int]:
    return getattr(getattr(exc, "response", None), "status_code", None)


def describe(exc: BaseException) -> str:
    status = http_status(exc)
    prefix = f"HTTP {status}: " if status else f"{type(exc).__name__}: "
    return prefix + mask_secrets(str(exc))[:200]
