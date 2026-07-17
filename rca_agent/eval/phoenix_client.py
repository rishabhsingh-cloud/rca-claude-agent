"""The Phoenix REST client, pointed at our self-hosted server.

Phoenix 18.0.0 exposes a lightweight REST client (`phoenix.client.Client`) with
`.datasets` / `.experiments` namespaces — that's all the eval needs, so we depend
on `arize-phoenix-client` (client only), NOT the full server package.

The import is lazy (inside `client()`) so this package stays importable — and the
non-Phoenix code paths keep working — on a machine where the client isn't installed.
"""

from __future__ import annotations

import os


def base_url() -> str:
    """Root of the Phoenix server. On the box that's the local server; override with
    PHOENIX_BASE_URL if the eval runs somewhere else (e.g. through the SSH tunnel)."""
    return os.getenv("PHOENIX_BASE_URL", "http://localhost:6006").rstrip("/")


def client():
    """A `phoenix.client.Client` for our server. Raises a clear error if the client
    package isn't installed (it ships in the `[eval]` extra)."""
    try:
        from phoenix.client import Client
    except ImportError as e:  # pragma: no cover
        raise SystemExit(
            "eval: phoenix client not installed. Install the eval extra:\n"
            "  pip install -e \".[eval]\"   (or: pip install arize-phoenix-client)"
        ) from e
    return Client(base_url=base_url())
