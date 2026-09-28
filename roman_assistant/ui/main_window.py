"""Dark, minimal desktop interface for ROMAN Ⅰ."""

import tkinter as tk
from tkinter import scrolledtext

from config.settings import (
    APP_NAME,
    APP_SUBTITLE,
    BG,
    BLUE,
    CYAN,
    FONT,
    GOLD,
    GREEN,
    LINE,
    MIN_WINDOW_SIZE,
    MUTED,
    PANEL,
    PANEL_ALT,
    TEXT,
)
from controls.keyboard import bind_controls
from core.state import AssistantState


class RomanWindow:
    def __init__(self, root: tk.Tk, assistant) -> None:
        self.root = root
        self.assistant = assistant
        self.root.title(APP_NAME)
        self.root.configure(bg=BG)
        self.root.minsize(*MIN_WINDOW_SIZE)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        self._append_system("ROMAN Ⅰ is ready.")
        self._append_system("Whisper Base · CUDA when available")
        self._append_system("Ollama · Llama 3.2 3B")
        self._append_system("Windows SAPI speech")
        self.root.after(60, self._drain_events)

    def _build(self) -> None:
        self.root.grid_rowconfigure(1, weight=1)
        self.root.grid_columnconfigure(1, weight=1)
        self._build_header()
        self._build_sidebar()
        self._build_center()
        self._build_voice_bar()

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=BG, height=84)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=24, pady=(14, 8))
        header.grid_columnconfigure(1, weight=1)
        self._draw_logo(header, size=58)
        title = tk.Frame(header, bg=BG)
        title.grid(row=0, column=1, sticky="w", padx=(12, 0))
        tk.Label(title, text=APP_NAME, bg=BG, fg=TEXT, font=(FONT, 20, "bold")).pack(anchor="w")
        tk.Label(title, text=APP_SUBTITLE, bg=BG, fg=MUTED, font=(FONT, 9, "bold")).pack(anchor="w", pady=(1, 0))
        self.status = tk.Label(
            header, text=f"●  {APP_NAME} · Online", bg=PANEL, fg=GREEN,
            font=(FONT, 10, "bold"), padx=14, pady=8,
        )
        self.status.grid(row=0, column=2, sticky="e", padx=(8, 0))

    def _build_sidebar(self) -> None:
        sidebar = tk.Frame(self.root, bg=BG, width=118)
        sidebar.grid(row=1, column=0, sticky="ns", padx=(18, 12), pady=(8, 12))
        sidebar.grid_propagate(False)
        self.nav_buttons = {}
        for label, icon in (("Chat", "◉"), ("Files", "▱"), ("Tools", "⌘"), ("System", "◎"), ("Settings", "⚙")):
            active = label == "Chat"
            button = tk.Button(
                sidebar,
                text=f"{icon}\n{label}",
                command=lambda name=label: self._select_nav(name),
                bg=PANEL_ALT if active else BG,
                fg=CYAN if active else MUTED,
                activebackground=PANEL_ALT,
                activeforeground=TEXT,
                relief="flat",
                borderwidth=0,
                font=(FONT, 10, "bold"),
                justify="center",
                width=10,
                height=3,
                cursor="hand2",
            )
            button.pack(fill="x", pady=5)
            self.nav_buttons[label] = button

    def _build_center(self) -> None:
        panel = tk.Frame(self.root, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        panel.grid(row=1, column=1, sticky="nsew", padx=(0, 24), pady=(8, 12))
        panel.grid_rowconfigure(1, weight=1)
        panel.grid_columnconfigure(0, weight=1)
        greeting = tk.Frame(panel, bg=PANEL)
        greeting.grid(row=0, column=0, sticky="ew", padx=24, pady=(20, 10))
        tk.Label(greeting, text="Your assistant, ready when you are.", bg=PANEL, fg=TEXT, font=(FONT, 16, "bold")).pack(anchor="w")
        tk.Label(greeting, text="Ask by voice or type a message below.", bg=PANEL, fg=MUTED, font=(FONT, 10)).pack(anchor="w", pady=(4, 0))
        self.chat = scrolledtext.ScrolledText(
            panel, wrap=tk.WORD, bg=PANEL, fg=TEXT, insertbackground=TEXT,
            relief="flat", borderwidth=0, padx=22, pady=12, spacing1=4,
            font=(FONT, 11), state="disabled", highlightthickness=0,
        )
        self.chat.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        self.chat.tag_configure("system", foreground=MUTED, font=(FONT, 9))
        self.chat.tag_configure("you", foreground=CYAN, font=(FONT, 10, "bold"))
        self.chat.tag_configure("roman", foreground=TEXT, font=(FONT, 10, "bold"))
        self.chat.tag_configure("body", foreground=TEXT, lmargin1=12, lmargin2=12, spacing3=12)
        self.chat.tag_configure("error", foreground="#ff9e9e", font=(FONT, 10))

    def _build_voice_bar(self) -> None:
        bar = tk.Frame(self.root, bg=BG)
        bar.grid(row=2, column=0, columnspan=2, sticky="ew", padx=24, pady=(0, 20))
        bar.grid_columnconfigure(1, weight=1)
        self.mic = tk.Button(
            bar, text="🎙", command=self.assistant.toggle_listening,
            bg=PANEL_ALT, fg=CYAN, activebackground="#163450", activeforeground=TEXT,
            relief="flat", font=(FONT, 18), width=4, height=2, cursor="hand2",
        )
        self.mic.grid(row=0, column=0, sticky="w", padx=(0, 12))
        entry_frame = tk.Frame(bar, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        entry_frame.grid(row=0, column=1, sticky="ew")
        entry_frame.grid_columnconfigure(0, weight=1)
        self.entry = tk.Entry(
            entry_frame, bg=PANEL, fg=TEXT, insertbackground=CYAN, relief="flat",
            font=(FONT, 11), borderwidth=0,
        )
        self.entry.insert(0, "Type a message…  ·  Click the mic or press C")
        self.entry.bind("<FocusIn>", self._clear_placeholder)
        self.entry.bind("<Return>", lambda _event: self._send())
        self.entry.grid(row=0, column=0, sticky="ew", padx=16, pady=16)
        self.send = tk.Button(
            entry_frame, text="➤", command=self._send, bg=PANEL, fg=CYAN,
            activebackground=PANEL_ALT, activeforeground=TEXT, relief="flat",
            font=(FONT, 15, "bold"), cursor="hand2", padx=14,
        )
        self.send.grid(row=0, column=1, padx=(0, 8))
        self.stop_button = tk.Button(
            entry_frame, text="■  Stop", command=self.assistant.cancel,
            bg="#172738", fg="#ff9b9b", activebackground="#293b4c",
            activeforeground=TEXT, relief="flat", font=(FONT, 9, "bold"),
            cursor="hand2", padx=12, pady=8,
        )
        self.stop_button.grid(row=0, column=2, padx=(0, 8))
        self.state_text = tk.Label(bar, text="C Listen / stop   ·   A Resume   ·   B Pause   ·   Esc / Stop Cancel   ·   D Exit", bg=BG, fg=MUTED, font=(FONT, 8))
        self.state_text.grid(row=1, column=1, sticky="e", pady=(8, 0))
        bind_controls(
            self.root,
            {"c": self.assistant.toggle_listening, "a": self.assistant.resume,
             "b": self.assistant.pause, "e": self.assistant.cancel, "d": self.close,
             "escape": self.assistant.cancel},
            self.entry,
        )

    @staticmethod
    def _draw_logo(parent, size: int = 56) -> tk.Canvas:
        c = tk.Canvas(parent, width=size, height=size, bg=BG, highlightthickness=0)
        c.grid(row=0, column=0, sticky="w")
        # Crown above a simple R + Roman numeral one mark.
        c.create_line(size*.25, size*.31, size*.19, size*.15, size*.38, size*.25,
                      size*.50, size*.08, size*.62, size*.25, size*.81, size*.15,
                      size*.75, size*.31, fill=CYAN, width=max(1, size//24), joinstyle="round")
        c.create_line(size*.27, size*.34, size*.73, size*.34, fill=BLUE, width=max(1, size//20))
        c.create_line(size*.28, size*.42, size*.28, size*.86, fill=TEXT, width=max(2, size//13))
        c.create_line(size*.28, size*.42, size*.61, size*.42, size*.71, size*.49,
                      size*.71, size*.59, size*.61, size*.66, size*.43, size*.66,
                      fill=CYAN, width=max(2, size//13), smooth=True, capstyle="round", joinstyle="round")
        c.create_line(size*.46, size*.66, size*.70, size*.87, fill=BLUE, width=max(2, size//13), capstyle="round")
        c.create_line(size*.82, size*.43, size*.82, size*.87, fill=TEXT, width=max(2, size//13), capstyle="round")
        return c

    def _clear_placeholder(self, _event=None) -> None:
        if self.entry.get().startswith("Type a message"):
            self.entry.delete(0, tk.END)

    def _send(self) -> None:
        text = self.entry.get().strip()
        if not text or text.startswith("Type a message"):
            return
        self.entry.delete(0, tk.END)
        self.assistant.send_message(text)

    def _append_system(self, text: str, error: bool = False) -> None:
        self.chat.configure(state="normal")
        self.chat.insert(tk.END, f"{APP_NAME}  ·  {text}\n\n", "error" if error else "system")
        self.chat.see(tk.END)
        self.chat.configure(state="disabled")

    def _append_message(self, author: str, text: str) -> None:
        self.chat.configure(state="normal")
        tag = "you" if author == "You" else "roman"
        self.chat.insert(tk.END, f"{author}\n", tag)
        self.chat.insert(tk.END, f"{text}\n\n", "body")
        self.chat.see(tk.END)
        self.chat.configure(state="disabled")

    def _drain_events(self) -> None:
        while not self.assistant.events.events.empty():
            event = self.assistant.events.events.get_nowait()
            if event[0] == "state":
                self._set_state(event[1], event[2])
            elif event[0] == "message":
                self._append_message(event[1], event[2])
            elif event[0] == "log":
                self._append_system(event[1])
        self.root.after(60, self._drain_events)

    def _set_state(self, state: AssistantState, message: str) -> None:
        colors = {
            AssistantState.ONLINE: GREEN,
            AssistantState.LISTENING: GOLD,
            AssistantState.TRANSCRIBING: CYAN,
            AssistantState.THINKING: BLUE,
            AssistantState.SPEAKING: CYAN,
            AssistantState.PAUSED: GOLD,
            AssistantState.CANCELLED: MUTED,
            AssistantState.ERROR: "#ff7777",
            AssistantState.SHUTTING_DOWN: MUTED,
        }
        self.status.configure(text=f"●  {APP_NAME} · {state.name.title()}", fg=colors[state])
        self.state_text.configure(text=f"{APP_NAME}  ·  {message or 'Ready'}")
        self.mic.configure(fg=colors[state])
        if state == AssistantState.ERROR:
            self._append_system(message, error=True)

    def _select_nav(self, name: str) -> None:
        for label, button in self.nav_buttons.items():
            active = label == name
            button.configure(bg=PANEL_ALT if active else BG, fg=CYAN if active else MUTED)
        if name == "System":
            self._append_system("ROMAN Ⅰ · Personal AI Assistant")
            self._append_system("Whisper Base · CUDA / CPU fallback")
            self._append_system("Ollama · Llama 3.2 3B · Windows SAPI")
        elif name != "Chat":
            self._append_system(f"{name} tools are ready to be connected.")

    def close(self) -> None:
        self.assistant.shutdown()
        self.root.destroy()
