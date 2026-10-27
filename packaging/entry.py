"""PyInstaller entry point: the executable's name picks what runs (see archicad_mcp.launcher)."""

import sys

from archicad_mcp.launcher import main

if __name__ == "__main__":
    sys.exit(main())
