"""
Command menus and reply keyboards.
Run: python -m unittest test_menus
"""

import unittest

from telegram import ReplyKeyboardMarkup

import menus


def labels(keyboard: ReplyKeyboardMarkup):
    return [button.text for row in keyboard.keyboard for button in row]


class MenusTest(unittest.TestCase):
    def test_public_commands(self):
        self.assertEqual([c.command for c in menus.PUBLIC_COMMANDS],
                         ['status', 'grid', 'history', 'subscribe',
                          'unsubscribe', 'settings', 'help'])

    def test_public_keyboard_has_settings_button(self):
        self.assertIn(menus.BTN_SETTINGS, labels(menus.public_keyboard()))
        self.assertEqual(menus.PUBLIC_BUTTONS[menus.BTN_SETTINGS], 'settings')

    def test_private_commands(self):
        self.assertEqual([c.command for c in menus.PRIVATE_COMMANDS],
                         ['status', 'chart', 'stats', 'subscribers', 'ntfytest',
                          'help'])

    def test_public_keyboard_buttons_all_have_actions(self):
        kb = menus.public_keyboard()
        self.assertTrue(kb.resize_keyboard)
        self.assertTrue(kb.is_persistent)
        self.assertEqual(sorted(labels(kb)), sorted(menus.PUBLIC_BUTTONS))

    def test_private_keyboard_buttons_all_have_actions(self):
        kb = menus.private_keyboard()
        self.assertEqual(sorted(labels(kb)), sorted(menus.PRIVATE_BUTTONS))

    def test_button_actions(self):
        self.assertEqual(menus.PUBLIC_BUTTONS[menus.BTN_STATUS], 'status')
        self.assertEqual(menus.PUBLIC_BUTTONS[menus.BTN_NOTIFY], 'notify')
        self.assertEqual(menus.PRIVATE_BUTTONS[menus.BTN_CHARTS], 'chart')
        self.assertEqual(menus.PRIVATE_BUTTONS[menus.BTN_SUBSCRIBERS], 'subscribers')


if __name__ == '__main__':
    unittest.main()
