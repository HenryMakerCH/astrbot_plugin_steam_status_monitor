"""OwnedGamesService：基线、差分、隐私防护、抖动抑制、去重、补发与重启续播。"""
import unittest

from src.application.services.owned_games import OwnedGamesService
from src.infrastructure.clients.steam import (
    OwnedGamesInvisible,
    OwnedGamesRateLimited,
)
from src.infrastructure.persistence.owned_games_store import OwnedGamesStore

SID = "76561198000000001"


def games(*appids):
    return [{"appid": appid, "playtime_forever": 0} for appid in appids]


class PluginStub:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.config = {
            "enable_owned_games_notify": True,
            "owned_games_poll_interval": 1800,
        }
        self.running_groups = {"111"}
        self.group_monitor_enabled = {"111": True}
        self.group_steam_ids = {"111": [SID]}
        self.group_last_states = {}
        self._bind_data = {}
        self.push_groups = {}
        self.price_query = None
        self.fetch_queue = []
        self.fetch_calls = []
        self.sent = []
        self.send_should_fail = False

    async def fetch_owned_games(self, sid, include_appinfo=False, include_played_free_games=True, retry=None):
        self.fetch_calls.append({
            "sid": str(sid),
            "include_appinfo": include_appinfo,
            "include_played_free_games": include_played_free_games,
        })
        if not self.fetch_queue:
            raise AssertionError("fetch_owned_games 调用次数超出预期")
        item = self.fetch_queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def get_game_names(self, gameid, fallback_name=None):
        return (f"游戏{gameid}", f"Game{gameid}")

    def _resolve_bind_name(self, sid, steam_name=None):
        return steam_name or f"玩家{sid}"

    async def _send_merged_notification(self, group_id, notifications):
        self.sent.append((str(group_id), [dict(n) for n in notifications]))
        if self.send_should_fail:
            return [dict(n, _target_session=f"session:{group_id}") for n in notifications]
        return []


class OwnedGamesServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = self._tmp.name
        self.plugin = PluginStub(self.data_dir)
        self.service = OwnedGamesService(self.plugin)

    def _sent_notifications(self):
        return [noti for _group, notis in self.plugin.sent for noti in notis]

    async def test_first_sync_builds_baseline_without_announce(self):
        self.plugin.fetch_queue = [games(1, 2)]

        summary = await self.service.sync_all(baseline_only=True)

        state = self.service.players[SID]
        self.assertEqual(state["ids"], [1, 2])
        self.assertGreater(state["baseline_at"], 0)
        self.assertEqual(summary["baselines"], 1)
        self.assertEqual(self.plugin.sent, [])

    async def test_new_game_detected_and_announced(self):
        self.plugin.fetch_queue = [games(1, 2), games(1, 2, 3)]
        await self.service.sync_all(baseline_only=True)

        summary = await self.service.sync_all()

        self.assertEqual(summary["added"], 1)
        notis = self._sent_notifications()
        self.assertEqual(len(notis), 1)
        self.assertEqual(notis[0]["type"], "owned")
        self.assertEqual(notis[0]["gameid"], "3")
        self.assertEqual(notis[0]["game"], "游戏3")
        self.assertEqual(notis[0]["owned_count"], 3)
        self.assertEqual(self.service.players[SID]["pending"], [])
        self.assertEqual(self.service.players[SID]["recent"][-1]["name"], "游戏3")

    async def test_invisible_response_keeps_baseline(self):
        self.plugin.fetch_queue = [games(1, 2), OwnedGamesInvisible("不可见")]

        await self.service.sync_all(baseline_only=True)
        summary = await self.service.sync_all()

        self.assertEqual(summary["failed"], 1)
        self.assertEqual(self.service.players[SID]["ids"], [1, 2])
        self.assertEqual(self.plugin.sent, [])

    async def test_rate_limited_sets_global_backoff(self):
        self.plugin.fetch_queue = [OwnedGamesRateLimited("限流")]

        summary = await self.service.sync_all()

        self.assertEqual(summary["failed"], 1)
        self.assertGreater(self.service._backoff_until, 0)

    async def test_grace_suppresses_reappear(self):
        self.plugin.fetch_queue = [games(1, 2), games(1), games(1, 2)]

        await self.service.sync_all(baseline_only=True)
        await self.service.sync_all()  # 2 被移除 → 记入 grace
        self.assertIn("2", self.service.players[SID]["grace"])

        summary = await self.service.sync_all()  # 2 抖动重现 → 不再播报
        self.assertEqual(summary["added"], 0)
        self.assertEqual(self.plugin.sent, [])

    async def test_multi_group_single_fetch_and_multi_route(self):
        self.plugin.group_steam_ids = {"111": [SID], "222": [SID]}
        self.plugin.running_groups = {"111", "222"}
        self.plugin.group_monitor_enabled = {"111": True, "222": True}
        self.plugin.fetch_queue = [games(1), games(1, 9)]

        await self.service.sync_all(baseline_only=True)
        self.assertEqual(len(self.plugin.fetch_calls), 1)  # 同 sid 只拉一次

        await self.service.sync_all()
        self.assertEqual(len(self.plugin.fetch_calls), 2)
        routed_groups = sorted(group for group, _notis in self.plugin.sent)
        self.assertEqual(routed_groups, ["111", "222"])

    async def test_failed_delivery_kept_in_pending_then_retried(self):
        self.plugin.fetch_queue = [games(1), games(1, 2)]
        await self.service.sync_all(baseline_only=True)

        self.plugin.send_should_fail = True
        await self.service.sync_all()
        state = self.service.players[SID]
        self.assertEqual(len(state["pending"]), 1)
        self.assertEqual(len(state["pending"][0]["deliveries"]), 1)

        self.plugin.send_should_fail = False
        delivered = await self.service.retry_pending()
        self.assertEqual(delivered, 1)
        self.assertEqual(self.service.players[SID]["pending"], [])
        self.assertEqual(len(self.plugin.sent), 2)

    async def test_pending_survives_restart_and_retries(self):
        self.plugin.fetch_queue = [games(1), games(1, 2)]
        await self.service.sync_all(baseline_only=True)
        self.plugin.send_should_fail = True
        await self.service.sync_all()
        self.service.store.flush()

        restarted_plugin = PluginStub(self.data_dir)
        restarted = OwnedGamesService(restarted_plugin)

        delivered = await restarted.retry_pending()

        self.assertEqual(delivered, 1)
        self.assertEqual(len(restarted_plugin.sent), 1)
        self.assertEqual(restarted.players[SID]["pending"], [])

    async def test_disabled_switch_skips_announce_but_advances_baseline(self):
        self.plugin.fetch_queue = [games(1), games(1, 2)]
        await self.service.sync_all(baseline_only=True)

        self.plugin.config["enable_owned_games_notify"] = False
        await self.service.sync_all()

        self.assertEqual(self.plugin.sent, [])
        self.assertEqual(self.service.players[SID]["pending"], [])
        self.assertEqual(self.service.players[SID]["ids"], [1, 2])

    async def test_filter_blacklist_and_ignore_appids(self):
        self.plugin.config["game_filter_mode"] = "黑名单"
        self.plugin.config["game_filter_ids"] = "3"
        self.plugin.config["owned_games_ignore_appids"] = "4"
        self.plugin.fetch_queue = [games(1), games(1, 3, 4)]

        await self.service.sync_all(baseline_only=True)
        summary = await self.service.sync_all()

        self.assertEqual(summary["added"], 0)
        self.assertEqual(self.plugin.sent, [])
        self.assertEqual(self.service.players[SID]["ids"], [1, 3, 4])

    async def test_max_per_notify_caps_render_flag(self):
        self.plugin.config["owned_games_max_per_notify"] = 1
        self.plugin.fetch_queue = [games(1), games(1, 2, 3)]

        await self.service.sync_all(baseline_only=True)
        await self.service.sync_all()

        notis = self._sent_notifications()
        self.assertEqual(len(notis), 2)
        self.assertEqual([noti["render"] for noti in notis], [True, False])

    async def test_sync_group_with_report_baseline_only(self):
        self.plugin.group_steam_ids = {"111": [SID, "76561198000000002"]}
        self.plugin.fetch_queue = [games(1), games(2)]

        result = await self.service.sync_group_with_report("111")

        self.assertEqual(result, {"players": 2, "baselines": 2, "added": 0, "failed": 0})
        self.assertEqual(self.plugin.sent, [])

    async def test_budget_available_resets_on_new_day(self):
        self.service._day_key = "old-day"
        self.service._day_calls = 999999

        self.assertTrue(self.service._budget_available())  # 跨天自动重置

        self.service._day_calls = 3000
        self.assertFalse(self.service._budget_available())

    async def test_list_for_sender_requires_binding(self):
        result = await self.service.list_for_sender("10001")
        self.assertIn("error", result)

    async def test_list_for_sender_uses_snapshot_then_live_search(self):
        self.plugin.fetch_queue = [games(1, 2)]
        await self.service.sync_all(baseline_only=True)
        self.plugin._bind_data = {"10001": {"sid": SID, "nickname": "阿猫"}}

        summary = await self.service.list_for_sender("10001")
        self.assertEqual(summary["total_owned"], 2)
        self.assertFalse(self.plugin.fetch_calls[-1]["include_appinfo"])

        self.plugin.fetch_queue = [[
            {"appid": 1, "name": "Dota 2"},
            {"appid": 2, "name": "传送门"},
        ]]
        found = await self.service.list_for_sender("10001", query="dota")
        self.assertEqual(found["matched"], 1)
        self.assertEqual(found["games"][0]["name"], "Dota 2")
        self.assertTrue(self.plugin.fetch_calls[-1]["include_appinfo"])

    async def test_forget_and_clear(self):
        self.plugin.fetch_queue = [games(1)]
        await self.service.sync_all(baseline_only=True)

        self.assertTrue(self.service.forget(SID))
        self.assertEqual(self.service.players, {})
        self.service.players["x"] = {"ids": []}
        self.service.clear()
        self.assertEqual(self.service.players, {})

    async def test_admin_flush_helpers_persist_immediately(self):
        self.plugin.fetch_queue = [games(1)]
        await self.service.sync_all(baseline_only=True)

        self.assertTrue(self.service.forget_and_flush(SID))
        self.assertEqual(OwnedGamesStore(self.data_dir).players, {})

        self.service.players["x"] = OwnedGamesStore.new_state()
        self.assertEqual(self.service.prune_orphans(), ["x"])
        self.assertEqual(OwnedGamesStore(self.data_dir).players, {})

        self.service.players["y"] = OwnedGamesStore.new_state()
        self.service.clear_and_flush()
        self.assertEqual(OwnedGamesStore(self.data_dir).players, {})


if __name__ == "__main__":
    unittest.main()
