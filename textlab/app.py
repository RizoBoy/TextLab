"""Russian desktop UI; keyboard acquisition is scoped to the active editor."""
from __future__ import annotations

import argparse
import os
import re
import time
import traceback
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .core import PRESET_TOPICS, Recorder, full_report, validate_settings, words
from .export import METHODS, export_csv, export_html, export_json, fmt
from .storage import Store
from .runtime import InstanceLock

BG = "#eef3f5"
WHITE = "#ffffff"
INK = "#203748"
MUTED = "#687e8d"
TEAL = "#087f78"
LINE = "#d9e3e9"


class ScrollFrame(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0)
        bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = ttk.Frame(self.canvas)
        self.item = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.item, width=e.width))
        # A bindtag scoped to this scrollframe, removed when destroyed.
        self.tag = "Scroll" + str(id(self))
        self.bind_class(self.tag, "<MouseWheel>", self._wheel)
        self.after_idle(self._tag_children)
        self.bind("<Destroy>", self._destroyed, add=True)

    def _tag_children(self):
        def visit(widget):
            if not isinstance(widget, (tk.Text, tk.Listbox, ttk.Treeview, ttk.Combobox)):
                widget.bindtags((*widget.bindtags(), self.tag))
            for child in widget.winfo_children():
                visit(child)
        if self.winfo_exists():
            visit(self)

    def _wheel(self, event):
        self.canvas.yview_scroll(-int(event.delta / 120), "units")

    def _destroyed(self, event):
        if event.widget is self:
            self.unbind_class(self.tag, "<MouseWheel>")


class ObservedText(tk.Text):
    """Observe every atomic Tk edit, including paste, selections and undo."""
    def __init__(self, parent, callback, **kwargs):
        super().__init__(parent, **kwargs)
        self.callback = callback
        self.suppress = False
        self.depth = 0
        self.pending_edit = None
        self.original = self._w + "_original"
        self.before_command = self._w + "_before"
        self.after_command = self._w + "_after"
        self.tk.call("rename", self._w, self.original)
        self.tk.createcommand(self.before_command, self._before_edit)
        self.tk.createcommand(self.after_command, self._after_edit)
        # Native queries must preserve Tcl errors (Text bindings use catch on
        # sel.first). Sending those through a Python command leaks exceptions
        # into mainloop even when the calling Tcl binding handles the error.
        body = f'''
            set op [lindex $args 0]
            set change [expr {{$op in {{insert delete replace}} || ($op eq "edit" && [lindex $args 1] in {{undo redo}})}}]
            if {{$change}} {{ {self.before_command} {{*}}$args }}
            set code [catch {{ {self.original} {{*}}$args }} result options]
            if {{$change}} {{ {self.after_command} }}
            return -options $options $result
        '''
        self.tk.call("proc", self._w, "args", body)

    def selection_offsets(self):
        ranges = self.tag_ranges("sel")
        # Python code-point offsets are stable for Cyrillic and emoji.
        return [len(self.get("1.0", ranges[0])), len(self.get("1.0", ranges[1]))] if ranges else None

    def _before_edit(self, *args):
        observe = not self.suppress and self.depth == 0
        if observe:
            stamp = time.perf_counter_ns()
            before = self.get("1.0", "end-1c")
            cursor = len(self.get("1.0", "insert"))
            selection = self.selection_offsets()
            self.pending_edit = (before, cursor, selection, stamp, args)
        self.depth += 1

    def _after_edit(self):
        self.depth -= 1
        if self.depth == 0 and self.pending_edit is not None:
            before, cursor, selection, stamp, args = self.pending_edit
            self.pending_edit = None
            after = self.get("1.0", "end-1c")
            if before != after:
                self.callback(after, cursor, selection, stamp, args)

    def replace_all(self, text: str):
        self.suppress = True
        try:
            self.configure(state="normal")
            self.delete("1.0", "end")
            self.insert("1.0", text)
            self.edit_reset()
            self.mark_set("insert", "end-1c")
        finally:
            self.suppress = False

    def destroy(self):
        super().destroy()
        for command in (self.before_command, self.after_command):
            try:
                self.tk.deletecommand(command)
            except tk.TclError:
                pass
        self.tk.call("rename", self._w, "")


