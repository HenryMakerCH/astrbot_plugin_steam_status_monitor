import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from astrbot.api.message_components import Plain
from src.application.services.achievement_tracking import AchievementTrackingMixin


class AchievementNotificationTest(unittest.TestCase):
    def test_achievement_broadcast_omits_achievement_names(self):
        class DummyPlugin(AchievementTrackingMixin):
            def __init__(self):
                self.config = {"notify_send_text": True, "notify_send_image": False}
                self.group_achievement_enabled = {"1001": True}
                self.max_achievement_notifications = 5
                self.achievement_monitor = MagicMock()
                self.achievement_monitor.details_cache = {}
                self.API_KEY = "test_key"
                self.notify_sessions = {"1001": "aiocqhttp:GroupMessage:0_1001"}
                self.push_groups = {}
                self.context = MagicMock()
                self.context.send_message = AsyncMock()

        plugin = DummyPlugin()
        achievements = {"FORGE20_SAW", "FORGE_FIRSTDEATH"}

        result = asyncio.run(
            plugin.notify_new_achievements(
                group_id="1001",
                steamid="76561198000000001",
                player_name="鸡",
                gameid="12345",
                game_name="ForgeTax",
                new_achievements=achievements,
            )
        )

        self.assertTrue(result)
        plugin.context.send_message.assert_called_once()
        _, args, _ = plugin.context.send_message.mock_calls[0]
        chain = args[1]
        plain_components = [comp for comp in chain.chain if isinstance(comp, Plain)]
        self.assertEqual(len(plain_components), 1)
        text = plain_components[0].text
        self.assertEqual(text, "🏆【鸡】在 ForgeTax 解锁成就：")
        self.assertNotIn("FORGE20_SAW", text)
        self.assertNotIn("FORGE_FIRSTDEATH", text)
