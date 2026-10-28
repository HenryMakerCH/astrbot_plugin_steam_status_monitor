"""成就轮询失败 10 次拉黑必须落盘（V4.11.3 修复双集合）。"""
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from src.application.services.achievement_tracking import AchievementTrackingMixin
from src.infrastructure.persistence.achievement_blacklist_store import (
    REASON_FETCH_FAILED,
    SOURCE_AUTO,
    AchievementBlacklistStore,
)


class _Monitor:
    """最小成就监视器：始终返回 None（模拟持续失败）。"""

    def __init__(self, store):
        self.blacklist_store = store
        self.details_cache = {}

    async def get_player_achievements(self, *args, **kwargs):
        return None

    def is_blacklisted(self, appid):
        return self.blacklist_store.contains(appid)

    def add_to_blacklist(self, appid, reason, game_name="", source="auto"):
        return self.blacklist_store.add(
            appid, reason=reason, game_name=game_name, source=source
        )


class _Plugin(AchievementTrackingMixin):
    def __init__(self, monitor):
        self.achievement_monitor = monitor
        self.achievement_fail_count = {}
        self.achievement_snapshots = {}
        self.achievement_poll_tasks = {}
        self.API_KEY = "test-key"


class AchievementTrackingBlacklistTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = self._tmp.name
        self.store = AchievementBlacklistStore(self.data_dir)
        self.monitor = _Monitor(self.store)
        self.plugin = _Plugin(self.monitor)

    def tearDown(self):
        self._tmp.cleanup()

    async def test_periodic_10_failures_persist(self):
        with patch(
            "src.application.services.achievement_tracking.asyncio.sleep",
            new=AsyncMock(return_value=None),
        ):
            await self.plugin.achievement_periodic_check(
                "g1", "s1", "730", "玩家", "CS2"
            )

        self.assertIn("730", self.store.entries)
        meta = self.store.entries["730"]
        self.assertEqual(REASON_FETCH_FAILED, meta["reason"])
        self.assertEqual(SOURCE_AUTO, meta["source"])
        self.assertEqual("CS2", meta["game_name"])
        self.assertGreater(meta["added_at"], 0)

        # 重启后仍在（落盘验证）
        reloaded = AchievementBlacklistStore(self.data_dir)
        self.assertTrue(reloaded.contains("730"))

    async def test_delayed_final_check_persists(self):
        today = time.strftime("%Y-%m-%d")
        self.plugin.achievement_fail_count = {("730", today): 9}

        with patch(
            "src.application.services.achievement_tracking.asyncio.sleep",
            new=AsyncMock(return_value=None),
        ):
            await self.plugin.achievement_delayed_final_check(
                "g1", "s1", "730", "玩家", "CS2"
            )

        reloaded = AchievementBlacklistStore(self.data_dir)
        self.assertTrue(reloaded.contains("730"))
        self.assertEqual(REASON_FETCH_FAILED, reloaded.entries["730"]["reason"])

    async def test_blacklisted_game_skips_polling(self):
        self.store.add("730", reason=REASON_FETCH_FAILED, game_name="CS2")
        called = AsyncMock(return_value=set())
        self.monitor.get_player_achievements = called

        with patch(
            "src.application.services.achievement_tracking.asyncio.sleep",
            new=AsyncMock(return_value=None),
        ):
            await self.plugin.achievement_periodic_check(
                "g1", "s1", "730", "玩家", "CS2"
            )

        called.assert_not_called()


if __name__ == "__main__":
    unittest.main()
