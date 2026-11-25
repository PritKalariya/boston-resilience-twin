"""python -m serving: serve on $PORT (default 8080). A bad environment exits 2 with one line."""

from __future__ import annotations

import os
import sys

from serving.config import ConfigError, from_env


def main() -> int:
    try:
        settings = from_env()
    except ConfigError as exc:
        print(f"serving: {exc}", file=sys.stderr)
        return 2
    import uvicorn

    from serving.app import create_app

    uvicorn.run(create_app(settings), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
