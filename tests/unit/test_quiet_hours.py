"""静默时段 notify_quiet_hours：解析边界、跨午夜、4 个闸门与「丢弃不补发」语义。"""

import asyncio
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.application.services.achievement_tracking import AchievementTrackingMixin
from src.application.services.notification_tracking import NotificationTrackingMixin
from src.application.services.polling_tracking import PollingTrackingMixin
from src.application.services.session_service import SessionService
from src.shared.utils.quiet_hours import hour_in_window, in_quiet_hours, parse_quiet_hours

NOTIFY_PATCH = "src.application.services.notification_tracking.in_quiet_hours"
ACH_PATCH = "src.application.services.achievement_tracking.in_quiet_hours"
RESUME_PATCH = "src.application.services.session_service.in_quiet_hours"


class _Ctx:
    def __init__(self):
        self.sent = []

    async def send_message(self, session, chain):
        self.sent.append((session, chain))


# ---------- 纯函数 ----------

class QuietHoursParseTests(unittest.TestCase):
    def test_parse_returns_window(self):
        self.assertEqual((0, 8), parse_quiet_hours("0,8"))
        self.assertEqual((23, 8), parse_quiet_hours("23,8"))

    def test_parse_tolerates_spaces_and_fullwidth_comma(self):
        self.assertEqual((0, 8), parse_quiet_hours(" 0 ， 8 "))
        self.assertEqual((23, 8), parse_quiet_hours("23、8"))

    def test_parse_disabled_values(self):
        for raw in ("0,0", "8,8", "", "   ", None):
            self.assertIsNone(parse_quiet_hours(raw), raw)

    def test_parse_invalid_values_return_none(self):
        for raw in ("abc", "1", "1,2,3", "24,3", "-1,5", "1.5,3", "0,24"):
            self.assertIsNone(parse_quiet_hours(raw), raw)

    def test_hour_in_window_same_day(self):
        self.assertTrue(hour_in_window(0, 8, 0))
        self.assertTrue(hour_in_window(0, 8, 7))
        self.assertFalse(hour_in_window(0, 8, 8))
        self.assertFalse(hour_in_window(0, 8, 22))

    def test_hour_in_window_cross_midnight(self):
        self.assertTrue(hour_in_window(23, 8, 23))
        self.assertTrue(hour_in_window(23, 8, 0))
        self.assertTrue(hour_in_window(23, 8, 7))
        self.assertFalse(hour_in_window(23, 8, 8))
        self.assertFalse(hour_in_window(23, 8, 22))

    def test_hour_in_window_same_value_is_never(self):
        self.assertFalse(hour_in_window(5, 5, 5))

    def test_in_quiet_hours_uses_injected_now(self):
        cfg = {"notify_quiet_hours": "23,8"}
        self.assertTrue(in_quiet_hours(cfg, now=datetime(2026, 1, 1, 2, 30)))
        self.assertTrue(in_quiet_hours(cfg, now=datetime(2026, 1, 1, 23, 0)))
        self.assertFalse(in_quiet_hours(cfg, now=datetime(2026, 1, 1, 8, 0)))
        self.assertFalse(in_quiet_hours(cfg, now=datetime(2026, 1, 1, 12, 0)))

    def test_in_quiet_hours_fail_open(self):
        now = datetime(2026, 1, 1, 3)
        self.assertFalse(in_quiet_hours({}, now=now))
        self.assertFalse(in_quiet_hours(None, now=now))
        self.assertFalse(in_quiet_hours({"notify_quiet_hours": "oops"}, now=now))


# ---------- 闸门 1：合并通知 ----------

class _NotifyPlugin(NotificationTrackingMixin):
    def __init__(self, quiet="0,0"):
        self.config = {
            "notify_send_text": True,
            "notify_send_image": False,
            "notify_quiet_hours": quiet,
        }
        self.context = _Ctx()
        self._pending_end_notifications = {}
        self.group_monitor_enabled = {}

    def _get_notify_sessions(self, group_id, sid):
        return [f"test:GroupMessage:0_{group_id}"]


