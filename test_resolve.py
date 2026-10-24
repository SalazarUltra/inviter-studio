"""Compatibility launcher; importing it does not contact Telegram."""

import sys

from tools.telegram_diagnostics import main

if __name__ == "__main__":
    main(["resolve", *sys.argv[1:]])
