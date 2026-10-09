"""
Owner notification channels outside Telegram (ntfy)
The topic and token are secrets: never log them
"""

import logging
from dataclasses import dataclass
from typing import Optional, Tuple

import requests

import config

logger = logging.getLogger(__name__)

PRIORITY_LOW = 2
PRIORITY_DEFAULT = 3
PRIORITY_HIGH = 4
PRIORITY_URGENT = 5


@dataclass(frozen=True)
class Notice:
    title: str
    message: str
    priority: int = PRIORITY_DEFAULT
    tags: Tuple[str, ...] = ()

    @classmethod
    def from_text(cls, text: str, priority: int = PRIORITY_DEFAULT,
                  tags: Tuple[str, ...] = ()) -> 'Notice':
        """First line becomes the title, the rest the body"""
        title, _, body = text.strip().partition("\n")
        title = title.strip()
        return cls(title, body.strip() or title, priority, tuple(tags))


class NtfyNotifier:
    """Publishes to an ntfy topic through the JSON API (POST to server root)"""

    def __init__(self, server: str, topic: str, token: str = "",
                 timeout: float = 10, session=None):
        self.url = server.rstrip('/') + '/'
        self.topic = topic
        self.token = token
        self.timeout = timeout
        self.session = session or requests

    def send(self, notice: Notice) -> bool:
        payload = {'topic': self.topic, 'title': notice.title,
                   'message': notice.message, 'priority': notice.priority}
        if notice.tags:
            payload['tags'] = list(notice.tags)
        headers = {'Authorization': f"Bearer {self.token}"} if self.token else {}
        try:
            response = self.session.post(self.url, json=payload, headers=headers,
                                         timeout=self.timeout)
        except requests.RequestException as e:
            # The exception text may contain the URL/topic: log the type only
            logger.error(f"ntfy publish failed: {type(e).__name__}")
            return False
        if response.status_code >= 300:
            logger.error(f"ntfy publish failed: HTTP {response.status_code}")
            return False
        return True


def ntfy_from_config() -> Optional[NtfyNotifier]:
    """ntfy channel from config, None when no topic is set"""
    if not config.NTFY_TOPIC:
        return None
    return NtfyNotifier(config.NTFY_URL, config.NTFY_TOPIC, config.NTFY_TOKEN)
