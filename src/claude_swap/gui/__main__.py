"""Allow running as `python -m claude_swap.gui`."""

import sys

from claude_swap.gui import main

if __name__ == "__main__":
    sys.exit(main())
