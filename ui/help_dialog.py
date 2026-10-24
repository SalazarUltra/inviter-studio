"""Contextual, searchable help with navigation to the actual app screens."""

import customtkinter as ctk

from ui.help_content import GUIDES, search_guides
from ui.hints import wrap_label
from ui.theme import COLORS, FONTS, apply_button_role, style_tree


class HelpDialog(ctk.CTkToplevel):
    def __init__(self, app, topic="start"):
        super().__init__(app)
        self.app = app
        self.topic = "start"
        self.title("Помощь · Inviter Studio")
        self.geometry("800x660")
        self.minsize(600, 420)
        self.transient(app)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 10))
        top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(top, text="Помощь для первого запуска", font=FONTS["heading"]).grid(row=0, column=0, sticky="w")
        close_button = ctk.CTkButton(top, text="Закрыть", width=85, command=self.destroy)
        apply_button_role(close_button, "secondary")
        close_button.grid(row=0, column=1, padx=(12, 0))
        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 8))
        controls.grid_columnconfigure(1, weight=1)
        self.choices = {guide["title"]: key for key, guide in GUIDES.items()}
        self.menu = ctk.CTkOptionMenu(
            controls, values=list(self.choices), command=lambda value: self.show_topic(self.choices[value]), width=245
        )
        ctk.CTkLabel(controls, text="Раздел справки", font=FONTS["small"], text_color=COLORS["muted"]).grid(
            row=0, column=0, sticky="w"
        )
        ctk.CTkLabel(controls, text="Поиск по справке", font=FONTS["small"], text_color=COLORS["muted"]).grid(
            row=0, column=1, sticky="w"
        )
        self.menu.grid(row=1, column=0, sticky="w", padx=(0, 8))
        self.search = ctk.CTkEntry(controls, placeholder_text="Поиск: личка, интервал, сохранение…")
        self.search.grid(row=1, column=1, sticky="ew")
        self.search_variable = ctk.StringVar()
        self.search.configure(textvariable=self.search_variable)
        self.search_variable.trace_add("write", self.search_changed)
        self.summary = ctk.CTkLabel(
            self, text="", anchor="w", justify="left", font=FONTS["small"], text_color=COLORS["muted"]
        )
        self.summary.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 10))
        wrap_label(self.summary, 280)
        self.content = ctk.CTkScrollableFrame(self, fg_color=COLORS["bg"])
        self.content.grid(row=3, column=0, sticky="nsew", padx=12)
        self.content.grid_columnconfigure(0, weight=1)
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=4, column=0, sticky="ew", padx=20, pady=14)
        self.action_buttons = {}
        for column, (key, label) in enumerate(
            (("accounts", "1. Аккаунты"), ("targets", "2. Аудитория"), ("task", "3. Задача"))
        ):
            footer.grid_columnconfigure(column, weight=1)
            button = ctk.CTkButton(footer, text=label, command=lambda tab=key: self.navigate(tab), height=34)
            button.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 5, 0 if column == 2 else 5))
            self.action_buttons[key] = button
        style_tree(self)
        self.bind("<Escape>", lambda _event: self.destroy())
        self.show_topic(topic)

    def navigate(self, key):
        self.app._switch_tab(key)
        self.destroy()

    def show_topic(self, key):
        key = key if key in GUIDES else "start"
        self.topic = key
        self.search_variable.set("")
        self.menu.set(GUIDES[key]["title"])
        self.summary.configure(text=GUIDES[key]["intro"])
        self.render([(key, heading, text) for heading, text in GUIDES[key]["blocks"]])

    def search_changed(self, *_args):
        query = self.search_variable.get().strip()
        if not query:
            guide = GUIDES[self.topic]
            self.summary.configure(text=guide["intro"])
            self.render([(self.topic, heading, text) for heading, text in guide["blocks"]])
            return
        matches = search_guides(query)
        self.summary.configure(
            text=f"Найдено пояснений: {len(matches)}"
            if matches
            else "Ничего не найдено. Попробуйте «личные сообщения», «интервал» или выберите раздел."
        )
        self.render(matches, show_topic=True)

    def render(self, blocks, show_topic=False):
        for widget in self.content.winfo_children():
            widget.destroy()
        self.rendered_blocks = blocks
        for row, (key, heading, text) in enumerate(blocks):
            card = ctk.CTkFrame(self.content, fg_color=COLORS["surface"], border_width=1, border_color=COLORS["border"])
            card.grid(row=row, column=0, sticky="ew", padx=8, pady=(0, 10))
            title = f"{GUIDES[key]['title']} · {heading}" if show_topic else heading
            for value, font, color, padding in (
                (title, FONTS["subhead"], COLORS["text"], (12, 5)),
                (text, FONTS["body"], COLORS["muted"], (0, 14)),
            ):
                label = ctk.CTkLabel(
                    card, text=value, justify="left", anchor="w", font=font, text_color=color, wraplength=650
                )
                label.pack(fill="x", padx=16, pady=padding)
                wrap_label(label, 240)
        style_tree(self.content)
        for button in self.action_buttons.values():
            apply_button_role(button, "secondary")
        self.content._parent_canvas.yview_moveto(0)
