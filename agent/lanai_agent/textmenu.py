"""Вставка и копирование в окне программы: меню по правой кнопке мыши («Вырезать / Копировать / Вставить / Выделить всё») и сочетания Ctrl+V, Ctrl+C, Ctrl+X, Ctrl+A при ЛЮБОЙ
раскладке клавиатуры, а также Shift+Insert, Ctrl+Insert, Shift+Delete.

В tkinter сочетания «Ctrl+буква» привязаны к самой букве, поэтому при русской раскладке (Ctrl+М вместо Ctrl+V) вставка не срабатывала; здесь они привязаны к коду клавиши
Windows (V=86, C=67, X=88, A=65), который от раскладки не зависит. При латинской раскладке работают обычные привязки Tk (тогда этот обработчик ничего не делает — иначе вставка
выполнилась бы дважды)."""
from __future__ import annotations

import tkinter as tk

_CLASSES = ("Entry", "TEntry", "Spinbox", "TSpinbox", "Text", "TCombobox")
_BY_KEYCODE = {86: "<<Paste>>", 67: "<<Copy>>", 88: "<<Cut>>", 65: "<<SelectAll>>"}      # V, C, X, A (коды клавиш Windows)
_LATIN = {"v", "c", "x", "a"}


def read_only(widget: tk.Misc) -> bool:
    """Поле только для чтения: журнал (Text в состоянии disabled) или список «readonly» — туда вставлять нельзя."""
    try:
        state = str(widget.cget("state"))
    except tk.TclError:
        return False
    return state in ("disabled", "readonly")


def _fire(widget: tk.Misc, virtual: str) -> None:
    if virtual in ("<<Paste>>", "<<Cut>>") and read_only(widget):
        return
    widget.event_generate(virtual)


def _on_ctrl(event: tk.Event) -> str | None:
    if (event.keysym or "").lower() in _LATIN:
        return None                                   # латиница: обычные привязки Tk сами всё сделают
    virtual = _BY_KEYCODE.get(event.keycode)
    if virtual is None:
        return None
    _fire(event.widget, virtual)
    return "break"


def _show_menu(event: tk.Event) -> str:
    widget = event.widget
    try:
        widget.focus_set()
    except tk.TclError:
        pass
    ro = read_only(widget)
    menu = tk.Menu(widget, tearoff=0)
    menu.add_command(label="Вырезать", command=lambda: _fire(widget, "<<Cut>>"), state="disabled" if ro else "normal")
    menu.add_command(label="Копировать", command=lambda: _fire(widget, "<<Copy>>"))
    menu.add_command(label="Вставить", command=lambda: _fire(widget, "<<Paste>>"), state="disabled" if ro else "normal")
    menu.add_separator()
    menu.add_command(label="Выделить всё", command=lambda: _fire(widget, "<<SelectAll>>"))
    try:
        menu.tk_popup(event.x_root, event.y_root)
    finally:
        menu.grab_release()
    return "break"


def install(root: tk.Misc) -> None:
    """Включить меню и сочетания для всех полей ввода окна (в том числе созданных позже)."""
    for cls in _CLASSES:
        root.bind_class(cls, "<Control-KeyPress>", _on_ctrl)
        root.bind_class(cls, "<Button-3>", _show_menu)
        root.bind_class(cls, "<Shift-Insert>", lambda e: (_fire(e.widget, "<<Paste>>"), "break")[1])
        root.bind_class(cls, "<Control-Insert>", lambda e: (_fire(e.widget, "<<Copy>>"), "break")[1])
        root.bind_class(cls, "<Shift-Delete>", lambda e: (_fire(e.widget, "<<Cut>>"), "break")[1])
