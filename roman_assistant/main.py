"""Launch Roman, the personal AI assistant."""

import ctypes
import os
import tkinter as tk

from config.settings import APP_NAME, WINDOW_SIZE
from core.assistant import Assistant
from core.instance_guard import SingleInstanceGuard
from ui.main_window import RomanWindow


def main() -> None:
    instance_guard = SingleInstanceGuard()
    if not instance_guard.acquire():
        print("Roman is already running.")
        return
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    try:
        root = tk.Tk()
        root.title(APP_NAME)
        root.geometry(WINDOW_SIZE)
        assistant = Assistant()
        RomanWindow(root, assistant)
        root.mainloop()
    finally:
        instance_guard.release()


if __name__ == "__main__":
    main()
