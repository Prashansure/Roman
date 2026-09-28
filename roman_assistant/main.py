"""Launch ROMAN Ⅰ, the personal AI assistant."""

import tkinter as tk

from config.settings import APP_NAME, WINDOW_SIZE
from core.assistant import Assistant
from ui.main_window import RomanWindow


def main() -> None:
    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry(WINDOW_SIZE)
    assistant = Assistant()
    RomanWindow(root, assistant)
    root.mainloop()


if __name__ == "__main__":
    main()
