"""
Stage 4: ntfy wiring in the bots.
Run: python -m unittest test_bot_ntfy
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import bot


def owner_update():
    update = MagicMock()
    update.effective_chat.id = bot.config.OWNER_CHAT_ID
    update.message.reply_text = AsyncMock()
    return update


class NtfyTestCommandTest(unittest.TestCase):
    def run_cmd(self, channel):
        update = owner_update()
        with patch.object(bot, 'ntfy_from_config', return_value=channel):
            asyncio.run(bot.cmd_ntfy_test(update, MagicMock()))
        return update.message.reply_text.await_args.args[0]

    def test_not_configured(self):
        self.assertIn("не налаштовано", self.run_cmd(None))

    def test_sent(self):
        channel = MagicMock()
        channel.send.return_value = True
        self.assertIn("Надіслано", self.run_cmd(channel))
        notice = channel.send.call_args.args[0]
        self.assertEqual(notice.tags, ('test_tube',))

    def test_failed(self):
        channel = MagicMock()
        channel.send.return_value = False
        self.assertIn("Не вдалося", self.run_cmd(channel))


class WiringTest(unittest.TestCase):
    def builder(self):
        builder = MagicMock()
        for name in ('token', 'post_init', 'post_stop', 'post_shutdown'):
            getattr(builder, name).return_value = builder
        return builder

    def test_public_bot_passes_ntfy_to_alert_manager(self):
        builder = self.builder()
        channel = MagicMock()
        with patch.object(bot.Application, 'builder', return_value=builder), \
             patch.object(bot, 'get_db'), patch.object(bot, 'close_db'), \
             patch.object(bot, 'get_poller'), patch.object(bot, 'Bot'), \
             patch.object(bot, '_init_owner_bot', new=AsyncMock(return_value=None)), \
             patch.object(bot, 'ntfy_from_config', return_value=channel), \
             patch.object(bot, 'AlertManager') as manager:
            bot.run_public_bot()
            application = MagicMock()
            application.bot.set_my_commands = AsyncMock()
            asyncio.run(builder.post_init.call_args.args[0](application))
        self.assertEqual(manager.call_args.kwargs['owner_channels'], [channel])

    def test_private_bot_registers_ntfytest(self):
        builder = self.builder()
        with patch.object(bot.Application, 'builder', return_value=builder):
            bot.run_private_bot()
        commands = set()
        for call in builder.build.return_value.add_handler.call_args_list:
            commands |= set(getattr(call.args[0], 'commands', ()))
        self.assertIn('ntfytest', commands)


if __name__ == '__main__':
    unittest.main()
