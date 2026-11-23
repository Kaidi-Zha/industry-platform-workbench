"""MCP launcher for clients that do not set a working directory."""

import sys

from industplatform.__main__ import main

if __name__ == "__main__":
    sys.argv.insert(1, "mcp")
    raise SystemExit(main())
