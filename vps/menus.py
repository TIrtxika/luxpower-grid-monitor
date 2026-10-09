"""
Command menus ("/" button) and persistent reply keyboards for both bots
"""

from typing import List

from telegram import BotCommand, KeyboardButton, ReplyKeyboardMarkup

PUBLIC_COMMANDS = [
    BotCommand("status", "Чи є світло зараз"),
    BotCommand("grid", "Статистика наявності світла"),
    BotCommand("history", "Відключення за добу"),
    BotCommand("subscribe", "Увімкнути сповіщення"),
    BotCommand("unsubscribe", "Вимкнути сповіщення"),
    BotCommand("help", "Допомога"),
]

PRIVATE_COMMANDS = [
    BotCommand("status", "Повний статус інвертора"),
    BotCommand("chart", "Графіки"),
    BotCommand("stats", "Статистика відключень"),
    BotCommand("subscribers", "Кількість підписників"),
    BotCommand("help", "Допомога"),
]

BTN_STATUS = "\U0001f6a6 Статус"
BTN_STATS = "\U0001f4ca Статистика"
BTN_HISTORY = "\U0001f4cb Історія"
BTN_NOTIFY = "\U0001f514 Сповіщення"
BTN_CHARTS = "\U0001f4c8 Графіки"
BTN_SUBSCRIBERS = "\U0001f465 Підписники"

# Button text -> action handled by the bot
PUBLIC_BUTTONS = {
    BTN_STATUS: 'status',
    BTN_STATS: 'grid',
    BTN_HISTORY: 'history',
    BTN_NOTIFY: 'notify',
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
    return _keyboard([[BTN_STATUS, BTN_STATS], [BTN_HISTORY, BTN_NOTIFY]])


def private_keyboard() -> ReplyKeyboardMarkup:
    return _keyboard([[BTN_STATUS, BTN_CHARTS], [BTN_STATS, BTN_SUBSCRIBERS]])
