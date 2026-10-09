"""
Command menus ("/" button) and persistent reply keyboards for both bots
"""

from typing import List

from telegram import (
    BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
    ReplyKeyboardMarkup,
)

from subscriptions import QUIET_WINDOWS, Settings, window_key

PUBLIC_COMMANDS = [
    BotCommand("status", "Чи є світло зараз"),
    BotCommand("grid", "Статистика наявності світла"),
    BotCommand("history", "Відключення за добу"),
    BotCommand("subscribe", "Увімкнути сповіщення"),
    BotCommand("unsubscribe", "Вимкнути сповіщення"),
    BotCommand("settings", "Тихі години та які сповіщення надсилати"),
    BotCommand("help", "Допомога"),
]

PRIVATE_COMMANDS = [
    BotCommand("status", "Повний статус інвертора"),
    BotCommand("chart", "Графіки"),
    BotCommand("stats", "Статистика відключень"),
    BotCommand("subscribers", "Кількість підписників"),
    BotCommand("ntfytest", "Тест сповіщення ntfy"),
    BotCommand("help", "Допомога"),
]

BTN_STATUS = "\U0001f6a6 Статус"
BTN_STATS = "\U0001f4ca Статистика"
BTN_HISTORY = "\U0001f4cb Історія"
BTN_NOTIFY = "\U0001f514 Сповіщення"
BTN_CHARTS = "\U0001f4c8 Графіки"
BTN_SUBSCRIBERS = "\U0001f465 Підписники"
BTN_SETTINGS = "⚙️ Налаштування"

# Button text -> action handled by the bot
PUBLIC_BUTTONS = {
    BTN_STATUS: 'status',
    BTN_STATS: 'grid',
    BTN_HISTORY: 'history',
    BTN_NOTIFY: 'notify',
    BTN_SETTINGS: 'settings',
}

PRIVATE_BUTTONS = {
    BTN_STATUS: 'status',
    BTN_CHARTS: 'chart',
    BTN_STATS: 'stats',
    BTN_SUBSCRIBERS: 'subscribers',
}


def _keyboard(rows: List[List[str]]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(text) for text in row] for row in rows],
        resize_keyboard=True,
        is_persistent=True,
    )


def public_keyboard() -> ReplyKeyboardMarkup:
    return _keyboard([[BTN_STATUS, BTN_STATS], [BTN_HISTORY, BTN_NOTIFY],
                      [BTN_SETTINGS]])


MODE_LABELS = {'all': "Усі", 'off_only': "Лише відключення",
               'on_only': "Лише повернення"}


def _mark(label: str, current: bool) -> str:
    return f"• {label}" if current else label


def settings_keyboard(s: Settings) -> InlineKeyboardMarkup:
    """Inline /settings menu; callback data "set:<kind>:<value>" """
    rows = []
    if s.quiet_enabled:
        rows.append([InlineKeyboardButton("\U0001f515 Тихі години: увімк",
                                          callback_data="set:quiet:off")])
        current = window_key(s)
        rows.append([
            InlineKeyboardButton(_mark(key.replace('-', '–'), key == current),
                                 callback_data=f"set:window:{key}")
            for key in QUIET_WINDOWS
        ])
    else:
        rows.append([InlineKeyboardButton("\U0001f514 Тихі години: вимк",
                                          callback_data="set:quiet:on")])
    modes = [InlineKeyboardButton(_mark(label, mode == s.notify_mode),
                                  callback_data=f"set:mode:{mode}")
             for mode, label in MODE_LABELS.items()]
    rows.append(modes[:2])
    rows.append(modes[2:])
    if s.remind_enabled:
        rows.append([InlineKeyboardButton("⏰ Нагадування за графіком: увімк",
                                          callback_data="set:remind:off")])
    else:
        rows.append([InlineKeyboardButton("⏰ Нагадування за графіком: вимк",
                                          callback_data="set:remind:on")])
    return InlineKeyboardMarkup(rows)


def private_keyboard() -> ReplyKeyboardMarkup:
    return _keyboard([[BTN_STATUS, BTN_CHARTS], [BTN_STATS, BTN_SUBSCRIBERS]])
