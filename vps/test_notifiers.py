"""
ntfy owner notifications.
Run: python -m unittest test_notifiers
"""

import unittest
from unittest.mock import MagicMock, patch

import requests

import notifiers
from notifiers import Notice, NtfyNotifier

TOPIC = "luxpower-secret-topic"
TOKEN = "tk_secret_token"


def session(status=200, error=None):
    s = MagicMock()
    if error:
        s.post.side_effect = error
    else:
        s.post.return_value = MagicMock(status_code=status)
    return s


class NoticeTest(unittest.TestCase):
    def test_from_text_splits_title_and_body(self):
        n = Notice.from_text("Світла немає\n\n21:40:00 09.10.2026", 4, ('red_circle',))
        self.assertEqual((n.title, n.message, n.priority, n.tags),
                         ("Світла немає", "21:40:00 09.10.2026", 4, ('red_circle',)))

    def test_single_line_text_is_also_the_body(self):
        n = Notice.from_text("Тест")
        self.assertEqual((n.title, n.message), ("Тест", "Тест"))


class NtfyNotifierTest(unittest.TestCase):
    def test_publishes_json_to_server_root(self):
        s = session()
        ok = NtfyNotifier("https://ntfy.sh/", TOPIC, session=s).send(
            Notice("Світла немає", "21:40", 4, ('red_circle',)))
        self.assertTrue(ok)
        call = s.post.call_args
        self.assertEqual(call.args[0], "https://ntfy.sh/")
        self.assertEqual(call.kwargs['json'], {
            'topic': TOPIC, 'title': "Світла немає", 'message': "21:40",
            'priority': 4, 'tags': ['red_circle']})
        self.assertNotIn('Authorization', call.kwargs['headers'])
        self.assertEqual(call.kwargs['timeout'], 10)

    def test_token_sent_as_bearer(self):
        s = session()
        NtfyNotifier("https://ntfy.sh", TOPIC, TOKEN, session=s).send(Notice("a", "b"))
        self.assertEqual(s.post.call_args.kwargs['headers'],
                         {'Authorization': f"Bearer {TOKEN}"})

    def test_http_error_is_logged_without_secrets(self):
        s = session(status=403)
        with self.assertLogs('notifiers', level='ERROR') as logs:
            ok = NtfyNotifier("https://ntfy.sh", TOPIC, TOKEN, session=s).send(Notice("a", "b"))
        self.assertFalse(ok)
        text = "\n".join(logs.output)
        self.assertIn("403", text)
        self.assertNotIn(TOPIC, text)
        self.assertNotIn(TOKEN, text)

    def test_network_error_is_logged_without_secrets(self):
        s = session(error=requests.ConnectionError(f"https://ntfy.sh/{TOPIC} {TOKEN}"))
        with self.assertLogs('notifiers', level='ERROR') as logs:
            ok = NtfyNotifier("https://ntfy.sh", TOPIC, TOKEN, session=s).send(Notice("a", "b"))
        self.assertFalse(ok)
        text = "\n".join(logs.output)
        self.assertNotIn(TOPIC, text)
        self.assertNotIn(TOKEN, text)


class ConfigTest(unittest.TestCase):
    def test_disabled_without_topic(self):
        with patch.object(notifiers.config, 'NTFY_TOPIC', ''):
            self.assertIsNone(notifiers.ntfy_from_config())

    def test_enabled_with_topic(self):
        with patch.object(notifiers.config, 'NTFY_TOPIC', TOPIC), \
             patch.object(notifiers.config, 'NTFY_URL', 'https://ntfy.sh'), \
             patch.object(notifiers.config, 'NTFY_TOKEN', ''):
            n = notifiers.ntfy_from_config()
        self.assertEqual((n.url, n.topic), ("https://ntfy.sh/", TOPIC))


if __name__ == '__main__':
    unittest.main()