class QuietNotificationGateTests(unittest.IsolatedAsyncioTestCase):
    ALL_TYPES = [
        {"type": "start", "sid": "s1", "name": "A", "game": "CS2", "event_time": 1000},
        {"type": "end", "sid": "s1", "name": "A", "game": "CS2", "quit_time": 1000,
         "duration_str": "1小时"},
        {"type": "online", "sid": "s1", "name": "A", "event_time": 1000},
        {"type": "offline", "sid": "s1", "name": "A", "event_time": 1000,
         "online_minutes": 60},
        {"type": "owned", "sid": "s1", "name": "A", "game": "Dota 2", "gameid": "570",
         "added_at": 1000, "render": False},
    ]

    async def test_gate_blocks_all_notification_types(self):
        plugin = _NotifyPlugin()
        with patch(NOTIFY_PATCH, return_value=True):
            for noti in self.ALL_TYPES:
                self.assertEqual([], await plugin._send_merged_notification("1", [noti]))
        self.assertEqual([], plugin.context.sent)

    async def test_gate_keeps_sending_when_disabled(self):
        plugin = _NotifyPlugin("0,0")
        failed = await plugin._send_merged_notification(
            "1", [{"type": "online", "sid": "s1", "name": "A", "event_time": 1000}]
        )
        self.assertEqual([], failed)
        self.assertEqual(1, len(plugin.context.sent))

    async def test_blocked_send_does_not_mark_dedup(self):
        plugin = _NotifyPlugin()
        with patch(NOTIFY_PATCH, return_value=True):
            await plugin._send_merged_notification(
                "1", [{"type": "online", "sid": "s1", "name": "A", "event_time": 1000}]
            )
        # 闸门在 _should_send_notification 之前：去重表与 inflight 集合都未被创建
        self.assertFalse(hasattr(plugin, "_sent_notification_events"))
        self.assertFalse(hasattr(plugin, "_inflight_notification_events"))

    async def test_flush_drops_pending_without_replay(self):
        plugin = _NotifyPlugin()
        plugin._pending_end_notifications = {
            "1": [{"type": "online", "sid": "s1", "name": "A", "event_time": 1000}]
        }
        with patch(NOTIFY_PATCH, return_value=True):
            await plugin._flush_pending_end_notifications()
        self.assertEqual({}, plugin._pending_end_notifications)
        # 静默结束后再 flush：已丢弃，不补发
        await plugin._flush_pending_end_notifications()
        self.assertEqual([], plugin.context.sent)


# ---------- 闸门 2：成就 ----------

class _AchievementPlugin(AchievementTrackingMixin):
    def __init__(self, quiet="0,0"):
        self.config = {
            "notify_send_text": True,
            "notify_send_image": False,
            "notify_quiet_hours": quiet,
        }
        self.group_achievement_enabled = {"1": True}
        self.max_achievement_notifications = 5
        self.achievement_monitor = MagicMock()
        self.achievement_monitor.details_cache = {}
        self.achievement_monitor.get_achievement_details = AsyncMock(return_value={})
        self.API_KEY = "k"
        self.notify_sessions = {"1": "test:GroupMessage:0_1"}
        self.push_groups = {}
        self.context = _Ctx()


class QuietAchievementGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_achievement_returns_true_and_sends_nothing(self):
        plugin = _AchievementPlugin()
        with patch(ACH_PATCH, return_value=True):
            delivered = await plugin.notify_new_achievements(
                "1", "s1", "A", "570", "Dota 2", {"WIN_1"}
            )
        self.assertTrue(delivered)          # 视为已消费 → 调用方推进快照
        self.assertEqual([], plugin.context.sent)

    async def test_achievement_sends_when_not_quiet(self):
        plugin = _AchievementPlugin("0,0")
        delivered = await plugin.notify_new_achievements(
            "1", "s1", "A", "570", "Dota 2", {"WIN_1"}
        )
        self.assertTrue(delivered)
        self.assertEqual(1, len(plugin.context.sent))


# ---------- 闸门 3：网络波动 ----------

class _ResumePlugin:
    def __init__(self, quiet="0,0"):
        self.config = {
            "enable_network_fluctuation_notify": True,
            "notify_quiet_hours": quiet,
        }
        self.context = _Ctx()

    def _get_notify_sessions(self, group_id, sid):
        return [f"test:GroupMessage:0_{group_id}"]


class QuietResumeGateTests(unittest.IsolatedAsyncioTestCase):
    def _session(self):
        return SimpleNamespace(group_id="1", sid="s1", gameid="570")

    async def test_on_resumed_skips_when_quiet(self):
        plugin = _ResumePlugin()
        service = SessionService(plugin)
        with patch(RESUME_PATCH, return_value=True):
            service._on_resumed(self._session(), player_name="A", skip_push=False)
            await asyncio.sleep(0.01)
        self.assertEqual([], plugin.context.sent)

    async def test_on_resumed_sends_when_not_quiet(self):
        plugin = _ResumePlugin("0,0")
        service = SessionService(plugin)
        service._on_resumed(self._session(), player_name="A", skip_push=False)
        await asyncio.sleep(0.01)
        self.assertEqual(1, len(plugin.context.sent))


# ---------- 闸门 4：每日排行榜 ----------

class _RankPlugin(PollingTrackingMixin):
    def __init__(self, quiet):
        self.config = {"notify_quiet_hours": quiet}


class QuietRankGateTests(unittest.TestCase):
    def test_rank_push_blocked_in_window(self):
        plugin = _RankPlugin("0,8")
        self.assertFalse(plugin._rank_push_allowed(datetime(2026, 1, 1, 0, 1)))
        self.assertFalse(plugin._rank_push_allowed(datetime(2026, 1, 1, 7, 59)))

    def test_rank_push_allowed_outside_window(self):
        self.assertTrue(_RankPlugin("0,8")._rank_push_allowed(datetime(2026, 1, 1, 8, 0)))
        self.assertTrue(_RankPlugin("0,0")._rank_push_allowed(datetime(2026, 1, 1, 3, 0)))
        self.assertTrue(_RankPlugin("23,8")._rank_push_allowed(datetime(2026, 1, 1, 12, 0)))


if __name__ == "__main__":
    unittest.main()
