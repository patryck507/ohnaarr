"""Container healthcheck: exit 0 when the status endpoint reports healthy."""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    port = os.environ.get("HTTP_PORT", "8787").strip() or "8787"
    url = f"http://127.0.0.1:{port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=4) as resp:  # noqa: S310 - fixed localhost URL
            return 0 if resp.status == 200 else 1
    except (urllib.error.URLError, OSError, ValueError):
        return 1


if __name__ == "__main__":
    sys.exit(main())
