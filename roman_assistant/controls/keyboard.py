"""Tk window-scoped controls, suppressed while the message field has focus."""


def bind_controls(root, callbacks, input_widget):
    def dispatch(key):
        # Escape is a dedicated stop shortcut and remains available while typing.
        if key != "escape" and root.focus_get() == input_widget:
            return "break"
        callback = callbacks.get(key)
        if callback:
            callback()
        return "break"

    for key in ("c", "a", "b", "e", "d"):
        root.bind(f"<KeyPress-{key}>", lambda event, value=key: dispatch(value))
    root.bind("<Escape>", lambda _event: dispatch("escape"))