class TextLab(tk.Tk):
    def __init__(self, data_dir: Path):
        super().__init__()
        self.title("TextLab — исследование текстового ввода")
        self.geometry(f"{min(1320, self.winfo_screenwidth()-80)}x{min(900, self.winfo_screenheight()-100)}")
        self.minsize(1020, 680)
        self.configure(bg=BG)
        self.data_dir = data_dir
        self.store = Store(data_dir / "textlab.sqlite3")
        self.rec: Recorder | None = None
        self.persisted = 0
        self.last_save = 0.0
        self.pending_source = "keyboard"
        self.last_selection = None
        self.pause_mouse_intent = None
        self.view = ""
        self.snapshot = None
        self.live_edit_index = 0
        self.live_typed = 0
        self.recent_edits = []
        self._styles()
        self._shell()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.report_callback_exception = self._callback_error
        self.show_setup()
        self.after(100, self.tick)

    def _styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10), foreground=INK, background=BG)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=WHITE)
        style.configure("TLabel", background=BG)
        style.configure("Card.TLabel", background=WHITE)
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Hint.TLabel", foreground=MUTED, background=WHITE, font=("Segoe UI", 9))
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 23))
        style.configure("Section.TLabel", font=("Segoe UI Semibold", 13), background=WHITE)
        style.configure("Metric.TLabel", font=("Segoe UI Semibold", 25), foreground=TEAL, background=WHITE)
        style.configure("TButton", padding=(15, 9), background=WHITE, borderwidth=0)
        style.map("TButton", background=[("active", "#e0eeee")])
        style.configure("Primary.TButton", background=TEAL, foreground=WHITE, font=("Segoe UI Semibold", 10))
        style.map("Primary.TButton", background=[("active", "#066861"), ("disabled", "#b0c5c5")], foreground=[("disabled", WHITE)])
        style.configure("TEntry", padding=7, fieldbackground=WHITE, bordercolor=LINE)
        style.configure("TCombobox", padding=6, fieldbackground=WHITE, arrowsize=14)
        style.map("TCombobox", fieldbackground=[("readonly", WHITE)])
        style.configure("TRadiobutton", background=WHITE, padding=(0, 4))
        style.configure("Treeview", background=WHITE, fieldbackground=WHITE, rowheight=34, borderwidth=0)
        style.configure("Treeview.Heading", background="#e7eff2", padding=(9, 8), font=("Segoe UI Semibold", 10))
        style.map("Treeview", background=[("selected", "#cce7e4")], foreground=[("selected", INK)])

    def _shell(self):
        top = ttk.Frame(self, padding=(26, 15))
        top.pack(fill="x")
        ttk.Label(top, text="TEXTLAB", foreground=TEAL, font=("Segoe UI", 19, "bold")).pack(side="left")
        ttk.Label(top, text="  /  Лаборатория текстового ввода", style="Muted.TLabel").pack(side="left", padx=(6, 20))
        self.nav = []
        for text, command in [("Новая сессия", self.show_setup), ("Архив сессий", self.show_archive), ("Методика", self.show_methods)]:
            button = ttk.Button(top, text=text, command=command)
            button.pack(side="left", padx=3)
            self.nav.append(button)
        self.footer = tk.StringVar(value="Локальное хранение · события записываются только в редакторе")
        ttk.Label(self, textvariable=self.footer, style="Muted.TLabel", padding=(26, 4, 20, 12)).pack(side="bottom", fill="x")
        self.body = ttk.Frame(self, padding=(26, 7, 26, 14))
        self.body.pack(fill="both", expand=True)

    def clear(self, view):
        if self.rec and self.rec.status != "completed":
            return False
        self.view = view
        for child in self.body.winfo_children():
            child.destroy()
        return True

    def heading(self, title, subtitle):
        frame = ttk.Frame(self.body)
        frame.pack(fill="x", pady=(2, 17))
        ttk.Label(frame, text=title, style="Title.TLabel").pack(anchor="w")
        ttk.Label(frame, text=subtitle, style="Muted.TLabel").pack(anchor="w", pady=(5, 0))
        return frame

    def card(self, parent, title):
        frame = ttk.Frame(parent, style="Card.TFrame", padding=20)
        ttk.Label(frame, text=title, style="Section.TLabel").pack(anchor="w", pady=(0, 14))
        return frame

    def field(self, parent, label, var, row, column=0, choices=None, width=23):
        outer = ttk.Frame(parent, style="Card.TFrame")
        outer.grid(row=row, column=column, sticky="ew", padx=(0, 15 if column == 0 else 0), pady=(0, 11))
        ttk.Label(outer, text=label, style="Card.TLabel").pack(anchor="w", pady=(0, 5))
        if choices:
            widget = ttk.Combobox(outer, textvariable=var, values=choices, state="readonly", width=width)
        else:
            widget = ttk.Entry(outer, textvariable=var, width=width)
        widget.pack(fill="x")
        return widget

    def textbox(self, parent, height=3):
        return tk.Text(parent, height=height, wrap="word", font=("Segoe UI", 10), bg=WHITE, fg=INK,
                       relief="solid", bd=1, highlightthickness=0, padx=9, pady=7, undo=True)

    def show_setup(self):
        if not self.clear("setup"):
            return
        self.rec = None
        header = self.heading("Новый исследовательский лист", "Заполните профиль участника и выберите протокол. Запись начнется после нажатия кнопки.")
        saved = self.store.protocols()
        if saved:
            tools = ttk.Frame(header)
            tools.pack(anchor="w", pady=(12, 0))
            ttk.Label(tools, text="Сохраненный протокол", style="Muted.TLabel").pack(side="left", padx=(0, 10))
            choice = ttk.Combobox(tools, values=list(saved), state="readonly", width=36)
            choice.pack(side="left")
            choice.bind("<<ComboboxSelected>>", lambda e: self.load_protocol(saved[choice.get()]))
        actions = ttk.Frame(self.body)
        actions.pack(side="bottom", fill="x", pady=(14, 0))
        ttk.Button(actions, text="Начать сессию  →", style="Primary.TButton", command=self.start_session).pack(side="right")
        ttk.Button(actions, text="Сохранить протокол", command=self.save_protocol).pack(side="right", padx=12)
        ttk.Label(actions, text="0 в ограничениях — без лимита", style="Muted.TLabel").pack(side="left")
        scroll = ScrollFrame(self.body)
        scroll.pack(fill="both", expand=True)
        grid = scroll.inner
        grid.columnconfigure(0, weight=1, uniform="cards")
        grid.columnconfigure(1, weight=1, uniform="cards")
        profile_card = self.card(grid, "01  Профиль участника")
        profile_card.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        config_card = self.card(grid, "02  Протокол сессии")
        config_card.grid(row=0, column=1, sticky="nsew")
        hour = datetime.now().hour
        period = "Утро" if 6 <= hour < 12 else "День" if 12 <= hour < 18 else "Вечер" if 18 <= hour < 24 else "Ночь"
        defaults = {"session_label": "", "age": "", "gender": "Не указан", "handedness": "Правша",
                    "touch_typing": "Нет", "fatigue": "0", "stress": "0", "time_of_day": period,
                    "caffeine": "Не указано", "sedatives": "Не указано"}
        self.pvars = {key: tk.StringVar(value=value) for key, value in defaults.items()}
        fields = ttk.Frame(profile_card, style="Card.TFrame")
        fields.pack(fill="x")
        fields.columnconfigure(0, weight=1)
        fields.columnconfigure(1, weight=1)
        self.field(fields, "ID сессии (пусто = автоматически)", self.pvars["session_label"], 0, width=20)
        self.field(fields, "Возраст, полных лет", self.pvars["age"], 0, 1, width=18)
        self.field(fields, "Пол", self.pvars["gender"], 1, choices=["Не указан", "Женский", "Мужской", "Другое"])
        self.field(fields, "Ведущая рука", self.pvars["handedness"], 1, 1, ["Правша", "Левша", "Амбидекстр"])
        self.field(fields, "Навык слепой печати", self.pvars["touch_typing"], 2, choices=["Нет", "Да", "Частично"])
        self.field(fields, "Время суток", self.pvars["time_of_day"], 2, 1, ["Утро", "День", "Вечер", "Ночь"])
        self.field(fields, "Усталость: 0 нет · 10 максимум", self.pvars["fatigue"], 3, choices=list(range(11)))
        self.field(fields, "Стресс: 0 нет · 10 максимум", self.pvars["stress"], 3, 1, list(range(11)))
        self.field(fields, "Употребление кофеина", self.pvars["caffeine"], 4, choices=["Не указано", "Нет", "Да"])
        self.field(fields, "Седативные вещества", self.pvars["sedatives"], 4, 1, ["Не указано", "Нет", "Да"])
        ttk.Label(profile_card, text="Примечания исследователя", style="Card.TLabel").pack(anchor="w", pady=(3, 6))
        self.notes = self.textbox(profile_card, 6)
        self.notes.pack(fill="x")
        ttk.Label(profile_card, text="Условия, помехи, оборудование; при необходимости —\nвремя и количество употребленных веществ.", style="Hint.TLabel").pack(anchor="w", pady=(9, 0))
        self.cvars = {key: tk.StringVar(value=value) for key, value in {
            "title": "Исследование текстового ввода", "mode": "continuous", "time_limit_minutes": "0",
            "target_characters": "0", "target_words": "0", "micro_pause_ms": "300", "macro_pause_ms": "2000"}.items()}
        ttk.Label(config_card, text="Название листа", style="Card.TLabel").pack(anchor="w")
        ttk.Entry(config_card, textvariable=self.cvars["title"]).pack(fill="x", pady=(5, 10))
        ttk.Label(config_card, text="Описание и инструкция участнику", style="Card.TLabel").pack(anchor="w")
        self.description = self.textbox(config_card, 2)
        self.description.pack(fill="x", pady=(5, 9))
        modes = ttk.Frame(config_card, style="Card.TFrame")
        modes.pack(fill="x")
        for label, value in [("Сплошной текст", "continuous"), ("По темам / карточкам", "topics")]:
            ttk.Radiobutton(modes, text=label, variable=self.cvars["mode"], value=value, command=self.update_topic_fields).pack(side="left", padx=(0, 14))
        self.topic_frame = ttk.Frame(config_card, style="Card.TFrame")
        self.topic_frame.pack(fill="x", pady=(7, 0))
        ttk.Label(self.topic_frame, text="Темы из каталога (можно выбрать несколько)", style="Card.TLabel").pack(anchor="w")
        self.topic_list = tk.Listbox(self.topic_frame, selectmode="multiple", exportselection=False, height=4,
                                    font=("Segoe UI", 10), relief="solid", bd=1, bg=WHITE, fg=INK,
                                    selectbackground="#cce7e4", selectforeground=INK, activestyle="none")
        for topic in PRESET_TOPICS:
            self.topic_list.insert("end", topic)
        self.topic_list.pack(fill="x", pady=(5, 8))
        ttk.Label(self.topic_frame, text="Свои темы — по одной на строку", style="Card.TLabel").pack(anchor="w")
        self.custom_topics = self.textbox(self.topic_frame, 2)
        self.custom_topics.pack(fill="x", pady=(5, 9))
        limits = self.limit_fields = ttk.Frame(config_card, style="Card.TFrame")
        limits.pack(fill="x", pady=(10, 0))
        for i, (label, key) in enumerate([("Время, мин", "time_limit_minutes"), ("Символы", "target_characters"), ("Слова", "target_words")]):
            limits.columnconfigure(i, weight=1)
            self.field(limits, label, self.cvars[key], 0, i, width=9)
        pauses = ttk.Frame(config_card, style="Card.TFrame")
        pauses.pack(fill="x")
        pauses.columnconfigure(0, weight=1)
        pauses.columnconfigure(1, weight=1)
        self.field(pauses, "Микропауза от, мс", self.cvars["micro_pause_ms"], 0, width=15)
        self.field(pauses, "Макропауза от, мс", self.cvars["macro_pause_ms"], 0, 1, width=15)
        ttk.Label(config_card, text="Лимиты действуют на весь лист. При нескольких\nлимитах сессия завершается по первому достигнутому.", style="Hint.TLabel").pack(anchor="w")
        self.update_topic_fields()
        self.after_idle(lambda: scroll.canvas.yview_moveto(0) if scroll.winfo_exists() else None)
        self.footer.set(f"Данные: {self.data_dir}  ·  Работа без подключения к интернету")

    def update_topic_fields(self):
        enabled = self.cvars["mode"].get() == "topics"
        self.topic_list.configure(state="normal" if enabled else "disabled")
        self.custom_topics.configure(state="normal" if enabled else "disabled")
        if enabled:
            self.topic_frame.pack(fill="x", pady=(7, 0), before=self.limit_fields)
        else:
            self.topic_frame.pack_forget()

    def settings(self):
        try:
            topics = ([self.topic_list.get(i) for i in self.topic_list.curselection()] +
                      [s.strip() for s in self.custom_topics.get("1.0", "end").splitlines() if s.strip()]) if self.cvars["mode"].get() == "topics" else ["Свободный текст"]
            settings = {
                "title": self.cvars["title"].get().strip(), "description": self.description.get("1.0", "end").strip(),
                "mode": self.cvars["mode"].get(), "topics": list(dict.fromkeys(topics)),
                "time_limit_s": float(self.cvars["time_limit_minutes"].get().replace(",", ".")) * 60,
                "target_characters": int(self.cvars["target_characters"].get()),
                "target_words": int(self.cvars["target_words"].get()),
                "micro_pause_ms": float(self.cvars["micro_pause_ms"].get().replace(",", ".")),
                "macro_pause_ms": float(self.cvars["macro_pause_ms"].get().replace(",", ".")),
            }
        except ValueError:
            raise ValueError("Проверьте числа в ограничениях и порогах пауз. Символы и слова — целые числа.") from None
        validate_settings(settings)
        return settings

    def save_protocol(self):
        try:
            settings = self.settings()
            existing = self.store.protocols()
            if settings["title"] in existing and not messagebox.askyesno("Заменить протокол", "Сохранить новые настройки протокола с этим названием?", parent=self):
                return
            self.store.save_protocol(settings)
            self.footer.set("Протокол сохранен. Он будет доступен при создании следующей сессии.")
        except (ValueError, OSError) as exc:
            messagebox.showerror("Не удалось сохранить", str(exc), parent=self)

    def load_protocol(self, settings):
        for key in self.cvars:
            value = settings.get(key)
            if key == "time_limit_minutes":
                value = settings["time_limit_s"] / 60
            if value is not None:
                self.cvars[key].set(str(value))
        self.description.delete("1.0", "end")
        self.description.insert("1.0", settings.get("description", ""))
        self.topic_list.configure(state="normal")
        self.topic_list.selection_clear(0, "end")
        for index, topic in enumerate(PRESET_TOPICS):
            if topic in settings["topics"]:
                self.topic_list.selection_set(index)
        self.custom_topics.configure(state="normal")
        self.custom_topics.delete("1.0", "end")
        self.custom_topics.insert("1.0", "\n".join(t for t in settings["topics"] if t not in PRESET_TOPICS and t != "Свободный текст"))
        self.update_topic_fields()

    def start_session(self):
        try:
            profile = {key: var.get().strip() for key, var in self.pvars.items()}
            try:
                profile["age"] = int(profile["age"])
            except ValueError:
                raise ValueError("Укажите возраст участника целым числом от 1 до 120.") from None
            if not 1 <= profile["age"] <= 120:
                raise ValueError("Возраст должен быть от 1 до 120 лет.")
            profile["fatigue"] = int(profile["fatigue"])
            profile["stress"] = int(profile["stress"])
            profile["notes"] = self.notes.get("1.0", "end").strip()
            if profile["session_label"] and self.store.has_label(profile["session_label"]):
                raise ValueError("Такой ID уже есть в архиве. Задайте другой или оставьте поле пустым.")
            settings = self.settings()
        except ValueError as exc:
            messagebox.showerror("Проверьте параметры", str(exc), parent=self)
            return
        self.clear("editor")
        self.rec = Recorder(profile, settings)
        self.persisted = 0
        self.live_edit_index = 0
        self.live_typed = 0
        self.recent_edits = []
        self.last_selection = None
        for button in self.nav:
            button.configure(state="disabled")
        self.show_editor()
        self.rec.resume()
        self.save()
        self.editor.focus_set()

    def show_editor(self):
        rec = self.rec
        top = ttk.Frame(self.body)
        top.pack(fill="x", pady=(3, 15))
        ttk.Label(top, text=rec.settings["title"], style="Title.TLabel", wraplength=700).pack(side="left")
        ttk.Button(top, text="Завершить", command=self.finish).pack(side="right")
        self.pause_button = ttk.Button(top, text="Приостановить", command=self.toggle_pause, style="Primary.TButton")
        self.pause_button.bind("<ButtonPress-1>", self.capture_pause_intent)
        self.pause_button.bind("<ButtonRelease-1>", lambda e: self.after_idle(lambda: setattr(self, "pause_mouse_intent", None)), add=True)
        self.pause_button.pack(side="right", padx=10)
        shell = ttk.Frame(self.body)
        shell.pack(fill="both", expand=True)
        sidebar = self.card(shell, "Протокол")
        sidebar.pack(side="left", fill="y", padx=(0, 17))
        sidebar.configure(width=265)
        sidebar.pack_propagate(False)
        ttk.Label(sidebar, text="УЧАСТНИК / СЕССИЯ", style="Hint.TLabel").pack(anchor="w")
        ttk.Label(sidebar, text=rec.profile["session_label"], style="Card.TLabel", wraplength=220).pack(anchor="w", pady=(5, 22))
        ttk.Label(sidebar, text="ТЕМЫ", style="Hint.TLabel").pack(anchor="w", pady=(0, 8))
        self.topic_buttons = []
        topic_scroll = tk.Canvas(sidebar, bg=WHITE, highlightthickness=0, width=220, height=240)
        topic_scroll.pack(fill="x")
        topic_content = ttk.Frame(topic_scroll, style="Card.TFrame")
        topic_scroll.create_window(0, 0, window=topic_content, anchor="nw", width=220)
        topic_content.bind("<Configure>", lambda e: topic_scroll.configure(scrollregion=topic_scroll.bbox("all")))
        topic_scroll.bind("<MouseWheel>", lambda e: topic_scroll.yview_scroll(-int(e.delta / 120), "units"))
        for index, topic in enumerate(rec.settings["topics"]):
            b = tk.Button(topic_content, text=f"{index+1:02}  {topic}", command=lambda i=index: self.switch_topic(i),
                          anchor="w", justify="left", wraplength=185, relief="flat", bd=0,
                          bg="#e0f1ef" if index == rec.topic else WHITE, fg=TEAL if index == rec.topic else INK,
                          font=("Segoe UI", 10), padx=10, pady=12)
            b.pack(fill="x", pady=(0, 3))
            b.bind("<MouseWheel>", lambda e: topic_scroll.yview_scroll(-int(e.delta / 120), "units"))
            self.topic_buttons.append(b)
        self.limit_label = ttk.Label(sidebar, text="", style="Hint.TLabel", wraplength=215)
        self.limit_label.pack(anchor="w", pady=(20, 10))
        ttk.Label(sidebar, text="Потеря фокуса приостанавливает запись. Для продолжения нажмите «Продолжить».", style="Hint.TLabel", wraplength=215).pack(side="bottom", anchor="w")
        main = ttk.Frame(shell, style="Card.TFrame", padding=24)
        main.pack(side="left", fill="both", expand=True)
        self.topic_title = ttk.Label(main, text=rec.settings["topics"][rec.topic], style="Section.TLabel", wraplength=760)
        self.topic_title.pack(anchor="w")
        if rec.settings["description"]:
            ttk.Label(main, text=rec.settings["description"], style="Hint.TLabel", wraplength=750).pack(anchor="w", pady=(8, 0))
        line = ttk.Frame(main, style="Card.TFrame")
        line.pack(fill="x", pady=(18, 12))
        self.record_status = ttk.Label(line, text="●  Идет запись", foreground=TEAL, style="Card.TLabel")
        self.record_status.pack(side="left")
        self.timer_label = ttk.Label(line, text="00:00", font=("Consolas", 15), style="Card.TLabel")
        self.timer_label.pack(side="right")
        edit_frame = ttk.Frame(main, style="Card.TFrame")
        edit_frame.pack(fill="both", expand=True)
        self.editor = ObservedText(edit_frame, self.on_edit, wrap="word", undo=True, autoseparators=True,
                                   width=1, height=8,
                                   font=("Segoe UI", 17), bg="#fcfdfd", fg=INK, insertbackground=TEAL,
                                   selectbackground="#cce7e4", relief="solid", bd=1, highlightthickness=0,
                                   padx=20, pady=18, spacing1=3, spacing3=7)
        bar = ttk.Scrollbar(edit_frame, command=self.editor.yview)
        self.editor.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.editor.pack(fill="both", expand=True)
        self.editor.bind("<KeyPress>", self.on_key_down)
        self.editor.bind("<KeyRelease>", self.on_key_up)
        self.editor.bind("<FocusOut>", self.on_focus_out)
        self.editor.bind("<ButtonRelease-1>", self.on_selection)
        for virtual, source in [("<<Paste>>", "paste"), ("<<Cut>>", "cut"), ("<<Undo>>", "undo"), ("<<Redo>>", "redo")]:
            self.editor.bind(virtual, lambda e, s=source: self.operation(s), add=True)
        self.editor.bind("<Control-y>", self.redo)
        self.editor.bind("<Tab>", lambda e: self.insert_tab(e))
        self.live_label = ttk.Label(main, text="0 символов  ·  0 слов", style="Hint.TLabel")
        self.live_label.pack(anchor="w", pady=(15, 0))
        self.footer.set("Запись событий только в этом поле · автоматическое сохранение каждую секунду")

    def operation(self, source):
        self.pending_source = source
        if self.rec and self.rec.running:
            self.rec.event("operation", source=source)
        self.after_idle(lambda: setattr(self, "pending_source", "keyboard"))

    def redo(self, event):
        self.on_key_down(event)
        self.operation("redo")
        try:
            self.editor.edit_redo()
        except tk.TclError:
            pass
        return "break"

    def insert_tab(self, event):
        if self.on_key_down(event) == "break":
            return "break"
        self.editor.insert("insert", "\t")
        return "break"

    def on_edit(self, after, cursor, selection, stamp, command):
        if self.rec and self.rec.running:
            source = self.pending_source
            if command[0] == "edit":
                source = command[1]
            self.rec.edit(after, source, cursor, selection, stamp - self.rec.origin)
            self.pending_source = "keyboard"
            reason = self.rec.limit_reached()
            if reason:
                # Stop before another input event; render report after Tk's edit completes.
                self.rec.finish(reason)
                self.editor.configure(state="disabled")
                self.after_idle(self.finish_report)

    def on_key_down(self, event):
        stamp = time.perf_counter_ns()
        if not self.rec or not self.rec.running:
            return "break"
        if self.rec.limit_reached():
            self.finish(self.rec.limit_reached())
            return "break"
        self.rec.key_down(event.keycode, event.keysym, event.char,
                          len(self.editor.get("1.0", "insert")), self.editor.selection_offsets(),
                          event.state, event.time, stamp - self.rec.origin)

    def on_key_up(self, event):
        stamp = time.perf_counter_ns()
        if self.rec and self.rec.running:
            self.rec.key_up(event.keycode, event.keysym, event.time, stamp - self.rec.origin)
            self.on_selection()

    def on_selection(self, event=None):
        if self.rec and self.rec.running:
            selection = self.editor.selection_offsets()
            if selection != self.last_selection:
                self.rec.event("selection", selection=selection, cursor=len(self.editor.get("1.0", "insert")))
                self.last_selection = selection
            if event:
                self.rec.event("cursor", cursor=len(self.editor.get("1.0", "insert")), selection=selection)

    def on_focus_out(self, event):
        # Timestamp before UI work; no global keyboard hook is installed.
        if self.rec and self.rec.running:
            self.rec.pause("focus_lost")
            self.update_recording_ui()
            self.save()

    def capture_pause_intent(self, event=None):
        # Button focus can pause the editor before ttk invokes its command.
        self.pause_mouse_intent = self.rec.running

    def toggle_pause(self):
        should_pause = self.rec.running if self.pause_mouse_intent is None else self.pause_mouse_intent
        self.pause_mouse_intent = None
        if should_pause:
            self.rec.pause()
        else:
            self.rec.resume()
            self.editor.configure(state="normal")
            self.editor.focus_set()
        self.update_recording_ui()
        self.save()

    def update_recording_ui(self):
        running = self.rec.running
        self.editor.configure(state="normal" if running else "disabled")
        self.record_status.configure(text="●  Идет запись" if running else "Ⅱ  Пауза — запись остановлена", foreground=TEAL if running else "#986c2e")
        self.pause_button.configure(text="Приостановить" if running else "Продолжить")

    def switch_topic(self, index):
        if self.rec.status == "completed" or index == self.rec.topic:
            return
        self.rec.switch_topic(index)
        self.editor.replace_all(self.rec.texts[index])
        self.last_selection = None
        self.topic_title.configure(text=self.rec.settings["topics"][index])
        for i, button in enumerate(self.topic_buttons):
            button.configure(bg="#e0f1ef" if i == index else WHITE, fg=TEAL if i == index else INK)
        self.rec.resume()
        self.editor.focus_set()
        self.update_recording_ui()
        self.save()

    def save(self):
        if self.rec:
            events = self.rec.events[self.persisted:]
            self.store.enqueue(self.rec.snapshot(False), events)
            self.persisted = len(self.rec.events)
            self.last_save = time.monotonic()

    def tick(self):
        try:
            if self.rec and self.view == "editor" and self.rec.status != "completed":
                elapsed = self.rec.active_ns() / 1e9
                seconds = int(elapsed)
                self.timer_label.configure(text=f"{seconds//60:02}:{seconds%60:02}")
                total = sum(map(len, self.rec.texts))
                count = sum(len(words(t)) for t in self.rec.texts)
                for event in self.rec.events[self.live_edit_index:]:
                    if event["type"] == "edit" and event["source"] == "keyboard":
                        length = len(event["inserted"])
                        self.live_typed += length
                        self.recent_edits.append((event["active_ns"] / 1e9, length))
                self.live_edit_index = len(self.rec.events)
                self.recent_edits = [(t, n) for t, n in self.recent_edits if elapsed - t <= 10]
                current = sum(n for _, n in self.recent_edits) * 60 / min(elapsed, 10) if elapsed else 0
                average = self.live_typed * 60 / elapsed if elapsed else 0
                self.live_label.configure(text=f"{total} символов  ·  {count} слов  ·  Сейчас {current:.0f} зн/мин ({current/5:.0f} сл/мин)  ·  Средняя {average:.0f} зн/мин ({average/5:.0f} сл/мин)")
                settings = self.rec.settings
                limits = []
                if settings["time_limit_s"]:
                    limits.append(f"Осталось {max(0, settings['time_limit_s']-elapsed):.0f} с")
                if settings["target_characters"]:
                    limits.append(f"Символы: {total} / {settings['target_characters']}")
                if settings["target_words"]:
                    limits.append(f"Слова: {count} / {settings['target_words']}")
                self.limit_label.configure(text="\n".join(limits) or "Лист без ограничений")
                if self.rec.running and self.rec.limit_reached():
                    self.finish(self.rec.limit_reached())
                elif time.monotonic() - self.last_save >= 1:
                    self.save()
            if not self.store.errors.empty():
                error = self.store.errors.get_nowait()
                if self.rec and self.rec.running:
                    self.rec.pause("storage_error")
                    self.update_recording_ui()
                self.persisted = 0  # Retry the full immutable stream after a failed transaction.
                messagebox.showerror("Ошибка автосохранения", f"Данные пока остаются в памяти. Запись приостановлена.\n{error}\nМожно завершить сессию и экспортировать JSON.", parent=self)
        finally:
            self.after(200, self.tick)

    def finish(self, reason="manual"):
        if not self.rec or self.rec.status == "completed":
            return
        if reason == "manual":
            self.rec.pause("finish_dialog")
            self.update_recording_ui()
            if not messagebox.askyesno("Завершить сессию", "Завершить запись и открыть отчет?\nВсе введенные тексты и события будут сохранены.", parent=self):
                return
        self.rec.finish(reason)
        self.finish_report()

    def finish_report(self):
        self.save()
        snapshot = self.rec.snapshot()
        try:
            self.store.flush()
        except OSError as exc:
            messagebox.showerror("Сохранение не выполнено", f"{exc}\nЭкспортируйте JSON из отчета, чтобы сохранить данные.", parent=self)
        for button in self.nav:
            button.configure(state="normal")
        self.show_report(snapshot)

    def show_report(self, snapshot):
        if not self.clear("report"):
            return
        self.snapshot = snapshot
        self.report = full_report(snapshot)
        title = "Отчет по сессии" if snapshot["status"] == "completed" else "Отчет по прерванной сессии"
        header = self.heading(title, f"{snapshot['settings']['title']}  ·  {snapshot['profile']['session_label']}  ·  {snapshot['started_at'][:19].replace('T',' ')}")
        tools = ttk.Frame(header)
        tools.pack(fill="x", pady=(13, 0))
        self.report_topic = ttk.Combobox(tools, state="readonly", width=48,
                                         values=["Весь лист"] + [f"{i+1}. {t}" for i, t in enumerate(snapshot["settings"]["topics"])])
        self.report_topic.current(0)
        self.report_topic.pack(side="left")
        self.report_topic.bind("<<ComboboxSelected>>", lambda e: self.report_content())
        for label, kind in [("Отчет HTML", "html"), ("События CSV", "csv"), ("Полные данные JSON", "json")]:
            ttk.Button(tools, text=label, command=lambda k=kind: self.export(k), style="Primary.TButton" if kind == "html" else "TButton").pack(side="right", padx=(8, 0))
        self.report_scroll = ScrollFrame(self.body)
        self.report_scroll.pack(fill="both", expand=True)
        self.report_content()
        self.footer.set("HTML открывается в браузере и печатается в PDF · JSON содержит профиль, тексты, события и расчеты")

    def report_content(self):
        parent = self.report_scroll.inner
        for child in parent.winfo_children():
            child.destroy()
        index = self.report_topic.current() - 1
        m = self.report["overall"] if index < 0 else self.report["topics"][index]
        tiles = ttk.Frame(parent)
        tiles.pack(fill="x", pady=(0, 15))
        for i, (value, label, hint) in enumerate([
            (fmt(m["cpm"], 0), "Знаков в минуту", f'{fmt(m["wpm"])} условных слов/мин'),
            (fmt(m["pause_seconds"]), "Секунд пауз", f'{m["micro_pause_count"]} микро · {m["macro_pause_count"]} макро'),
            (fmt(m["correction_percent"]) + "%", "Исправления", f'{m["correction_cycles"]} циклов исправлений'),
            (fmt(m["cognitive_delay_index"]) + "%", "Индекс задержек", "Макропаузы / активное время"),
        ]):
            tiles.columnconfigure(i, weight=1, uniform="metrics")
            card = ttk.Frame(tiles, style="Card.TFrame", padding=17)
            card.grid(row=0, column=i, sticky="nsew", padx=(0, 12 if i < 3 else 0))
            ttk.Label(card, text=label, style="Card.TLabel").pack(anchor="w")
            ttk.Label(card, text=value, style="Metric.TLabel").pack(anchor="w", pady=5)
            ttk.Label(card, text=hint, style="Hint.TLabel").pack(anchor="w")
        charts = ttk.Frame(parent)
        charts.pack(fill="x", pady=(0, 15))
        charts.columnconfigure(0, weight=1, uniform="charts")
        charts.columnconfigure(1, weight=1, uniform="charts")
        for col, title, kind in [(0, "Скорость ввода · зн/мин", "speed"), (1, "Распределение пауз · количество", "hist")]:
            card = self.card(charts, title)
            card.grid(row=0, column=col, sticky="nsew", padx=(0, 12 if col == 0 else 0))
            canvas = tk.Canvas(card, bg=WHITE, height=205, highlightthickness=0)
            canvas.pack(fill="x")
            canvas.bind("<Configure>", lambda e, c=canvas, k=kind: self.draw_chart(c, k, m))
        details = self.card(parent, "Параметры и качество данных")
        details.pack(fill="x", pady=(0, 15))
        rows = [
            ("Активное время", f'{fmt(m["active_seconds"])} с', "Символы / слова / предложения", f'{m["characters"]} / {m["words"]} / {m["sentences"]}'),
            ("Hold Time: среднее / медиана", f'{fmt(m["hold"]["mean_ms"])} / {fmt(m["hold"]["median_ms"])} мс', "Flight Time: среднее / медиана", f'{fmt(m["flight"]["mean_ms"])} / {fmt(m["flight"]["median_ms"])} мс'),
            ("TTR · уникальные слова / все", fmt(m["ttr"], 3), "Средняя длина слова / предложения", f'{fmt(m["mean_word_length"])} симв. / {fmt(m["mean_sentence_length"])} слов'),
            ("Backspace / Delete", f'{m["backspace_count"]} / {m["delete_count"]}', "Удаления выделений / операций", f'{m["selection_deletions"]} / {m["correction_operations"]}'),
            ("Набрано / вставлено из буфера", f'{m["typed_characters"]} / {m["pasted_characters"]}', "Полные / незавершенные пары", f'{m["hold"]["count"]} / {m["unpaired_key_downs"]}'),
        ]
        table = ttk.Frame(details, style="Card.TFrame")
        table.pack(fill="x")
        for row, values in enumerate(rows):
            for col, value in enumerate(values):
                table.columnconfigure(col, weight=1)
                ttk.Label(table, text=value, style="Hint.TLabel" if col % 2 == 0 else "Card.TLabel").grid(row=row, column=col, sticky="w", padx=(0, 16), pady=7)
        pos = m["pause_positions"]
        ttk.Label(details, text=f"Паузы: внутри слова {pos.get('within_word',0)} · между словами {pos.get('between_words',0)} · перед пунктуацией {pos.get('before_punctuation',0)} · прочие {pos.get('other',0)}", style="Hint.TLabel").pack(anchor="w", pady=(14, 0))
        compare = self.card(parent, "Сравнение тем")
        compare.pack(fill="x", pady=(0, 15))
        tree = ttk.Treeview(compare, columns=("topic", "time", "chars", "cpm", "pause", "ttr"), show="headings", height=min(6, len(self.report["topics"])))
        for key, label, width in [("topic", "Тема", 340), ("time", "Время, с", 95), ("chars", "Символы", 95), ("cpm", "Зн/мин", 95), ("pause", "Паузы, с", 95), ("ttr", "TTR", 80)]:
            tree.heading(key, text=label)
            tree.column(key, width=width, anchor="w" if key == "topic" else "center", stretch=key == "topic")
        for t in self.report["topics"]:
            tree.insert("", "end", values=(t["title"], fmt(t["active_seconds"]), t["characters"], fmt(t["cpm"]), fmt(t["pause_seconds"]), fmt(t["ttr"],3)))
        tree.pack(fill="x")
        text_card = self.card(parent, "Итоговый текст")
        text_card.pack(fill="x", pady=(0, 15))
        text = self.textbox(text_card, 8)
        display = "\n\n".join(f"{title}\n{content}" for title, content in zip(self.snapshot["settings"]["topics"], self.snapshot["texts"])) if index < 0 else self.snapshot["texts"][index]
        text.insert("1.0", display)
        text.configure(state="disabled")
        text.pack(fill="x")
        ttk.Label(parent, text="Индекс задержек описывает структуру пауз. Интерпретация состояния участника требует отдельной исследовательской методики.", style="Muted.TLabel", wraplength=1100).pack(anchor="w", pady=8)
        self.report_scroll._tag_children()

    def draw_chart(self, canvas, kind, metrics):
        canvas.delete("all")
        width = canvas.winfo_width()
        left, right, top, base = 48, width - 15, 20, 165
        font = ("Segoe UI", 8)
        if kind == "speed":
            series = metrics["speed_series"]
            maximum = max(10, max(p["cpm"] for p in series) * 1.1)
            duration = max(1, series[-1]["end_s"])
            for i in range(4):
                y = base - (base - top) * i / 3
                canvas.create_line(left, y, right, y, fill=LINE)
                canvas.create_text(left-8, y, text=f"{maximum*i/3:.0f}", anchor="e", fill=MUTED, font=font)
            points = [(left+(right-left)*(p["start_s"]+p["end_s"])/2/duration, base-(base-top)*p["cpm"]/maximum) for p in series]
            if len(points) > 1:
                canvas.create_line(*[v for p in points for v in p], fill=TEAL, width=2)
            for x, y in points if len(points) < 30 else []:
                canvas.create_oval(x-2, y-2, x+2, y+2, fill=TEAL, outline=TEAL)
            canvas.create_text(left, base+24, text="0 с", anchor="w", fill=MUTED, font=font)
            canvas.create_text(right, base+24, text=f"{duration:.1f} с активного времени", anchor="e", fill=MUTED, font=font)
        else:
            bins = metrics["pause_histogram"]
            maximum = max(1, max(b["count"] for b in bins))
            step = (right-left) / len(bins)
            for i, item in enumerate(bins):
                x = left + i * step + 8
                y = base-(base-top)*item["count"]/maximum
                canvas.create_rectangle(x, y, x+step-16, base, fill=TEAL, outline="")
                canvas.create_text(x+(step-16)/2, y-10, text=item["count"], fill=INK, font=font)
                upper = f"{item['to_ms']/1000:g}" if item["to_ms"] else "∞"
                canvas.create_text(x+(step-16)/2, base+24, text=f"{item['from_ms']/1000:g}–{upper} с", fill=MUTED, font=font)

    def export(self, kind):
        safe_name = re.sub(r"[^\w-]+", "_", self.snapshot["profile"]["session_label"])[:70] or "session"
        path = filedialog.asksaveasfilename(parent=self, title="Экспорт сессии", initialdir=str(self.data_dir),
                                           initialfile=f"textlab_{safe_name}.{kind}", defaultextension=f".{kind}",
                                           filetypes=[(kind.upper(), f"*.{kind}")])
        if not path:
            return
        try:
            {"html": export_html, "json": export_json, "csv": export_csv}[kind](self.snapshot, Path(path))
            self.footer.set(f"Экспорт сохранен: {path}")
        except OSError as exc:
            messagebox.showerror("Не удалось экспортировать", str(exc), parent=self)

    def show_archive(self):
        if not self.clear("archive"):
            return
        self.heading("Архив сессий", "Сохраненные сессии и доступные данные после незапланированного закрытия программы.")
        bar = ttk.Frame(self.body)
        bar.pack(fill="x", pady=(0, 15))
        search = tk.StringVar()
        ttk.Label(bar, text="Поиск по ID или названию").pack(side="left", padx=(0, 12))
        ttk.Entry(bar, textvariable=search, width=40).pack(side="left")
        ttk.Button(bar, text="Открыть отчет", style="Primary.TButton", command=lambda: open_selected()).pack(side="right")
        columns = ("label", "title", "date", "status", "chars")
        tree = ttk.Treeview(self.body, columns=columns, show="headings", selectmode="browse")
        for key, label, width in [("label", "ID сессии", 220), ("title", "Лист", 300), ("date", "Начало", 180), ("status", "Статус", 120), ("chars", "Символы", 90)]:
            tree.heading(key, text=label)
            tree.column(key, width=width, stretch=key in {"label", "title"})
        scrollbar = ttk.Scrollbar(self.body, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        sessions = self.store.list_sessions()
        def populate(*_):
            tree.delete(*tree.get_children())
            term = search.get().casefold()
            for s in sessions:
                if term not in (s["profile"]["session_label"] + " " + s["settings"]["title"]).casefold():
                    continue
                tree.insert("", "end", iid=s["session_id"], values=(s["profile"]["session_label"], s["settings"]["title"], s["started_at"][:19].replace("T", " "), "Завершена" if s["status"] == "completed" else "Прервана", sum(map(len, s["texts"]))))
        def open_selected():
            if tree.selection():
                self.show_report(self.store.load(tree.selection()[0]))
        search.trace_add("write", populate)
        tree.bind("<Double-1>", lambda e: open_selected())
        tree.bind("<Return>", lambda e: open_selected())
        populate()
        self.footer.set(f"Сессий в архиве: {len(sessions)}  ·  {self.store.path}")

    def show_methods(self):
        if not self.clear("methods"):
            return
        self.heading("Методика и регистрация", "Определения показателей и границы интерпретации")
        box = self.textbox(self.body, 20)
        box.pack(fill="both", expand=True)
        box.insert("1.0", METHODS + "\n\nДанные хранятся локально в SQLite. Автосохранение выполняется в отдельном потоке каждую секунду. При аварии доступен последний успешно сохраненный отрезок; продолжение прерванной сессии оформляется новым листом.\n\nJSON — основной формат для воспроизведения: содержит все исходные события и вычисленные метрики. CSV — одна строка на событие, разделитель точка с запятой, UTF-8 BOM. HTML — автономный отчет с графиками, профилем и текстами; его можно распечатать в PDF из браузера.")
        box.configure(state="disabled", font=("Segoe UI", 12), spacing3=9)

    def _callback_error(self, exc, value, tb):
        log = "".join(traceback.format_exception(exc, value, tb))
        with (self.data_dir / "errors.log").open("a", encoding="utf-8") as handle:
            handle.write(log + "\n")
        messagebox.showerror("Ошибка TextLab", f"{value}\nПодробности записаны в errors.log.", parent=self)

    def close(self):
        if self.rec and self.rec.status != "completed":
            self.rec.pause("close_dialog")
            self.update_recording_ui()
            if not messagebox.askyesno("Закрыть TextLab", "Завершить текущую сессию, сохранить данные и закрыть программу?", parent=self):
                return
            self.rec.finish("application_closed")
            self.save()
        try:
            self.store.flush()
        except OSError as exc:
            messagebox.showerror("Ошибка сохранения", f"{exc}\nЭкспортируйте данные перед закрытием.", parent=self)
            if self.rec and self.rec.status == "completed":
                for button in self.nav:
                    button.configure(state="normal")
                self.show_report(self.rec.snapshot())
            return
        self.store.close()
        self.destroy()

    def destroy(self):
        # Cancel interpreter callbacks before destroying their Python commands.
        # Drain widget-owned idle callbacks first so their own owners remove
        # them from _tclCommands; canceling those through the root double-frees.
        self.update_idletasks()
        for job in self.tk.splitlist(self.tk.call("after", "info")):
            self.after_cancel(job)
        super().destroy()


def main():
    parser = argparse.ArgumentParser(description="TextLab — исследование текстового ввода")
    parser.add_argument("--data-dir", type=Path, help="Папка базы SQLite и экспортов")
    args = parser.parse_args()
    default = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share")) / "TextLab"
    data_dir = args.data_dir or default
    lock = None
    try:
        lock = InstanceLock(data_dir)
        app = TextLab(data_dir)
        app.mainloop()
    except Exception:
        data_dir.mkdir(parents=True, exist_ok=True)
        log = traceback.format_exc()
        (data_dir / "startup-error.log").write_text(log, encoding="utf-8")
        messagebox.showerror("TextLab", f"Ошибка приложения.\n{log}")
        raise
    finally:
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    main()
