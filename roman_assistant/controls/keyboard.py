"""Tk window-scoped controls, suppressed while the message field has focus."""


def bind_controls(root, callbacks, input_widget):
    def dispatch(key, event):
        # Do not hijack standard editing shortcuts such as Ctrl+C/Ctrl+V.
        # Tk key state uses bit 0x4 for Control and 0x8 for Alt on Windows.
        if event.state & 0x0C:
            return None
        # Escape is a dedicated stop shortcut and remains available while typing.
        if key != "escape" and root.focus_get() == input_widget:
            return None
        callback = callbacks.get(key)
        if callback:
            callback()
        return "break"

    # Keep the established voice/playback shortcuts, but never bind a bare
    # letter to application shutdown: typing "d" must not close Roman.
    for key in ("c", "a", "b", "e"):
        root.bind(f"<KeyPress-{key}>", lambda event, value=key: dispatch(value, event))
    root.bind("<Escape>", lambda event: dispatch("escape", event))

    def chord(key):
        callback = callbacks.get(key)
        if callback:
            callback()
        return "break"

    root.bind("<Control-n>", lambda _event: chord("new"))
    root.bind("<Control-Shift-M>", lambda _event: chord("c"))
