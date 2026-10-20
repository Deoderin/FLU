"""Мова повідомлень: українська (за замовчуванням) або англійська.

Кожне повідомлення пишеться одразу двома мовами: T("українською", "in English").
Мова береться з FLEET_LANG, з прапорця --lang або із запиту веб-інтерфейсу.
"""
from __future__ import annotations

import contextvars
import os

LANGS = ("uk", "en")
_lang: contextvars.ContextVar[str] = contextvars.ContextVar("fleet_lang", default=os.environ.get("FLEET_LANG", "uk"))


def set_lang(lang: str | None) -> None:
    _lang.set(lang if lang in LANGS else "uk")


def get_lang() -> str:
    lang = _lang.get()
    return lang if lang in LANGS else "uk"


def T(uk: str, en: str) -> str:
    return en if get_lang() == "en" else uk
