"""owned 通知接入：去重键、文本行、图片渲染分支。"""
import os
import unittest
from unittest.mock import AsyncMock, patch

from src.application.services.notification_tracking import NotificationTrackingMixin


class _FakeContext:
    def __init__(self):
        self.messages = []

    async def send_message(self, session, chain):
        self.messages.append((session, chain))


class _Stub(NotificationTrackingMixin):
    def __init__(self):
        self.config = {"notify_send_text": True, "notify_send_image": False}
        self.data_dir = "."
        self.API_KEY = "k"
        self.SGDB_API_KEY = ""
        self.SGDB_API_BASE = ""
        self.STEAM_STORE_BASE = ""
        self.STEAM_API_BASE = ""
        self.proxy = None
        self._plugin_version = "4.10.0"
        self.context = _FakeContext()

    def _get_notify_sessions(self, group_id, sid):
        return [f"session:{group_id}"]


def _chain_text(chain):
    parts = []
    for segment in getattr(chain, "chain", []):
        text = getattr(segment, "text", None)
        if text:
            parts.append(text)
    return "".join(parts)


class OwnedNotificationTests(unittest.IsolatedAsyncioTestCase):
    def test_event_key_uses_added_at(self):
        stub = _Stub()
        key = stub._notification_event_key(
            {"type": "owned", "sid": "s1", "gameid": "570", "added_at": 123}, "session:111"
        )
        self.assertEqual(key, ("session:111", "s1", "570", "owned", 123))

    def test_dedup_window(self):
        stub = _Stub()
        noti = {"type": "owned", "sid": "s1", "gameid": "570", "added_at": 123}
        self.assertTrue(stub._should_send_notification(noti, "session:111"))
        self.assertFalse(stub._should_send_notification(noti, "session:111"))

    async def test_text_line_for_owned(self):
        stub = _Stub()
        noti = {
            "type": "owned", "sid": "s1", "gameid": "570", "added_at": 1,
            "name": "阿猫", "game": "Dota 2", "owned_count": 268, "render": False,
        }

        failed = await stub._send_merged_notification("111", [noti])

        self.assertEqual(failed, [])
        self.assertEqual(len(stub.context.messages), 1)
        session, chain = stub.context.messages[0]
        self.assertEqual(session, "session:111")
        text = _chain_text(chain)
        self.assertIn("新入库", text)
        self.assertIn("Dota 2", text)
        self.assertIn("当前共 268 款", text)

    async def test_render_flag_false_skips_image(self):
        stub = _Stub()
        path = await stub._render_notification_image({"type": "owned", "render": False})
        self.assertIsNone(path)

    async def test_render_owned_calls_renderer(self):
        stub = _Stub()
        with patch(
            "src.application.services.notification_tracking.render_owned_game",
            new=AsyncMock(return_value=b"fake-png"),
        ) as mocked:
            path = await stub._render_notification_image({
                "type": "owned", "render": True, "sid": "s1", "name": "阿猫",
                "avatar_url": None, "gameid": "570", "game": "Dota 2",
                "owned_count": 3, "card_data": None, "added_at": 1,
            })

        self.assertEqual(mocked.await_count, 1)
        self.assertIsNotNone(path)
        try:
            with open(path, "rb") as stream:
                self.assertEqual(stream.read(), b"fake-png")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
