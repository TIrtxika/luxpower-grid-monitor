"""
Stage 3: chart handlers in both bots.
Run: python -m unittest test_bot_charts
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import bot
from telegram.error import BadRequest


def chart_query(data, user_id=None, photo=False):
    query = MagicMock()
    query.data = data
    query.from_user.id = bot.config.OWNER_CHAT_ID if user_id is None else user_id
    query.answer = AsyncMock()
    query.edit_message_media = AsyncMock()
    query.message.photo = [MagicMock()] if photo else []
    query.message.reply_photo = AsyncMock()
    query.message.reply_text = AsyncMock()
    return MagicMock(callback_query=query), query


class ChartCallbackTest(unittest.TestCase):
    def run_cb(self, update, png=b'png'):
        with patch.object(bot.CHART_CACHE, 'get', new=AsyncMock(return_value=png)) as get:
            asyncio.run(bot.callback_chart(update, MagicMock()))
        return get

    def test_public_timeline_sends_photo(self):
        update, query = chart_query('ch:g:timeline:24h', user_id=12345)
        get = self.run_cb(update)
        get.assert_awaited_once_with('timeline', '24h')
        call = query.message.reply_photo.await_args
        self.assertEqual(call.args[0], b'png')
        self.assertEqual(call.kwargs['caption'], "Шкала світла — 24 години")

    def test_switching_period_edits_the_photo(self):
        update, query = chart_query('ch:p:voltage:7d', photo=True)
        self.run_cb(update)
        query.edit_message_media.assert_awaited_once()
        query.message.reply_photo.assert_not_awaited()

    def test_pressing_the_current_chart_again_is_not_an_error(self):
        update, query = chart_query('ch:p:voltage:7d', photo=True)
        query.edit_message_media.side_effect = BadRequest(
            "Message is not modified: specified new message content and reply "
            "markup are exactly the same as a current content")
        self.run_cb(update)  # must not raise

    def test_other_edit_errors_still_raise(self):
        update, query = chart_query('ch:p:voltage:7d', photo=True)
        query.edit_message_media.side_effect = BadRequest("Message to edit not found")
        with self.assertRaises(BadRequest):
            self.run_cb(update)

    def test_private_chart_denied_for_others(self):
        update, query = chart_query('ch:p:voltage:24h', user_id=bot.config.OWNER_CHAT_ID + 1)
        get = self.run_cb(update)
        get.assert_not_awaited()
        self.assertIn("Доступ заборонено", query.answer.await_args.args[0])

    def test_render_failure_is_reported(self):
        update, query = chart_query('ch:g:heatmap:7d', user_id=12345)
        with patch.object(bot.CHART_CACHE, 'get',
                          new=AsyncMock(side_effect=RuntimeError("boom"))):
            asyncio.run(bot.callback_chart(update, MagicMock()))
        self.assertIn("Не вдалося", query.message.reply_text.await_args.args[0])

    def test_invalid_callback_is_ignored(self):
        update, query = chart_query('ch:g:voltage:24h', user_id=12345)
        get = self.run_cb(update)
        get.assert_not_awaited()


class ChartMenusTest(unittest.TestCase):
    def test_grid_keyboard_has_chart_button(self):
        callbacks = [b.callback_data for row in bot._grid_keyboard().inline_keyboard
                     for b in row]
        self.assertIn('ch:g:timeline:24h', callbacks)

    def test_private_chart_command_shows_types(self):
        update = MagicMock()
        update.effective_chat.id = bot.config.OWNER_CHAT_ID
        update.message.reply_text = AsyncMock()
        asyncio.run(bot.cmd_chart(update, MagicMock()))
        markup = update.message.reply_text.await_args.kwargs['reply_markup']
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn('ch:p:voltage:24h', callbacks)

    def builder(self):
        builder = MagicMock()
        for name in ('token', 'post_init', 'post_stop', 'post_shutdown'):
            getattr(builder, name).return_value = builder
        return builder

    def patterns(self, app):
        return [getattr(getattr(c.args[0], 'pattern', None), 'pattern', None)
                for c in app.add_handler.call_args_list]

    def test_both_bots_route_chart_callbacks(self):
        public = self.builder()
        with patch.object(bot.Application, 'builder', return_value=public), \
             patch.object(bot, 'get_db'), patch.object(bot, 'close_db'), \
             patch.object(bot, 'get_poller'), patch.object(bot, 'Bot'):
            bot.run_public_bot()
        self.assertIn('^ch:', self.patterns(public.build.return_value))

        private = self.builder()
        with patch.object(bot.Application, 'builder', return_value=private):
            bot.run_private_bot()
        self.assertIn('^ch:', self.patterns(private.build.return_value))


if __name__ == '__main__':
    unittest.main()
