"""Минимальная заглушка aiogram, чтобы тестировать обработчики бота без сети и без установки aiogram.

Заглушка хранит зарегистрированные обработчики по имени функции, поэтому тест может вызвать их напрямую
с поддельными Message/CallbackQuery. Это проверяет нашу логику, но НЕ проверяет совместимость с настоящим API aiogram.
"""
import sys
import types
from dataclasses import dataclass, field


class Magic:
    def __getattr__(self, name):
        return Magic()

    def __call__(self, *a, **k):
        return Magic()

    def __and__(self, other):
        return Magic()

    def __invert__(self):
        return Magic()

    def __eq__(self, other):
        return Magic()

    __hash__ = object.__hash__


class Router:
    def __init__(self):
        self.handlers = {}

    def _reg(self, *a, **k):
        def deco(fn):
            self.handlers[fn.__name__] = fn
            return fn

        return deco

    message = callback_query = _reg


@dataclass
class InlineKeyboardButton:
    text: str
    url: str | None = None
    callback_data: str | None = None


@dataclass
class InlineKeyboardMarkup:
    inline_keyboard: list = field(default_factory=list)


@dataclass
class LinkPreviewOptions:
    is_disabled: bool = False


class TelegramBadRequest(Exception):
    pass


class TelegramForbiddenError(Exception):
    pass


class CommandObject:
    def __init__(self, args=None):
        self.args = args


class Bot:
    pass


class Dispatcher:
    pass


NAMES = ["aiogram", "aiogram.client", "aiogram.client.default", "aiogram.enums", "aiogram.exceptions",
         "aiogram.filters", "aiogram.types"]


def install():
    mods = {n: types.ModuleType(n) for n in NAMES}
    m = mods["aiogram"]
    m.Bot, m.Dispatcher, m.Router, m.F = Bot, Dispatcher, Router, Magic()
    mods["aiogram.client.default"].DefaultBotProperties = lambda **k: None
    mods["aiogram.enums"].ParseMode = types.SimpleNamespace(HTML="HTML")
    mods["aiogram.exceptions"].TelegramBadRequest = TelegramBadRequest
    mods["aiogram.exceptions"].TelegramForbiddenError = TelegramForbiddenError
    f = mods["aiogram.filters"]
    f.Command = lambda *a, **k: Magic()
    f.CommandStart = lambda *a, **k: Magic()
    f.CommandObject = CommandObject
    t = mods["aiogram.types"]
    t.CallbackQuery = t.Message = object
    t.InlineKeyboardButton, t.InlineKeyboardMarkup, t.LinkPreviewOptions = (
        InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions)
    sys.modules.update(mods)


def uninstall():
    for n in NAMES + ["kinobot.bot"]:
        sys.modules.pop(n, None)
