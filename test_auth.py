"""Compatibility launcher; importing it does not contact Telegram."""

from tools.telegram_diagnostics import main

if __name__ == "__main__":
    main(["auth"])
