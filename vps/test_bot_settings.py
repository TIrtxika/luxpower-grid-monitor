"""
/settings in the public bot: quiet hours and notify mode.
Run: python -m unittest test_bot_settings
"""

import asyncio
import unittest
from datetime import time
from unittest.mock import AsyncMock, MagicMock, patch

import bot
import menus
from messages import format_settings
from subscriptions import Settings

CHAT = 777


def callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def labels(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


class SettingsTextTest(unittest.TestCase):
    def test_default_text(self):
        text = format_settings(Settings(CHAT))
        self.assertIn("23:00–07:00", text)
        self.assertIn("беззвучно", text)
        self.assertIn("усі зміни", text)

    def test_disabled_and_mode(self):
        text = format_settings(Settings(CHAT, quiet_enabled=False, notify_mode='off_only'))
        self.assertIn("вимкнено", text)
        self.assertIn("лише відключення", text)

    def test_reminder_line_uses_lead_and_hides_without_schedule(self):
        self.assertIn("за 45 хв", format_settings(Settings(CHAT), remind_minutes=45))
        self.assertNotIn("Нагадування", format_settings(Settings(CHAT), remind_minutes=None))

    def test_reminder_line(self):
        self.assertIn("Нагадування за графіком: увімк", format_settings(Settings(CHAT)))
        self.assertIn("Нагадування за графіком: вимк",
                      format_settings(Settings(CHAT, remind_enabled=False)))


class SettingsKeyboardTest(unittest.TestCase):
    def test_marks_current_choices(self):
        kb = menus.settings_keyboard(Settings(CHAT))
        self.assertIn('set:quiet:off', callbacks(kb))
        self.assertIn('set:window:22-07', callbacks(kb))
        self.assertIn("• 23–07", labels(kb))
        self.assertIn("• Усі", labels(kb))

    def test_no_window_choice_when_disabled(self):
        kb = menus.settings_keyboard(Settings(CHAT, quiet_enabled=False))
        self.assertIn('set:quiet:on', callbacks(kb))
        self.assertNotIn('set:window:22-07', callbacks(kb))

    def test_no_reminder_toggle_without_schedule(self):
        kb = menus.settings_keyboard(Settings(CHAT), reminders=False)
        self.assertNotIn('set:remind:off', callbacks(kb))

    def test_bot_view_follows_config(self):
        with patch.object(bot.config, 'DTEK_GROUP', ''):
            text, kb = bot._settings_view(Settings(CHAT))
        self.assertNotIn('set:remind:off', callbacks(kb))
        self.assertNotIn("Нагадування", text)
        with patch.object(bot.config, 'DTEK_GROUP', '16.1'), \
             patch.object(bot.config, 'SCHEDULE_REMIND_MINUTES', 20):
            text, kb = bot._settings_view(Settings(CHAT))
        self.assertIn('set:remind:off', callbacks(kb))
        self.assertIn("за 20 хв", text)

    def test_reminder_toggle(self):
        self.assertIn('set:remind:off', callbacks(menus.settings_keyboard(Settings(CHAT))))
        self.assertIn('set:remind:on', callbacks(menus.settings_keyboard(
            Settings(CHAT, remind_enabled=False))))


class SettingsCommandTest(unittest.TestCase):
    def run_cmd(self, settings):
        db = MagicMock()
        db.get_settings.return_value = settings
        update = MagicMock()
        update.effective_chat.id = CHAT
        update.message.reply_text = AsyncMock()
        with patch.object(bot, 'get_db', return_value=db):
            asyncio.run(bot.cmd_settings(update, MagicMock()))
        return update.message.reply_text.await_args

    def test_not_subscribed(self):
        self.assertIn("/subscribe", self.run_cmd(None).args[0])

    def test_shows_settings_with_keyboard(self):
        call = self.run_cmd(Settings(CHAT))
        self.assertIn("23:00–07:00", call.args[0])
        self.assertIn('set:mode:off_only', callbacks(call.kwargs['reply_markup']))


class SettingsCallbackTest(unittest.TestCase):
    def press(self, data, before=Settings(CHAT), after=None):
        db = MagicMock()
        db.get_settings.side_effect = [before, after or before]
        query = MagicMock()
        query.data = data
        query.message.chat.id = CHAT
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        with patch.object(bot, 'get_db', return_value=db):
            asyncio.run(bot.callback_settings(MagicMock(callback_query=query), MagicMock()))
        return db, query

    def test_window(self):
        db, query = self.press('set:window:22-07')
        db.update_settings.assert_called_once_with(CHAT, quiet_from=time(22),
                                                   quiet_to=time(7))
        query.edit_message_text.assert_awaited_once()

    def test_mode(self):
        db, _ = self.press('set:mode:off_only')
        db.update_settings.assert_called_once_with(CHAT, notify_mode='off_only')

    def test_quiet_off(self):
        db, _ = self.press('set:quiet:off')
        db.update_settings.assert_called_once_with(CHAT, quiet_enabled=False)

    def test_remind_off(self):
        db, _ = self.press('set:remind:off')
        db.update_settings.assert_called_once_with(CHAT, remind_enabled=False)

    def test_invalid_value_is_ignored(self):
        db, query = self.press('set:mode:sometimes')
        db.update_settings.assert_not_called()
        query.answer.assert_awaited()

    def test_not_subscribed(self):
        db, query = self.press('set:mode:all', before=None)
        db.update_settings.assert_not_called()
        self.assertIn("/subscribe", query.answer.await_args.args[0])


if __name__ == '__main__':
    unittest.main()
