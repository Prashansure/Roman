"""Premium, local-first chat interface for Roman."""

from __future__ import annotations

import io
import json
import math
import re
import tokenize
import tkinter as tk
import tkinter.font as tkfont
import textwrap
from datetime import date, datetime, timedelta
from pathlib import Path
from tkinter import ttk

from audio.microphone import list_input_devices
from config import settings as app_settings
from controls.keyboard import bind_controls
from core.state import AssistantState
from core.json_io import write_json_atomic

BG = app_settings.BG
SURFACE = app_settings.PANEL
SURFACE_HI = app_settings.PANEL_ALT
SURFACE_BORDER = app_settings.LINE
TEXT = app_settings.TEXT
MUTED = app_settings.MUTED
BLUE = app_settings.BLUE
GREEN = app_settings.GREEN
AMBER = app_settings.GOLD
RED = app_settings.RED
SEPARATOR = app_settings.LINE
SIDEBAR = app_settings.SIDEBAR
FONT = app_settings.FONT
CODE_FONT = app_settings.CODE_FONT
MAX_LANE = 820
HISTORY_PAGE_SIZE = 50
STREAM_RENDER_MS = 180


def _draw_rounded_rect(canvas, x1, y1, x2, y2, radius, fill, outline="", width=0, tags=()):
    """Draw a clean rounded rectangle using overlapping canvas primitives."""
    radius = max(1, min(int(radius), max(1, (x2 - x1) // 2), max(1, (y2 - y1) // 2)))

    def shape(left, top, right, bottom, color, tag, corner_radius):
        r = min(corner_radius, max(1, (right - left) // 2), max(1, (bottom - top) // 2))
        canvas.create_rectangle(left + r, top, right - r, bottom, fill=color, outline="", tags=tag)
        canvas.create_rectangle(left, top + r, right, bottom - r, fill=color, outline="", tags=tag)
        for cx, cy in ((left, top), (right - 2 * r, top), (left, bottom - 2 * r), (right - 2 * r, bottom - 2 * r)):
            canvas.create_oval(cx, cy, cx + 2 * r, cy + 2 * r, fill=color, outline="", tags=tag)

    if outline and width > 0:
        shape(x1, y1, x2, y2, outline, tags, radius)
        inset = width
        if x2 - x1 > inset * 2 and y2 - y1 > inset * 2:
            shape(x1 + inset, y1 + inset, x2 - inset, y2 - inset, fill, tags, max(1, radius - inset))
    else:
        shape(x1, y1, x2, y2, fill, tags, radius)


class RoundedBubble(tk.Canvas):
    """Canvas-backed rounded surface for compact user message bubbles."""

    def __init__(self, parent, fill: str) -> None:
        super().__init__(parent, bg=BG, highlightthickness=0, borderwidth=0, width=240, height=58)
        self.fill = fill
        self.panel = tk.Frame(self, bg=fill, padx=12, pady=6)
        self.window = self.create_window((7, 7), window=self.panel, anchor="nw")
        self.panel.bind("<Configure>", self._sync_size)
        self.bind("<Configure>", self._draw)

    def _sync_size(self, _event=None) -> None:
        width = max(88, self.panel.winfo_reqwidth() + 16)
        height = max(48, self.panel.winfo_reqheight() + 16)
        self.configure(width=width, height=height)
        self.coords(self.window, 8, 8)
        self.itemconfigure(self.window, width=width - 16, height=height - 16)
        self._draw()

    def _draw(self, _event=None) -> None:
        width, height = max(1, self.winfo_width()), max(1, self.winfo_height())
        radius = min(16, width // 2, height // 2)
        self.delete("bubble")
        _draw_rounded_rect(self, 0, 0, width, height, radius, self.fill,
                           outline=SURFACE_BORDER, width=1, tags="bubble")


class RomanWindow:
    def __init__(self, root: tk.Tk, assistant) -> None:
        self.root = root
        self.assistant = assistant
        self.logo_image = None
        self.small_logo_image = None
        self.state = AssistantState.ONLINE
        self.chat_items: list[dict] = []
        self.message_widgets: list[dict] = []
        self.sidebar_buttons: list[tk.Button] = []
        self.text_size_buttons: list[tk.Label] = []
        self._placeholder_active = True
        self._history_open = False
        self._status_phase = 0
        self._typing_phase = 0
        self._typing_active = False
        self._response_streaming = False
        self._active_response = None
        self._resize_job = None
        self._stream_render_job = None
        self._error_reset_job = None
        self._toast_job = None
        self._height_job = None
        self._pending_height_widgets = set()
        self._last_render_width = None
        self._history_messages = []
        self._history_start = 0
        self.current_conversation_id = self.assistant.memory.session_id
        self._composer_height = 128
        self.preferences_path = Path(app_settings.BASE_DIR) / "config" / "roman_ui_preferences.json"
        self.preferences = self._load_preferences()
        default_sidebar = int(app_settings.WINDOW_SIZE.split("x", 1)[0]) >= 1000
        self._sidebar_visible = bool(self.preferences.get("sidebar_visible", default_sidebar))
        if isinstance(self.preferences.get("microphone_index"), int):
            app_settings.MIC_DEVICE_INDEX = self.preferences["microphone_index"]

        self.root.title("Roman")
        self.root.configure(bg=BG)
        self.root.minsize(*app_settings.MIN_WINDOW_SIZE)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._load_logo()
        self._build()
        self.root.after_idle(self.entry.focus_set)

        saved = self.assistant.memory.list_conversations()
        if saved:
            self.current_conversation_id = saved[0]["id"]
            messages = self.assistant.load_conversation(saved[0]["id"])
            self._show_messages(messages)
        else:
            self._show_welcome()
        self._set_state(AssistantState.ONLINE, "Ready")
        self.root.after(30, self._drain_events)
        self.root.after(550, self._animate_status)

    def _load_preferences(self) -> dict:
        try:
            with self.preferences_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _text_size(self) -> int:
        size = self.preferences.get("text_size", 11)
        return size if size in (10, 11, 13) else 11

    def _save_preferences(self) -> None:
        try:
            write_json_atomic(self.preferences_path, self.preferences)
        except OSError as exc:
            self._append_system(f"Could not save settings: {exc}", error=True)

    def _load_logo(self) -> None:
        path = Path(app_settings.ASSETS_DIR) / "roman_logo.png"
        try:
            from PIL import Image, ImageTk

            with Image.open(path) as opened:
                source = opened.convert("RGBA")
                pixels = source.load()
                for y in range(source.height):
                    for x in range(source.width):
                        red, green, blue, alpha = pixels[x, y]
                        edge_level = max(red, green, blue)
                        if edge_level <= 28:
                            pixels[x, y] = (red, green, blue, 0)
                        elif edge_level < 58:
                            pixels[x, y] = (red, green, blue, round(alpha * (edge_level - 28) / 30))
                self.logo_image = ImageTk.PhotoImage(self._resize_logo(source, 32))
                self.small_logo_image = ImageTk.PhotoImage(self._resize_logo(source, 20))
        except (ImportError, tk.TclError, OSError, ValueError):
            self.logo_image = self.small_logo_image = None

    @staticmethod
    def _resize_logo(source, bound: int):
        from PIL import Image

        resized = source.copy()
        resized.thumbnail((bound, bound), Image.Resampling.LANCZOS)
        return resized

    def _build(self) -> None:
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(0, weight=0)
        self.root.grid_columnconfigure(1, weight=1)
        self._build_sidebar()
        self._build_main()
        if self._sidebar_visible:
            self.sidebar.grid(row=0, column=0, sticky="nsw")

    def _build_sidebar(self) -> None:
        self.sidebar = tk.Frame(self.root, bg=SIDEBAR, width=254)
        self.sidebar.grid_propagate(False)
        self.sidebar.grid_columnconfigure(0, weight=1)
        self.sidebar.grid_rowconfigure(3, weight=1)

        top = tk.Frame(self.sidebar, bg=SIDEBAR)
        top.grid(row=0, column=0, sticky="ew", padx=12, pady=(14, 14))
        top.grid_columnconfigure(0, weight=1)
        tk.Label(top, text="Conversations", bg=SIDEBAR, fg=MUTED,
                 font=(FONT, 9, "bold")).grid(row=0, column=0, sticky="w")
        self._soft_button(top, "×", self._toggle_sidebar, width=3, background=SIDEBAR).grid(row=0, column=1, sticky="e")

        self._sidebar_action("＋   New conversation", self._new_conversation, row=1, accent=True)
        self._sidebar_action("◷   Conversation history", self._toggle_history, row=2)

        self.history_panel = tk.Frame(self.sidebar, bg=SIDEBAR)
        self.history_panel.grid(row=3, column=0, sticky="nsew", padx=12)
        self.history_panel.grid_rowconfigure(0, weight=1)
        self.history_panel.grid_columnconfigure(0, weight=1)
        self.history_canvas = tk.Canvas(self.history_panel, bg=SIDEBAR, highlightthickness=0, borderwidth=0)
        self.history_canvas.grid(row=0, column=0, sticky="nsew")
        history_scroll = tk.Scrollbar(
            self.history_panel, command=self.history_canvas.yview, bg=SEPARATOR,
            troughcolor=SIDEBAR, activebackground=MUTED, relief="flat", borderwidth=0, width=7,
        )
        history_scroll.grid(row=0, column=1, sticky="ns")
        self.history_canvas.configure(yscrollcommand=history_scroll.set)
        self.history_list = tk.Frame(self.history_canvas, bg=SIDEBAR)
        self.history_window = self.history_canvas.create_window((0, 0), window=self.history_list, anchor="nw")
        self.history_list.bind("<Configure>", lambda _event: self.history_canvas.configure(scrollregion=self.history_canvas.bbox("all")))
        self.history_canvas.bind("<Configure>", lambda event: self.history_canvas.itemconfigure(self.history_window, width=event.width))
        self.history_panel.grid_remove()

        tk.Frame(self.sidebar, bg=SEPARATOR, height=1).grid(row=4, column=0, sticky="ew", padx=16, pady=(10, 8))
        lower = tk.Frame(self.sidebar, bg=SIDEBAR)
        lower.grid(row=6, column=0, sticky="ew", padx=0, pady=(0, 14))
        self._sidebar_action("Settings", self._open_settings, row=0, parent=lower)
        self._sidebar_action("Voice settings", self._open_voice_settings, row=1, parent=lower)
        self._sidebar_action("Model information", self._open_model_info, row=2, parent=lower)

    def _sidebar_action(self, label: str, command, row: int, parent=None, accent=False) -> tk.Button:
        parent = parent or self.sidebar
        background = SURFACE if accent else parent.cget("bg")
        foreground = BLUE if accent else TEXT
        item = tk.Button(
            parent, text=label, command=command, bg=background, fg=foreground,
            activebackground=SURFACE_HI, activeforeground=TEXT, anchor="w",
            padx=12, pady=9, font=(FONT, 10), cursor="hand2", relief="flat",
            borderwidth=0, highlightthickness=1, highlightbackground=parent.cget("bg"),
            highlightcolor=BLUE, takefocus=True,
        )
        item.grid(row=row, column=0, sticky="ew", padx=12, pady=3)
        item._roman_rest_bg = background
        self.sidebar_buttons.append(item)
        item.bind("<Enter>", lambda _event: item.configure(bg=SURFACE_HI))
        item.bind("<Leave>", lambda _event: item.configure(bg=item._roman_rest_bg))
        return item

    def _build_main(self) -> None:
        self.main = tk.Frame(self.root, bg=BG)
        self.main.grid(row=0, column=1, sticky="nsew")
        self.main.grid_rowconfigure(1, weight=1)
        self.main.grid_columnconfigure(0, weight=1)
        self._build_topbar()
        self._build_chat()
        self._build_composer()

    def _build_topbar(self) -> None:
        bar = tk.Frame(self.main, bg=BG, height=68)
        bar.grid(row=0, column=0, sticky="ew", padx=28, pady=(12, 0))
        bar.grid_columnconfigure(1, weight=1)
        self.sidebar_toggle = self._soft_button(bar, "☰", self._toggle_sidebar, width=3)
        self.sidebar_toggle.grid(row=0, column=0, sticky="w", padx=(0, 14))

        brand = tk.Frame(bar, bg=BG)
        brand.grid(row=0, column=1, sticky="w")
        if self.small_logo_image:
            tk.Label(brand, image=self.small_logo_image, bg=BG).pack(side="left", padx=(0, 8))
        tk.Label(brand, text="Roman", bg=BG, fg=TEXT, font=(FONT, 12, "bold")).pack(side="left")

        status_group = tk.Frame(bar, bg=BG)
        status_group.grid(row=0, column=2, sticky="e")
        self.status_dot = tk.Label(status_group, text="●", bg=BG, fg=GREEN, font=(FONT, 9))
        self.status_dot.pack(side="left", padx=(0, 6))
        self.status_text = tk.Label(status_group, text="Online", bg=BG, fg=MUTED, font=(FONT, 9))
        self.status_text.pack(side="left")

    def _build_chat(self) -> None:
        holder = tk.Frame(self.main, bg=BG)
        holder.grid(row=1, column=0, sticky="nsew", padx=12)
        holder.grid_rowconfigure(0, weight=1)
        holder.grid_columnconfigure(0, weight=1)
        self.chat_canvas = tk.Canvas(holder, bg=BG, highlightthickness=0, borderwidth=0)
        self.chat_canvas.grid(row=0, column=0, sticky="nsew")
        self.message_column = tk.Frame(self.chat_canvas, bg=BG)
        self.message_window = self.chat_canvas.create_window((0, 0), window=self.message_column, anchor="nw")
        self.message_column.bind("<Configure>", self._on_messages_changed)
        self.chat_canvas.bind("<Configure>", self._on_chat_resize)
        self.scrollbar = tk.Scrollbar(
            holder, command=self.chat_canvas.yview, bg=SEPARATOR, troughcolor=BG,
            activebackground=MUTED, relief="flat", borderwidth=0, width=8,
        )
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.chat_canvas.configure(yscrollcommand=self.scrollbar.set)
        self.chat_canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _build_composer(self) -> None:
        area = tk.Frame(self.main, bg=BG)
        area.grid(row=2, column=0, sticky="ew", padx=20, pady=(6, 20))
        area.grid_columnconfigure(0, weight=1)

        self.composer_canvas = tk.Canvas(area, bg=BG, height=self._composer_height, highlightthickness=0, borderwidth=0)
        self.composer_canvas.grid(row=0, column=0, sticky="ew")
        self.composer_canvas.bind("<Configure>", self._draw_composer)
        self.composer = tk.Frame(self.composer_canvas, bg=SURFACE)
        self.composer_window = self.composer_canvas.create_window((1, 1), window=self.composer, anchor="nw")
        self.composer.grid_columnconfigure(0, weight=1)

        self.entry = tk.Text(
            self.composer, height=2, wrap=tk.WORD, bg=SURFACE, fg=MUTED,
            insertbackground=TEXT, relief="flat", borderwidth=0, padx=12, pady=12,
            font=(FONT, self._text_size()), undo=True, highlightthickness=0,
        )
        self.entry.insert("1.0", "Message Roman…")
        self.entry.bind("<FocusIn>", self._clear_placeholder)
        self.entry.bind("<FocusOut>", self._restore_placeholder)
        self.entry.bind("<Return>", self._send_from_return)
        self.entry.bind("<Shift-Return>", self._insert_newline)
        self.entry.bind("<KeyRelease>", self._on_composer_change)
        self.entry.bind("<Configure>", lambda _event: self.root.after_idle(self._on_composer_change))
        self.entry.bind("<<Paste>>", self._on_composer_change)
        self.entry.bind("<<Cut>>", self._on_composer_change)
        self.entry.grid(row=0, column=0, columnspan=4, sticky="ew", padx=7, pady=(7, 0))
        self.composer_scrollbar = tk.Scrollbar(
            self.composer, command=self.entry.yview, bg=SEPARATOR, troughcolor=SURFACE,
            activebackground=MUTED, relief="flat", borderwidth=0, width=7,
        )
        self.entry.configure(yscrollcommand=self.composer_scrollbar.set)
        self.composer_scrollbar.grid(row=0, column=4, sticky="ns", padx=(0, 3), pady=(10, 2))
        self.composer_scrollbar.grid_remove()

        controls = tk.Frame(self.composer, bg=SURFACE)
        controls.grid(row=1, column=0, columnspan=5, sticky="ew", padx=12, pady=(0, 8))
        controls.grid_columnconfigure(0, weight=1)
        self.voice_button = self._soft_button(controls, "◉  Voice", self.assistant.toggle_listening)
        self.voice_button.grid(row=0, column=1, padx=(0, 8))
        self.stop_button = self._soft_button(controls, "■  Stop", self.assistant.cancel, foreground=RED)
        self.stop_button.grid(row=0, column=2)
        self.stop_button.grid_remove()
        self.send_button = self._soft_button(controls, "↑", self._send, width=3, background=BLUE)
        self.send_button.grid(row=0, column=3, padx=(8, 0))
        self.send_button.configure(state="disabled", fg=BG, activebackground=BLUE)

        footer = tk.Frame(area, bg=BG)
        footer.grid(row=1, column=0, sticky="ew", padx=5, pady=(7, 0))
        footer.grid_columnconfigure(0, weight=1)
        self.notice_label = tk.Label(footer, text="", bg=BG, fg=MUTED, font=(FONT, 9), anchor="w")
        self.notice_label.grid(row=0, column=0, sticky="w")
        tk.Label(
            footer, text="ENTER TO SEND  ·  SHIFT + ENTER FOR A NEW LINE  ·  C TO TALK / CTRL + SHIFT + M",
            bg=BG, fg=MUTED, font=(FONT, 8),
        ).grid(row=0, column=1, sticky="e")
        bind_controls(
            self.root,
            {"c": self.assistant.toggle_listening, "a": self.assistant.resume,
             "b": self.assistant.pause, "e": self.assistant.cancel,
             "escape": self.assistant.cancel, "new": self._new_conversation},
            self.entry,
        )

    def _draw_composer(self, event=None) -> None:
        width = max(1, self.composer_canvas.winfo_width())
        height = self._composer_height
        self.composer_canvas.configure(height=height)
        self.composer_canvas.delete("surface")
        _draw_rounded_rect(self.composer_canvas, 0, 0, width, height, 18, SURFACE,
                           outline=SURFACE_BORDER, width=1, tags="surface")
        self.composer_canvas.coords(self.composer_window, 16, 10)
        self.composer_canvas.itemconfigure(self.composer_window, width=max(1, width - 32), height=height - 20)

    def _soft_button(self, parent, label: str, command, width=None, foreground=TEXT, background=SURFACE) -> tk.Button:
        widget = tk.Button(
            parent, text=label, command=command, bg=background, fg=foreground,
            activebackground=SURFACE_HI, activeforeground=TEXT, disabledforeground=MUTED,
            padx=12, pady=8, font=(FONT, 9), cursor="hand2", width=width,
            relief="flat", borderwidth=0, highlightthickness=1,
            highlightbackground=parent.cget("bg"), highlightcolor=BLUE, takefocus=True,
        )
        widget.bind("<Enter>", lambda _event: widget.configure(bg=SURFACE_HI))
        widget.bind("<Leave>", lambda _event: widget.configure(bg=background))
        return widget

    def _clear_placeholder(self, _event=None) -> None:
        if self._placeholder_active:
            self.entry.delete("1.0", tk.END)
            self.entry.configure(fg=TEXT)
            self._placeholder_active = False

    def _restore_placeholder(self, _event=None) -> None:
        if not self.entry.get("1.0", "end-1c").strip():
            self.entry.delete("1.0", tk.END)
            self.entry.insert("1.0", "Message Roman…")
            self.entry.configure(fg=MUTED)
            self._placeholder_active = True
        self._on_composer_change()

    def _on_composer_change(self, _event=None) -> None:
        if not hasattr(self, "send_button"):
            return
        text = "" if self._placeholder_active else self.entry.get("1.0", "end-1c")
        lines = max(1, len(text.splitlines()))
        try:
            display_lines = self.entry.count("1.0", "end-1c", "displaylines")
            lines = max(lines, display_lines[0] if display_lines else 1)
        except tk.TclError:
            pass
        visible_lines = min(8, max(2, lines))
        if int(self.entry.cget("height")) != visible_lines:
            self.entry.configure(height=visible_lines)
            line_height = tkfont.Font(font=self.entry.cget("font")).metrics("linespace")
            self._composer_height = 128 + max(0, visible_lines - 2) * line_height
            self._draw_composer()
        if lines > 8:
            self.composer_scrollbar.grid()
        else:
            self.composer_scrollbar.grid_remove()
        busy = self.state in {
            AssistantState.LISTENING, AssistantState.TRANSCRIBING,
            AssistantState.THINKING, AssistantState.SPEAKING, AssistantState.PAUSED,
        }
        enabled = bool(text.strip()) and not busy
        self.send_button.configure(state="normal" if enabled else "disabled")

    def _send_from_return(self, _event=None):
        self._send()
        return "break"

    def _insert_newline(self, _event=None):
        self.entry.insert(tk.INSERT, "\n")
        return "break"

    def _send(self) -> bool:
        if self._placeholder_active:
            return False
        message = self.entry.get("1.0", "end-1c").strip()
        if not message:
            return False
        if not self.assistant.send_message(message):
            self._show_toast("Finish the current response before sending another message.", error=False)
            self._on_composer_change()
            return False
        self.entry.delete("1.0", tk.END)
        self.entry.edit_reset()
        self._placeholder_active = False
        self.entry.configure(fg=TEXT, height=2)
        self._composer_height = 128
        self.composer_scrollbar.grid_remove()
        self._draw_composer()
        self._on_composer_change()
        self.entry.focus_set()
        return True

    def _show_welcome(self) -> None:
        self._clear_chat_widgets()
        self.chat_items = []
        self._history_messages = []
        self._history_start = 0
        welcome = tk.Frame(self.message_column, bg=BG)
        welcome.pack(fill="x", pady=(125, 30))
        if self.logo_image:
            tk.Label(welcome, image=self.logo_image, bg=BG).pack(pady=(0, 16))
        tk.Label(welcome, text="What can I help with?", bg=BG, fg=TEXT, font=(FONT, 24, "bold")).pack()
        tk.Label(
            welcome, text="A private local assistant, ready when you are.",
            bg=BG, fg=MUTED, font=(FONT, 11),
        ).pack(pady=(9, 0))
        self.chat_canvas.yview_moveto(0)

    def _show_messages(self, messages) -> None:
        self._clear_chat_widgets()
        self.chat_items = []
        self._history_messages = list(messages)
        self._history_start = max(0, len(self._history_messages) - HISTORY_PAGE_SIZE)
        if self._history_start:
            self._make_load_earlier_button().pack(fill="x", pady=(5, 12))
        for message in self._history_messages[self._history_start:]:
            item = {"role": message["role"], "text": message["content"], "streaming": False}
            self.chat_items.append(item)
            self._render_item(item)
        if not self._history_messages:
            self._show_welcome()
        self._scroll_bottom(force=True)

    def _make_load_earlier_button(self) -> tk.Button:
        return tk.Button(
            self.message_column, text="Load earlier messages", command=self._load_earlier_messages,
            bg=BG, fg=MUTED, activebackground=BG, activeforeground=TEXT,
            relief="flat", borderwidth=0, font=(FONT, 9), cursor="hand2",
            highlightthickness=1, highlightbackground=BG, highlightcolor=BLUE, takefocus=True,
        )

    def _load_earlier_messages(self) -> None:
        if self._history_start <= 0:
            return
        old_top = self.chat_canvas.canvasy(0)
        first_row = next((
            child for child in self.message_column.winfo_children()
            if isinstance(child, tk.Frame) and child.winfo_exists()
        ), None)
        buttons = [child for child in self.message_column.winfo_children() if isinstance(child, tk.Button)]
        for button in buttons:
            button.destroy()
        new_start = max(0, self._history_start - HISTORY_PAGE_SIZE)
        older = []
        for message in self._history_messages[new_start:self._history_start]:
            item = {"role": message["role"], "text": message["content"], "streaming": False}
            self.chat_items.insert(0, item)
            self._render_item(item, before=first_row)
            older.append(item)
        self._history_start = new_start
        if self._history_start:
            self._make_load_earlier_button().pack(before=older[0]["row_widget"] if older else first_row, fill="x", pady=(5, 12))
        self.root.update_idletasks()
        new_top = old_top + sum(item["row_widget"].winfo_height() for item in older)
        region = self.chat_canvas.bbox("all")
        total_height = max(1, (region[3] - region[1]) if region else 1)
        self.chat_canvas.yview_moveto(max(0.0, min(1.0, new_top / total_height)))

    def _clear_chat_widgets(self) -> None:
        for child in self.message_column.winfo_children():
            child.destroy()
        self.message_widgets.clear()

    def _new_conversation(self) -> None:
        self.current_conversation_id = self.assistant.new_conversation()
        self.chat_items = []
        self._history_messages = []
        self._history_start = 0
        self._response_streaming = False
        self._active_response = None
        self._stop_typing_indicator()
        self._show_welcome()
        self._refresh_history()
        self._save_sidebar_preference()

    def _toggle_sidebar(self) -> None:
        if self._sidebar_visible:
            self.sidebar.grid_remove()
            self._sidebar_visible = False
        else:
            self.sidebar.grid(row=0, column=0, sticky="nsw")
            self.main.grid_configure(row=0, column=1, sticky="nsew")
            self._sidebar_visible = True
        self._save_sidebar_preference()

    def _save_sidebar_preference(self) -> None:
        self.preferences["sidebar_visible"] = self._sidebar_visible
        self._save_preferences()

    def _toggle_history(self) -> None:
        self._history_open = not self._history_open
        if self._history_open:
            self.history_panel.grid()
            self._refresh_history()
        else:
            self.history_panel.grid_remove()

    def _refresh_history(self) -> None:
        for child in self.history_list.winfo_children():
            child.destroy()
        sessions = self.assistant.memory.list_conversations()
        if not sessions:
            tk.Label(self.history_list, text="No saved conversations yet", bg=SIDEBAR, fg=MUTED,
                     font=(FONT, max(9, self._text_size() - 2)), wraplength=210, justify="left").pack(anchor="w", padx=8, pady=8)
            return
        titles = self.preferences.setdefault("conversation_titles", {})
        groups: dict[str, list[dict]] = {}
        today = date.today()
        for session in sessions:
            messages = session["messages"]
            stamp_value = messages[-1].get("timestamp", session.get("started_at", "")) if messages else ""
            try:
                stamp_dt = datetime.fromisoformat(stamp_value)
                stamp = stamp_dt.astimezone().strftime("%H:%M")
                chat_day = stamp_dt.astimezone().date()
            except (ValueError, TypeError):
                stamp = str(stamp_value)[:10]
                try:
                    chat_day = date.fromisoformat(stamp)
                except ValueError:
                    chat_day = today
            if chat_day == today:
                group = "Today"
            elif chat_day == today - timedelta(days=1):
                group = "Yesterday"
            else:
                group = chat_day.strftime("%b %d, %Y")
            groups.setdefault(group, []).append({**session, "stamp": stamp, "day": chat_day})

        group_order = sorted(groups, key=lambda key: max(row["day"] for row in groups[key]), reverse=True)
        for group in group_order:
            tk.Label(self.history_list, text=group.upper(), bg=SIDEBAR, fg=MUTED,
                     anchor="w", font=(FONT, max(8, self._text_size() - 3), "bold")).pack(fill="x", padx=12, pady=(12, 4))
            for session in groups[group]:
                messages = session["messages"]
                first_user = next((row["content"] for row in messages if row["role"] == "user"), "Conversation")
                title = titles.get(session["id"]) or first_user.replace("\n", " ").strip() or "Conversation"
                title = textwrap.shorten(title, width=38, placeholder="…")
                active = session["id"] == self.current_conversation_id
                rest_bg = SURFACE_HI if active else SIDEBAR
                entry = tk.Button(
                    self.history_list, text=f"{title}\n{session['stamp']}",
                    command=lambda sid=session["id"]: self._open_conversation(sid),
                    bg=rest_bg, fg=TEXT if active else MUTED, activebackground=SURFACE_HI,
                    activeforeground=TEXT, anchor="w", justify="left", font=(FONT, max(9, self._text_size() - 2)),
                    padx=10, pady=8, wraplength=210, cursor="hand2", relief="flat",
                    borderwidth=0, highlightthickness=1,
                    highlightbackground=rest_bg, highlightcolor=BLUE, takefocus=True,
                )
                entry.pack(fill="x", padx=8, pady=2)
                entry.bind("<Enter>", lambda _event, label=entry: label.configure(bg=SURFACE_HI, fg=TEXT))
                entry.bind("<Leave>", lambda _event, label=entry, color=rest_bg, active=active: label.configure(bg=color, fg=TEXT if active else MUTED))
                entry.bind("<Button-3>", lambda event, sid=session["id"]: self._show_history_menu(event, sid))
                entry.bind("<Shift-F10>", lambda event, sid=session["id"]: self._show_history_menu(event, sid))

    def _show_history_menu(self, event, session_id: str):
        menu = tk.Menu(self.root, tearoff=False, bg=SURFACE, fg=TEXT,
                       activebackground=SURFACE_HI, activeforeground=TEXT)
        menu.add_command(label="Rename conversation…", command=lambda: self._rename_conversation(session_id))
        menu.add_command(label="Delete conversation…", command=lambda: self._confirm_delete_conversation(session_id))
        menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def _rename_conversation(self, session_id: str) -> None:
        session = next((row for row in self.assistant.memory.list_conversations() if row["id"] == session_id), None)
        if not session:
            return
        first_user = next((row["content"] for row in session["messages"] if row["role"] == "user"), "Conversation")
        titles = self.preferences.setdefault("conversation_titles", {})
        dialog = self._dialog("Rename conversation", 420, 170)
        tk.Label(dialog, text="Conversation name", bg=BG, fg=TEXT, font=(FONT, 12, "bold")).pack(anchor="w", padx=22, pady=(20, 8))
        value = tk.StringVar(value=titles.get(session_id, first_user.replace("\n", " ").strip()[:80] or "Conversation"))
        field = tk.Entry(dialog, textvariable=value, bg=SURFACE, fg=TEXT, insertbackground=TEXT,
                         relief="flat", font=(FONT, 10), highlightthickness=1,
                         highlightbackground=SURFACE_BORDER, highlightcolor=BLUE)
        field.pack(fill="x", padx=22, pady=(0, 14))
        actions = tk.Frame(dialog, bg=BG)
        actions.pack(fill="x", padx=22)
        tk.Button(actions, text="Cancel", command=dialog.destroy, bg=BG, fg=MUTED,
                  activebackground=SURFACE_HI, relief="flat", takefocus=True).pack(side="right", padx=(8, 0))

        def save_title():
            title = value.get().strip()
            if not title:
                return
            titles[session_id] = title[:120]
            self._save_preferences()
            self._refresh_history()
            dialog.destroy()

        tk.Button(actions, text="Save", command=save_title, bg=SURFACE_HI, fg=TEXT,
                  activebackground=SURFACE_BORDER, relief="flat", takefocus=True).pack(side="right")
        field.focus_set()
        field.selection_range(0, tk.END)
        field.bind("<Return>", lambda _event: save_title())

    def _confirm_delete_conversation(self, session_id: str) -> None:
        if self.state in {
            AssistantState.LISTENING, AssistantState.TRANSCRIBING,
            AssistantState.THINKING, AssistantState.SPEAKING, AssistantState.PAUSED,
        }:
            self._show_toast("Stop the current task before deleting a conversation.")
            return
        dialog = self._dialog("Delete conversation", 420, 175)
        tk.Label(dialog, text="Delete this conversation?", bg=BG, fg=TEXT,
                 font=(FONT, 12, "bold")).pack(anchor="w", padx=22, pady=(22, 8))
        tk.Label(dialog, text="This permanently removes its saved messages from Roman’s local history.",
                 bg=BG, fg=MUTED, wraplength=370, justify="left", font=(FONT, 9)).pack(anchor="w", padx=22)
        actions = tk.Frame(dialog, bg=BG)
        actions.pack(fill="x", padx=22, pady=(18, 0))
        tk.Button(actions, text="Cancel", command=dialog.destroy, bg=BG, fg=MUTED,
                  activebackground=SURFACE_HI, relief="flat", takefocus=True).pack(side="right", padx=(8, 0))

        def delete():
            removed = self.assistant.memory.delete_conversation(session_id)
            titles = self.preferences.setdefault("conversation_titles", {})
            titles.pop(session_id, None)
            self._save_preferences()
            dialog.destroy()
            if removed and session_id == self.current_conversation_id:
                self.current_conversation_id = self.assistant.new_conversation()
                self.chat_items = []
                self._history_messages = []
                self._history_start = 0
                self._show_welcome()
            self._refresh_history()
            self._show_toast("Conversation deleted." if removed else "Conversation could not be deleted.", error=not removed)

        tk.Button(actions, text="Delete", command=delete, bg=RED, fg=BG,
                  activebackground=RED, relief="flat", takefocus=True).pack(side="right")
        dialog.bind("<Return>", lambda _event: delete())

    def _open_conversation(self, session_id: str) -> None:
        messages = self.assistant.load_conversation(session_id)
        self.current_conversation_id = session_id
        self.chat_items = []
        self._response_streaming = False
        self._active_response = None
        self._stop_typing_indicator()
        self._show_messages(messages)
        self._refresh_history()

    def _open_settings(self) -> None:
        window = self._dialog("Settings", 420, 330)
        tk.Label(window, text="Appearance", bg=BG, fg=TEXT, font=(FONT, 14, "bold")).pack(anchor="w", padx=22, pady=(22, 12))
        tk.Label(window, text="Response text size", bg=BG, fg=MUTED, font=(FONT, 10)).pack(anchor="w", padx=22)
        controls = tk.Frame(window, bg=BG)
        controls.pack(anchor="w", padx=18, pady=(8, 20))
        current = self._text_size()
        self.text_size_buttons = []
        for label, size in (("Small", 10), ("Standard", 11), ("Large", 13)):
            item = tk.Button(
                controls, text=label, command=lambda value=size: self._set_text_size(value, window),
                bg=SURFACE_HI if current == size else BG, fg=TEXT,
                activebackground=SURFACE_HI, activeforeground=TEXT, padx=12, pady=8,
                font=(FONT, 9), cursor="hand2", relief="flat", takefocus=True,
                highlightthickness=1, highlightbackground=BG, highlightcolor=BLUE,
            )
            item.pack(side="left", padx=4)
            self.text_size_buttons.append(item)
        reduce_var = tk.BooleanVar(value=bool(self.preferences.get("reduce_motion", False)))
        toggle = tk.Checkbutton(
            window, text="Reduce motion", variable=reduce_var, command=lambda: self._set_reduce_motion(reduce_var.get()),
            bg=BG, fg=TEXT, activebackground=BG, activeforeground=TEXT, selectcolor=SURFACE,
            font=(FONT, 10), relief="flat", highlightthickness=1,
            highlightbackground=BG, highlightcolor=BLUE, takefocus=True,
        )
        toggle.pack(anchor="w", padx=22)
        tk.Label(window, text="Changes apply to the chat immediately.", bg=BG, fg=MUTED, font=(FONT, 9)).pack(anchor="w", padx=22, pady=(16, 0))

    def _set_text_size(self, size: int, dialog) -> None:
        self.preferences["text_size"] = size
        self._save_preferences()
        self._rerender_messages()
        self.entry.configure(font=(FONT, size))
        for button in self.sidebar_buttons:
            if button.winfo_exists():
                button.configure(font=(FONT, max(9, size - 1)))
        for value, button in zip((10, 11, 13), self.text_size_buttons):
            button.configure(bg=SURFACE_HI if value == size else BG)
        if self._history_open:
            self._refresh_history()

    def _set_reduce_motion(self, value: bool) -> None:
        self.preferences["reduce_motion"] = value
        self._save_preferences()

    def _open_voice_settings(self) -> None:
        window = self._dialog("Voice settings", 480, 300)
        tk.Label(window, text="Microphone input", bg=BG, fg=TEXT, font=(FONT, 14, "bold")).pack(anchor="w", padx=22, pady=(22, 8))
        try:
            devices = list_input_devices()
        except Exception as exc:
            devices = []
            tk.Label(window, text=f"Audio devices are unavailable: {exc}", bg=BG, fg=RED,
                     wraplength=430, justify="left", font=(FONT, 9)).pack(anchor="w", padx=22, pady=(0, 10))
        if not devices:
            tk.Label(window, text="No microphone inputs were detected.", bg=BG, fg=MUTED, font=(FONT, 10)).pack(anchor="w", padx=22, pady=(8, 0))
            return
        labels = [f"{name}  ·  device {index}" for index, name in devices]
        lookup = {label: index for (index, _name), label in zip(devices, labels)}
        selected = tk.StringVar()
        current_label = next((label for label in labels if lookup[label] == app_settings.MIC_DEVICE_INDEX), "")
        selected.set(current_label)
        style = ttk.Style(window)
        window.option_add("*TCombobox*Listbox.background", SURFACE)
        window.option_add("*TCombobox*Listbox.foreground", TEXT)
        window.option_add("*TCombobox*Listbox.selectBackground", SURFACE_HI)
        window.option_add("*TCombobox*Listbox.selectForeground", TEXT)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Roman.TCombobox", fieldbackground=SURFACE, background=SURFACE_HI,
            foreground=TEXT, arrowcolor=TEXT, bordercolor=SEPARATOR,
        )
        style.map("Roman.TCombobox", fieldbackground=[("readonly", SURFACE)], foreground=[("readonly", TEXT)])
        combo = ttk.Combobox(window, style="Roman.TCombobox", state="readonly", values=labels, textvariable=selected, width=48)
        combo.pack(anchor="w", padx=22, pady=(4, 8))
        note_text = "The selected input is used for the next voice recording." if current_label else "Choose an input to replace the unavailable configured device."
        note = tk.Label(window, text=note_text, bg=BG, fg=MUTED, font=(FONT, 9))
        note.pack(anchor="w", padx=22, pady=(8, 0))

        def apply_device(_event=None):
            index = lookup.get(selected.get())
            if index is not None:
                app_settings.MIC_DEVICE_INDEX = index
                self.preferences["microphone_index"] = index
                self._save_preferences()
                note.configure(text="Microphone updated.", fg=GREEN)

        combo.bind("<<ComboboxSelected>>", apply_device)

    def _open_model_info(self) -> None:
        window = self._dialog("Model information", 430, 310)
        tk.Label(window, text="Running locally", bg=BG, fg=TEXT, font=(FONT, 14, "bold")).pack(anchor="w", padx=22, pady=(22, 14))
        transcriber = self.assistant.transcriber
        device = transcriber.active_device or app_settings.WHISPER_DEVICE
        compute = transcriber.active_compute_type or app_settings.WHISPER_COMPUTE_TYPE
        runtime_note = "active" if transcriber.active_device else "configured · loads on first recording"
        speech_output = "Windows SAPI" if self.assistant.speech.available else "Unavailable · Windows only"
        details = (
            ("Chat model", app_settings.OLLAMA_MODEL),
            ("Chat runtime", "Ollama · local"),
            ("Speech recognition", f"Faster-Whisper · {app_settings.WHISPER_MODEL}"),
            ("Compute", f"{device.upper()} · {compute} ({runtime_note})"),
            ("Speech output", speech_output),
        )
        for name, value in details:
            row = tk.Frame(window, bg=BG)
            row.pack(fill="x", padx=22, pady=5)
            tk.Label(row, text=name, bg=BG, fg=MUTED, font=(FONT, 9), width=20, anchor="w").pack(side="left")
            tk.Label(row, text=value, bg=BG, fg=TEXT, font=(FONT, 9), anchor="w").pack(side="left")

    def _dialog(self, title: str, width: int, height: int) -> tk.Toplevel:
        window = tk.Toplevel(self.root)
        window.title(title)
        window.configure(bg=BG)
        window.geometry(f"{width}x{height}")
        window.resizable(False, False)
        window.transient(self.root)
        window.protocol("WM_DELETE_WINDOW", window.destroy)
        window.bind("<Escape>", lambda _event: window.destroy())
        window.update_idletasks()
        self.root.update_idletasks()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - width) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - height) // 2)
        window.geometry(f"{width}x{height}+{x}+{y}")
        close = tk.Button(window, text="Close", command=window.destroy, bg=BG, fg=MUTED,
                          activebackground=SURFACE_HI, relief="flat", takefocus=True,
                          highlightthickness=1, highlightbackground=BG, highlightcolor=BLUE)
        close.place(relx=1.0, rely=1.0, anchor="se", x=-18, y=-12)
        window.grab_set()
        return window

    def _rerender_messages(self) -> None:
        scroll_position = self.chat_canvas.yview()[0]
        code_views = self._capture_code_views(self.chat_items)
        self._clear_chat_widgets()
        if not self.chat_items and not self._history_messages:
            self._show_welcome()
            return
        if self._history_start:
            self._make_load_earlier_button().pack(fill="x", pady=(5, 12))
        for item in self.chat_items:
            self._render_item(item)
        self.root.after_idle(lambda: self.chat_canvas.yview_moveto(scroll_position))
        self.root.after_idle(lambda: self._restore_code_views(code_views))

    def _capture_code_views(self, items):
        views = []
        for item in items:
            widget = item.get("text_widget")
            if not isinstance(widget, tk.Text) or not widget.winfo_exists():
                continue
            try:
                for path in widget.window_names():
                    child = self.root.nametowidget(path)
                    for descendant in self._walk_widgets(child):
                        if isinstance(descendant, tk.Text):
                            views.append((descendant.xview()[0], descendant.yview()[0]))
            except (tk.TclError, KeyError):
                continue
        return views

    def _restore_code_views(self, views):
        index = 0
        for item in self.chat_items:
            widget = item.get("text_widget")
            if not isinstance(widget, tk.Text) or not widget.winfo_exists():
                continue
            try:
                for path in widget.window_names():
                    child = self.root.nametowidget(path)
                    for descendant in self._walk_widgets(child):
                        if isinstance(descendant, tk.Text) and index < len(views):
                            xview, yview = views[index]
                            descendant.xview_moveto(xview)
                            descendant.yview_moveto(yview)
                            index += 1
            except (tk.TclError, KeyError):
                continue

    @staticmethod
    def _walk_widgets(widget):
        yield widget
        for child in widget.winfo_children():
            yield from RomanWindow._walk_widgets(child)

    def _body_chars(self) -> int:
        width = max(360, self.chat_canvas.winfo_width())
        lane = min(MAX_LANE, width - 72)
        font = tkfont.Font(self.root, family=FONT, size=self._text_size())
        average = max(1, font.measure("0"))
        return max(28, min(140, lane // average))

    def _render_item(self, item: dict, before=None) -> None:
        role = item["role"]
        lane_width = min(MAX_LANE, max(320, self.chat_canvas.winfo_width() - 72))
        row = tk.Frame(self.message_column, bg=BG)
        row.pack(fill="x", padx=12, pady=(4, 8), before=before)
        content = tk.Frame(row, bg=BG, width=lane_width)
        content.pack(side="right" if role == "user" else "top", anchor="e" if role == "user" else "center")
        content.pack_propagate(True)
        chars = self._body_chars()
        text_size = self._text_size()

        if role == "user":
            bubble = RoundedBubble(content, SURFACE_HI)
            bubble.pack(anchor="e")
            text = tk.Text(
                bubble.panel, wrap=tk.WORD, width=max(12, min(chars, max((len(line) for line in item["text"].splitlines()), default=22))),
                height=1, bg=SURFACE_HI, fg=TEXT, font=(FONT, text_size),
                relief="flat", borderwidth=0, padx=0, pady=0, highlightthickness=0,
                spacing1=2, spacing3=3, state="normal",
            )
            text.pack()
            text.bind("<Configure>", lambda event, widget=text: self._on_text_configure(widget, event))
            self._set_plain_text(text, item["text"], editable=True)
            text.configure(state="disabled")
        elif role == "system":
            color = RED if item.get("error") else MUTED
            text = tk.Label(
                content, text=item["text"], bg=BG, fg=color,
                font=(FONT, 9), wraplength=lane_width, justify="left",
            )
            text.pack(anchor="w", pady=(2, 5))
        else:
            header = tk.Frame(content, bg=BG)
            header.pack(anchor="w", fill="x", pady=(0, 6))
            if self.small_logo_image:
                tk.Label(header, image=self.small_logo_image, bg=BG).pack(side="left", padx=(0, 7))
            tk.Label(header, text="Roman", bg=BG, fg=TEXT, font=(FONT, 10, "bold")).pack(side="left")
            text = tk.Text(
                content, wrap=tk.WORD, width=chars, height=1, bg=BG, fg=TEXT,
                font=(FONT, text_size), relief="flat", borderwidth=0, padx=0, pady=2,
                highlightthickness=0, spacing1=2, spacing3=7, state="normal",
            )
            text.pack(anchor="w", fill="x")
            text.bind("<Configure>", lambda event, widget=text: self._on_text_configure(widget, event))
            self._configure_markdown_tags(text, text_size)
            if item.get("streaming"):
                if not item["text"]:
                    dots = tk.Label(content, text="●  ●  ●", bg=BG, fg=MUTED, font=(FONT, 9))
                    dots.pack(anchor="w", pady=(3, 8))
                    item["typing_widget"] = dots
                else:
                    self._render_markdown(text, item["text"])
            else:
                self._render_markdown(text, item["text"])
            item["text_widget"] = text
            item["content_frame"] = content
            item["header_frame"] = header

        item["row_widget"] = row
        item["main_frame"] = content
        item["text_widget"] = text
        self.message_widgets.append(item)
        self._schedule_text_height(text)
        if role == "system":
            content.configure(height=text.winfo_reqheight() + 8)

    def _update_message_height(self, item: dict) -> None:
        widget = item.get("text_widget")
        if widget is None or not widget.winfo_exists():
            return
        if not isinstance(widget, tk.Text):
            item["main_frame"].configure(height=widget.winfo_reqheight() + 8)
            return
        try:
            # RoundedBubble sizes its embedded panel after Tk processes geometry.
            # Measuring at the initial 1px width counts every character as a line.
            if widget.winfo_width() <= 1:
                return
            count = widget.count("1.0", "end-1c", "displaylines")
            display_lines = count[0] if count else 0
            lines = max(1, display_lines + (1 if item.get("role") == "user" else 0))
            line_height = max(1, tkfont.Font(font=widget.cget("font")).metrics("linespace"))
            embedded_extra = 0
            for child_name in widget.window_names():
                try:
                    child = widget.nametowidget(child_name)
                    embedded_extra += max(0, math.ceil(child.winfo_reqheight() / line_height) - 1)
                except (KeyError, tk.TclError):
                    continue
            widget.configure(height=lines + embedded_extra)
            if item.get("role") == "user":
                # The Canvas window has a fixed height until it is explicitly
                # synchronized with the Text widget's updated requested size.
                bubble = widget.master.master
                self.root.after_idle(bubble._sync_size)
        except tk.TclError:
            return

    def _schedule_text_height(self, widget: tk.Text) -> None:
        if not widget.winfo_exists():
            return
        self._pending_height_widgets.add(widget)
        if self._height_job is not None:
            return
        try:
            self._height_job = self.root.after_idle(self._flush_height_updates)
        except tk.TclError:
            self._height_job = None

    def _on_text_configure(self, widget: tk.Text, event) -> None:
        if getattr(widget, "_roman_configure_width", None) != event.width:
            widget._roman_configure_width = event.width
            self._schedule_text_height(widget)

    def _flush_height_updates(self) -> None:
        self._height_job = None
        try:
            self.root.update_idletasks()
        except tk.TclError:
            return
        widgets, self._pending_height_widgets = self._pending_height_widgets, set()
        for widget in widgets:
            if not widget.winfo_exists():
                continue
            item = next((row for row in self.message_widgets if row.get("text_widget") is widget), None)
            if item:
                self._update_message_height(item)

    def _configure_markdown_tags(self, widget: tk.Text, size: int) -> None:
        widget.tag_configure("paragraph", spacing1=1, spacing3=size)
        widget.tag_configure("heading1", font=(FONT, size + 8, "bold"), spacing1=15, spacing3=12)
        widget.tag_configure("heading2", font=(FONT, size + 5, "bold"), spacing1=13, spacing3=10)
        widget.tag_configure("heading3", font=(FONT, size + 2, "bold"), spacing1=11, spacing3=8)
        widget.tag_configure("bold", font=(FONT, size, "bold"))
        widget.tag_configure("italic", font=(FONT, size, "italic"))
        widget.tag_configure("inline_code", font=(CODE_FONT, size - 1), foreground=app_settings.CODE_INLINE, background=SURFACE)
        widget.tag_configure("list", lmargin1=20, lmargin2=35, spacing3=5)
        widget.tag_configure("quote", lmargin1=14, lmargin2=20, foreground=MUTED, spacing1=4, spacing3=6)
        widget.tag_configure("link", foreground=BLUE, underline=True)

    def _render_markdown(self, widget: tk.Text, markdown: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", tk.END)
        lines = markdown.splitlines()
        index = 0
        while index < len(lines):
            line = lines[index]
            if line.strip().startswith("```"):
                language = line.strip()[3:].strip() or "code"
                index += 1
                code_lines = []
                while index < len(lines) and not lines[index].strip().startswith("```"):
                    code_lines.append(lines[index])
                    index += 1
                self._insert_code_block(widget, "\n".join(code_lines), language)
                index += 1
                continue
            if not line.strip():
                index += 1
                continue
            heading = re.match(r"^(#{1,3})\s+(.*)$", line)
            if heading:
                level = len(heading.group(1))
                self._insert_inline(widget, heading.group(2), f"heading{level}")
                widget.insert(tk.END, "\n")
                index += 1
                continue
            if line.lstrip().startswith(">"):
                self._insert_inline(widget, line.lstrip()[1:].lstrip(), "quote")
                widget.insert(tk.END, "\n", "quote")
                index += 1
                continue
            list_match = re.match(r"^\s*(?:[-*+]\s+|(\d+)\.\s+)(.*)$", line)
            if list_match:
                while index < len(lines):
                    item = re.match(r"^\s*(?:[-*+]\s+|(\d+)\.\s+)(.*)$", lines[index])
                    if not item:
                        break
                    bullet = f"{item.group(1)}." if item.group(1) else "•"
                    widget.insert(tk.END, f"{bullet}  ", "list")
                    self._insert_inline(widget, item.group(2), "list")
                    widget.insert(tk.END, "\n", "list")
                    index += 1
                widget.insert(tk.END, "\n")
                continue
            paragraph = [line]
            index += 1
            while index < len(lines) and lines[index].strip() and not lines[index].strip().startswith("```") and not re.match(r"^(#{1,3})\s+", lines[index]) and not re.match(r"^\s*(?:[-*+]\s+|\d+\.\s+)", lines[index]):
                paragraph.append(lines[index])
                index += 1
            self._insert_inline(widget, "\n".join(paragraph), "paragraph")
            widget.insert(tk.END, "\n", "paragraph")
        if not markdown:
            widget.insert(tk.END, "")
        widget.configure(state="disabled")
        self._update_message_height_for_widget(widget)

    def _insert_inline(self, widget: tk.Text, text: str, base_tag: str) -> None:
        pattern = re.compile(r"(\*\*.+?\*\*|__.+?__|`[^`]+`|\*[^*]+\*|_[^_]+_|\[[^\]]+\]\([^)]+\))")
        offset = 0
        for match in pattern.finditer(text):
            if match.start() > offset:
                widget.insert(tk.END, text[offset:match.start()], base_tag)
            token = match.group(0)
            if token.startswith(("**", "__")):
                widget.insert(tk.END, token[2:-2], (base_tag, "bold"))
            elif token.startswith("`"):
                widget.insert(tk.END, token[1:-1], (base_tag, "inline_code"))
            elif token.startswith(("*", "_")):
                widget.insert(tk.END, token[1:-1], (base_tag, "italic"))
            else:
                link = re.match(r"\[([^\]]+)\]\(([^)]+)\)", token)
                if link:
                    widget.insert(tk.END, link.group(1), (base_tag, "link"))
            offset = match.end()
        if offset < len(text):
            widget.insert(tk.END, text[offset:], base_tag)

    def _insert_code_block(self, widget: tk.Text, code: str, language: str) -> None:
        panel = tk.Frame(widget, bg=SURFACE, padx=10, pady=8, highlightthickness=1, highlightbackground=SEPARATOR)
        toolbar = tk.Frame(panel, bg=SURFACE)
        toolbar.pack(fill="x", pady=(0, 6))
        tk.Label(toolbar, text=language.upper(), bg=SURFACE, fg=MUTED, font=(FONT, 8, "bold")).pack(side="left")
        copy = tk.Button(toolbar, text="Copy", bg=SURFACE, fg=BLUE, activebackground=SURFACE_HI,
                         activeforeground=TEXT, font=(FONT, 8), cursor="hand2", padx=7,
                         relief="flat", borderwidth=0, highlightthickness=1,
                         highlightbackground=SURFACE, highlightcolor=BLUE, takefocus=True,
                         command=lambda value=code: self._copy_code(value, copy))
        copy.pack(side="right")

        code_area = tk.Frame(panel, bg=SURFACE)
        code_area.pack(fill="x")
        lines = max(1, len(code.splitlines()))
        code_size = max(9, self._text_size() - 1)
        code_text = tk.Text(
            code_area, wrap="none", height=min(22, lines), width=max(32, min(self._body_chars(), max((len(line) for line in code.splitlines()), default=32))),
            bg=SURFACE, fg=TEXT, insertbackground=TEXT, font=(CODE_FONT, code_size),
            relief="flat", borderwidth=0, padx=2, pady=3, highlightthickness=0,
        )
        code_area.grid_columnconfigure(0, weight=1)
        code_text.grid(row=0, column=0, sticky="ew")
        code_text.insert("1.0", code)
        code_text.tag_configure("kw", foreground=app_settings.CODE_KEYWORD)
        code_text.tag_configure("str", foreground=app_settings.CODE_STRING)
        code_text.tag_configure("comment", foreground=app_settings.CODE_COMMENT, font=(CODE_FONT, code_size, "italic"))
        code_text.tag_configure("num", foreground=app_settings.CODE_NUMBER)
        code_text.tag_configure("builtin", foreground=app_settings.CODE_BUILTIN)
        code_text.tag_configure("function", foreground=app_settings.CODE_FUNCTION)
        self._highlight_code(code_text, code, language)
        code_text.configure(state="disabled")
        line_width = max((len(row) for row in code.splitlines()), default=0)
        if lines > 22:
            vertical = tk.Scrollbar(code_area, orient="vertical", command=code_text.yview,
                                    bg=SEPARATOR, troughcolor=SURFACE, activebackground=MUTED,
                                    relief="flat", borderwidth=0, width=8)
            vertical.grid(row=0, column=1, sticky="ns")
            code_text.configure(yscrollcommand=vertical.set)
        if code and line_width > self._body_chars():
            bar = tk.Scrollbar(code_area, orient="horizontal", command=code_text.xview,
                               bg=SEPARATOR, troughcolor=SURFACE, activebackground=MUTED,
                               relief="flat", borderwidth=0, width=8)
            bar.grid(row=1, column=0, sticky="ew")
            code_text.configure(xscrollcommand=bar.set)
        widget.window_create(tk.END, window=panel, align="top")
        widget.insert(tk.END, "\n\n")

    @staticmethod
    def _highlight_code(widget: tk.Text, code: str, language: str) -> None:
        if language.casefold() in {"py", "python", "python3"}:
            try:
                for token in tokenize.generate_tokens(io.StringIO(code).readline):
                    if token.type == tokenize.NAME and token.string in __import__("keyword").kwlist:
                        tag = "kw"
                    elif token.type == tokenize.STRING:
                        tag = "str"
                    elif token.type == tokenize.COMMENT:
                        tag = "comment"
                    elif token.type == tokenize.NUMBER:
                        tag = "num"
                    elif token.type == tokenize.NAME and token.string in {"True", "False", "None", "print", "len", "range", "self"}:
                        tag = "builtin"
                    else:
                        continue
                    widget.tag_add(tag, f"{token.start[0]}.{token.start[1]}", f"{token.end[0]}.{token.end[1]}")
                return
            except (tokenize.TokenError, IndentationError, SyntaxError):
                pass
        keywords = r"\b(?:const|let|var|function|return|if|else|for|while|class|import|from|async|await|def|new|true|false|null|None)\b"
        for match in re.finditer(r"(['\"`])(?:\\.|(?!\1).)*?\1", code):
            widget.tag_add("str", f"1.0+{match.start()}c", f"1.0+{match.end()}c")
        for match in re.finditer(r"\b\d+(?:\.\d+)?\b", code):
            widget.tag_add("num", f"1.0+{match.start()}c", f"1.0+{match.end()}c")
        for match in re.finditer(keywords, code):
            widget.tag_add("kw", f"1.0+{match.start()}c", f"1.0+{match.end()}c")
        for match in re.finditer(r"(?:#|//).*", code):
            widget.tag_add("comment", f"1.0+{match.start()}c", f"1.0+{match.end()}c")

    def _copy_code(self, code: str, label) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(code)
        label.configure(text="Copied", fg=GREEN)
        self.root.after(1200, lambda: label.winfo_exists() and label.configure(text="Copy", fg=BLUE))

    def _set_plain_text(self, widget: tk.Text, text: str, editable=False) -> None:
        widget.insert(tk.END, text)
        self._schedule_text_height(widget)

    def _update_message_height_for_widget(self, widget: tk.Text) -> None:
        self._schedule_text_height(widget)

    def _append_user_message(self, content: str) -> None:
        if self.current_conversation_id is None:
            self.current_conversation_id = self.assistant.memory.session_id
        if not self.chat_items:
            self._clear_chat_widgets()
        item = {"role": "user", "text": content, "streaming": False}
        self.chat_items.append(item)
        self._render_item(item)
        self._scroll_bottom(force=True)

    def _begin_assistant_message(self) -> None:
        item = {"role": "assistant", "text": "", "streaming": True}
        self._stream_should_follow = self._is_near_bottom()
        self.chat_items.append(item)
        self._active_response = item
        self._response_streaming = True
        self._render_item(item)
        self._start_typing_indicator()
        self._scroll_bottom()

    def _receive_tokens(self, tokens: str) -> None:
        item = self._active_response
        if not item or not self._response_streaming:
            return
        stay_at_bottom = self._is_near_bottom()
        item["text"] += tokens
        typing = item.pop("typing_widget", None)
        if typing and typing.winfo_exists():
            typing.destroy()
        self._schedule_stream_render(stay_at_bottom)

    def _schedule_stream_render(self, stay_at_bottom=None) -> None:
        if stay_at_bottom is None:
            stay_at_bottom = self._is_near_bottom()
        self._stream_should_follow = stay_at_bottom
        if self._stream_render_job is None:
            self._stream_render_job = self.root.after(STREAM_RENDER_MS, self._render_streaming_response)

    def _render_streaming_response(self) -> None:
        self._stream_render_job = None
        item = self._active_response
        if not item or not item.get("streaming"):
            return
        widget = item.get("text_widget")
        if widget and widget.winfo_exists():
            self._render_markdown(widget, self._stream_visible_markdown(item["text"]))
        follow = bool(getattr(self, "_stream_should_follow", False) and self._is_near_bottom())
        self._scroll_bottom(force=follow)
        self._stream_should_follow = self._is_near_bottom()

    @staticmethod
    def _stream_visible_markdown(markdown: str) -> str:
        """Avoid flashing incomplete inline Markdown delimiters while streaming."""
        visible = markdown
        for marker in ("**", "__", "`", "*", "_"):
            positions = [match.start() for match in re.finditer(re.escape(marker), visible)]
            if len(positions) % 2:
                visible = visible[:positions[-1]]
        incomplete_link = re.search(r"\[[^\]]*\]\([^)]*$", visible)
        if incomplete_link:
            visible = visible[:incomplete_link.start()]
        return visible

    def _set_plain_height(self, widget: tk.Text) -> None:
        self._schedule_text_height(widget)

    def _finish_assistant_message(self, content: str, completed: bool) -> None:
        item = self._active_response
        if item is None:
            return
        stay_at_bottom = self._is_near_bottom()
        if content:
            item["text"] = content
        if self._stream_render_job is not None:
            self.root.after_cancel(self._stream_render_job)
            self._stream_render_job = None
        item["streaming"] = False
        self._response_streaming = False
        self._stop_typing_indicator()
        typing = item.pop("typing_widget", None)
        if typing and typing.winfo_exists():
            typing.destroy()
        if item.get("text_widget") and item["text_widget"].winfo_exists():
            self._render_markdown(item["text_widget"], item["text"])
        self._active_response = None
        if not completed and not item["text"]:
            try:
                self.chat_items.remove(item)
                item["row_widget"].destroy()
            except (ValueError, tk.TclError):
                pass
        self._scroll_bottom(force=stay_at_bottom)

    def _drain_events(self) -> None:
        tokens = []
        queue = self.assistant.events.events
        while not queue.empty():
            event = queue.get_nowait()
            kind = event[0]
            if kind == "generation_token":
                tokens.append(event[1])
                continue
            if tokens:
                self._receive_tokens("".join(tokens))
                tokens.clear()
            if kind == "state":
                self._set_state(event[1], event[2])
            elif kind == "message":
                if event[1].casefold() == "you":
                    self._append_user_message(event[2])
            elif kind == "log":
                self._append_system(event[1], error=bool(event[2]) if len(event) > 2 else False)
            elif kind == "generation_start":
                self._begin_assistant_message()
            elif kind == "generation_end":
                self._finish_assistant_message(event[1], event[2])
            elif kind == "conversation_reset":
                self.chat_items = []
                self.current_conversation_id = self.assistant.memory.session_id
                self._show_welcome()
            elif kind == "conversation_saved":
                self.current_conversation_id = self.assistant.memory.session_id
                if self._history_open:
                    self._refresh_history()
        if tokens:
            self._receive_tokens("".join(tokens))
        self.root.after(30, self._drain_events)

    def _append_system(self, message: str, error: bool = False) -> None:
        self._show_toast(message, error=error)

    def _show_toast(self, message: str, error: bool = False) -> None:
        self.notice_label.configure(text=message, fg=RED if error else MUTED)
        if self._toast_job:
            self.root.after_cancel(self._toast_job)
        self._toast_job = self.root.after(5000, lambda: self.notice_label.configure(text="", fg=MUTED))

    def _set_state(self, state: AssistantState, message: str = "") -> None:
        self.state = state
        if self._error_reset_job:
            self.root.after_cancel(self._error_reset_job)
            self._error_reset_job = None
        mapping = {
            AssistantState.ONLINE: ("Online", GREEN),
            AssistantState.LISTENING: ("Listening", BLUE),
            AssistantState.TRANSCRIBING: ("Transcribing", BLUE),
            AssistantState.THINKING: ("Thinking", BLUE),
            AssistantState.SPEAKING: ("Speaking", GREEN),
            AssistantState.PAUSED: ("Paused", AMBER),
            AssistantState.CANCELLED: ("Online", MUTED),
            AssistantState.ERROR: ("Error", RED),
            AssistantState.SHUTTING_DOWN: ("Offline", MUTED),
        }
        label, color = mapping[state]
        self.status_text.configure(text=label)
        self.status_dot.configure(fg=color)
        busy_states = {
            AssistantState.LISTENING, AssistantState.TRANSCRIBING,
            AssistantState.THINKING, AssistantState.SPEAKING,
            AssistantState.PAUSED,
        }
        if state in busy_states:
            self.stop_button.grid()
        else:
            self.stop_button.grid_remove()
        listening = state == AssistantState.LISTENING
        self.voice_button.configure(
            text="■  Stop recording" if listening else "◉  Voice",
            foreground=RED if listening else TEXT,
            state="normal" if state in (AssistantState.ONLINE, AssistantState.CANCELLED, AssistantState.ERROR, AssistantState.LISTENING) else "disabled",
        )
        if state == AssistantState.ERROR:
            self._error_reset_job = self.root.after(5000, self._reset_error_indicator)
        self._on_composer_change()

    def _reset_error_indicator(self) -> None:
        self._error_reset_job = None
        if self.state == AssistantState.ERROR:
            self.state = AssistantState.ONLINE
            self.status_text.configure(text="Online")
            self.status_dot.configure(fg=GREEN)
            self._on_composer_change()

    def _animate_status(self) -> None:
        self._status_phase = (self._status_phase + 1) % 2
        if not self.preferences.get("reduce_motion") and self.state in (AssistantState.LISTENING, AssistantState.THINKING, AssistantState.SPEAKING):
            colors = {AssistantState.LISTENING: BLUE, AssistantState.THINKING: app_settings.STATUS_THINKING, AssistantState.SPEAKING: GREEN}
            self.status_dot.configure(fg=colors[self.state] if self._status_phase else app_settings.STATUS_DIM)
        if self._typing_active:
            self._typing_phase = (self._typing_phase + 1) % 4
            if self._active_response and not self._active_response["text"]:
                label = self._active_response.get("typing_widget")
                if label and label.winfo_exists():
                    dots = "●  ●  ●" if self.preferences.get("reduce_motion") else "· " * (1 + self._typing_phase % 3)
                    label.configure(text=dots)
        interval = 800 if self.preferences.get("reduce_motion") else 500
        self.root.after(interval, self._animate_status)

    def _start_typing_indicator(self) -> None:
        self._typing_active = True

    def _stop_typing_indicator(self) -> None:
        self._typing_active = False

    def _on_chat_resize(self, event) -> None:
        self.chat_canvas.itemconfigure(self.message_window, width=event.width)
        if self._resize_job:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(100, self._resize_messages)

    def _resize_messages(self) -> None:
        self._resize_job = None
        width = self.chat_canvas.winfo_width()
        if self._last_render_width is not None and abs(width - self._last_render_width) <= 40:
            return
        stay_at_bottom = self._is_near_bottom()
        scroll_position = self.chat_canvas.yview()[0]
        code_views = self._capture_code_views(self.chat_items)
        self._last_render_width = width
        self._clear_chat_widgets()
        if not self.chat_items and not self._history_messages:
            self._show_welcome()
            return
        if self._history_start:
            self._make_load_earlier_button().pack(fill="x", pady=(5, 12))
        for item in self.chat_items:
            self._render_item(item)
        self.root.after_idle(lambda: self.chat_canvas.yview_moveto(1.0 if stay_at_bottom else scroll_position))
        self.root.after_idle(lambda: self._restore_code_views(code_views))

    def _on_messages_changed(self, _event=None) -> None:
        self.chat_canvas.configure(scrollregion=self.chat_canvas.bbox("all"))

    def _on_mousewheel(self, event) -> None:
        widget_path = str(event.widget)
        chat_path = str(self.chat_canvas)
        in_chat = widget_path == chat_path or widget_path.startswith(chat_path + ".")
        if in_chat and self.chat_canvas.winfo_exists():
            self.chat_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            return "break"
        return None

    def _is_near_bottom(self) -> bool:
        try:
            region = self.chat_canvas.bbox("all")
            if not region:
                return True
            viewport_bottom = self.chat_canvas.canvasy(self.chat_canvas.winfo_height())
            return region[3] - viewport_bottom <= 40
        except tk.TclError:
            return True

    def _scroll_bottom(self, force=False) -> None:
        if not force and not self._is_near_bottom():
            return
        self.root.after_idle(lambda: self.chat_canvas.yview_moveto(1.0) if self.chat_canvas.winfo_exists() else None)

    def close(self) -> None:
        self.assistant.shutdown()
        self.root.destroy()
