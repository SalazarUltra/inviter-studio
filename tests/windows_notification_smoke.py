"""One harmless local Windows notification; no Telegram, profile or session access."""

import json
import tkinter as tk
from pathlib import Path

from ui.notifications import WindowsNotifications


def main():
    root = tk.Tk()
    root.withdraw()
    root.update_idletasks()
    notifier = WindowsNotifications(root)
    accepted = notifier.show(
        "Inviter Studio · проверка", "Проверка локального уведомления. Сообщения в Telegram не отправлялись."
    )
    try:
        assert accepted, "Windows Shell did not accept the notification"
        root.after(5000, root.quit)
        root.mainloop()
    finally:
        notifier.close()
        root.destroy()
    output = Path(".audit/usability-20261022/windows-notification.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "shell_accepted": accepted,
                "icon_removed": notifier._data is None,
                "visibility_depends_on_windows_settings": True,
            }
        ),
        encoding="utf-8",
    )
    print("Windows accepted a native notification; the temporary icon was removed.")


if __name__ == "__main__":
    main()
