"""简洁合并播报：分页、去重键、跳过详情富化、仅图片模式误判修复。"""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from src.application.services.notification_tracking import NotificationTrackingMixin
from src.application.services.owned_games import (
    BATCH_KIND,
    OwnedGamesService,
    paginate_compact_entries,
)

SID = "76561198000000001"


def games(*appids):
    return [{"appid": appid, "playtime_forever": 0} for appid in appids]


class PluginStub:
    """最小插件桩：只暴露简洁模式需要的接口，价格管线故意做成会炸的探针。"""

    def __init__(self, data_dir, config=None):
        self.data_dir = data_dir
        self.config = {
            "enable_owned_games_notify": True,
            "owned_games_poll_interval": 1800,
            "owned_games_notify_style": "compact",
        }
        self.config.update(config or {})
        self.running_groups = {"111"}
        self.group_monitor_enabled = {"111": True}
        self.group_steam_ids = {"111": [SID]}
        self.group_last_states = {}
        self._bind_data = {}
        self.push_groups = {}
        self.price_query = None
        self.fetch_queue = []
        self.sent = []
        self.send_should_fail = False

    async def fetch_owned_games(self, sid, include_appinfo=False, include_played_free_games=True, retry=None):
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


class PaginateTests(unittest.TestCase):
    def test_splits_into_pages(self):
        chunks = paginate_compact_entries(list(range(17)), 8)
        self.assertEqual([len(chunk) for chunk in chunks], [8, 8, 1])

    def test_empty_returns_single_empty_page(self):
        self.assertEqual(paginate_compact_entries([], 8), [[]])

    def test_page_size_is_clamped(self):
        self.assertEqual([len(c) for c in paginate_compact_entries(list(range(30)), 999)], [20, 10])
        self.assertEqual(len(paginate_compact_entries(list(range(5)), 0)), 5)

    def test_max_pages_truncates(self):
        chunks = paginate_compact_entries(list(range(10)), 2, max_pages=2)
        self.assertEqual(2, len(chunks))


class CompactFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = self._tmp.name
        self.plugin = PluginStub(self.data_dir)
        self.service = OwnedGamesService(self.plugin)

    def _notis(self):
        return [noti for _group, notis in self.plugin.sent for noti in notis]

    async def test_compact_never_enriches_card(self):
        self.plugin.price_query = AsyncMock()
        self.plugin.price_query.build_card = AsyncMock()
        self.plugin.fetch_queue = [games(1), games(1, 2, 3)]

        with patch.object(
            OwnedGamesService, "_enrich_card", new=AsyncMock(return_value=None)
        ) as enrich:
            await self.service.sync_all(baseline_only=True)
            await self.service.sync_all()

        self.assertEqual(0, enrich.await_count)
        self.assertEqual(0, self.plugin.price_query.build_card.await_count)

    async def test_compact_paginates_and_shares_added_at(self):
        self.plugin.config["owned_games_compact_page_size"] = 8
        # 基线 [1]，随后 1..18 → 新增 17 款
        self.plugin.fetch_queue = [games(1), games(*range(1, 19))]

        await self.service.sync_all(baseline_only=True)
        await self.service.sync_all()

        notis = self._notis()
        self.assertEqual(3, len(notis))
        self.assertEqual([8, 8, 1], [len(n["items"]) for n in notis])
        self.assertEqual({3}, {n["pages"] for n in notis})
        self.assertEqual(1, len({n["added_at"] for n in notis}))
        self.assertEqual({17}, {n["added_count"] for n in notis})

    async def test_compact_noti_shape(self):
        self.plugin.fetch_queue = [games(1), games(1, 570)]

        await self.service.sync_all(baseline_only=True)
        await self.service.sync_all()

        noti = self._notis()[0]
        self.assertEqual(BATCH_KIND, noti["type"])
        self.assertEqual(1, noti["page"])
        self.assertEqual(1, noti["pages"])
        self.assertIs(True, noti["render"])
        self.assertNotIn("gameid", noti)
        self.assertEqual("570", noti["items"][0]["appid"])

    async def test_compact_ignores_max_per_notify(self):
        self.plugin.config["owned_games_max_per_notify"] = 1
        self.plugin.config["owned_games_compact_page_size"] = 5
        self.plugin.fetch_queue = [games(1), games(1, 2, 3, 4, 5, 6)]

        await self.service.sync_all(baseline_only=True)
        await self.service.sync_all()

        self.assertEqual(1, len(self._notis()))  # 6 款 / 每页 5 → 2 页，被 max_pages 默认 3 放行
        self.assertEqual([5], [len(n["items"]) for n in self._notis()])

    async def test_compact_max_pages_drops_extra_but_keeps_recent(self):
        self.plugin.config["owned_games_compact_page_size"] = 2
        self.plugin.config["owned_games_compact_max_pages"] = 2
        # 基线 [1]，随后 1..11 → 新增 10 款 → 5 页，截断到 2 页
        self.plugin.fetch_queue = [games(1), games(*range(1, 12))]

        await self.service.sync_all(baseline_only=True)
        await self.service.sync_all()

        self.assertEqual(2, len(self._notis()))
        state = self.service.players[SID]
        self.assertEqual(10, len(state["recent"]))

    async def test_compact_failed_page_only_retries_that_page(self):
        self.plugin.config["owned_games_compact_page_size"] = 2
        self.plugin.fetch_queue = [games(1), games(*range(1, 7))]
        await self.service.sync_all(baseline_only=True)

        original = self.plugin._send_merged_notification

        async def flaky(group_id, notifications):
            if notifications[0].get("page") == 2:
                self.plugin.sent.append((str(group_id), [dict(n) for n in notifications]))
                return [dict(n, _target_session=f"session:{group_id}") for n in notifications]
            return await original(group_id, notifications)

        self.plugin._send_merged_notification = flaky
        await self.service.sync_all()

        pending = self.service.players[SID]["pending"]
        self.assertEqual(1, len(pending))
        self.assertEqual(2, pending[0]["page"])

        self.plugin._send_merged_notification = original
        delivered = await self.service.retry_pending()
        self.assertEqual(1, delivered)
        self.assertEqual([], self.service.players[SID]["pending"])

    async def test_compact_pages_agree_on_totals(self):
        """页码与计数必须自洽：pages 是真实总页数，不受 max_pages 截断影响。"""
        self.plugin.config["owned_games_compact_page_size"] = 2
        self.plugin.config["owned_games_compact_max_pages"] = 2
        self.plugin.fetch_queue = [games(1), games(*range(1, 12))]  # 新增 10 款

        await self.service.sync_all(baseline_only=True)
        await self.service.sync_all()

        notis = self._notis()
        self.assertEqual(2, len(notis))  # 只发 2 页
        self.assertEqual({5}, {n["pages"] for n in notis})  # 但真实共 5 页
        self.assertEqual({10}, {n["added_count"] for n in notis})
        self.assertEqual({4}, {n["shown_count"] for n in notis})  # 实际展示 4 款
        self.assertEqual({2}, {n["page_size"] for n in notis})

    async def test_batch_noti_carries_page_size_for_numbering(self):
        """渲染器靠 page_size 推算跨页连续序号，必须随 noti 透传。"""
        self.plugin.config["owned_games_compact_page_size"] = 8
        self.plugin.fetch_queue = [games(1), games(*range(1, 19))]

        await self.service.sync_all(baseline_only=True)
        await self.service.sync_all()

        for noti in self._notis():
            self.assertEqual(8, noti["page_size"])

    async def test_batch_page_size_survives_persistence(self):
        self.plugin.config["owned_games_compact_page_size"] = 3
        self.plugin.fetch_queue = [games(1), games(*range(1, 9))]
        await self.service.sync_all(baseline_only=True)

        original = self.plugin._send_merged_notification

        async def always_fail(group_id, notifications):
            self.plugin.sent.append((str(group_id), [dict(n) for n in notifications]))
            return [dict(n, _target_session=f"session:{group_id}") for n in notifications]

        self.plugin._send_merged_notification = always_fail
        await self.service.sync_all()
        self.plugin._send_merged_notification = original

        self.service.store.flush()
        from src.infrastructure.persistence.owned_games_store import OwnedGamesStore
        reloaded = OwnedGamesStore(self.service.store.data_dir)
        pending = reloaded.players[SID]["pending"]
        self.assertTrue(pending)
        for item in pending:
            self.assertEqual("owned_batch", item["kind"])
            self.assertGreater(item["page_size"], 0)
            self.assertGreater(item["pages"], 0)

    async def test_detailed_style_keeps_render_flag_semantics(self):
        self.plugin.config["owned_games_notify_style"] = "detailed"
        self.plugin.config["owned_games_max_per_notify"] = 1
        # 基线 [1]，随后新增 2、3 两款 → 上限 1 张卡，第二款 render=False
        self.plugin.fetch_queue = [games(1), games(1, 2, 3)]

        await self.service.sync_all(baseline_only=True)
        await self.service.sync_all()

        notis = self._notis()
        self.assertEqual("owned", notis[0]["type"])
        self.assertEqual([True, False], [n["render"] for n in notis])

    async def test_invalid_style_falls_back_to_detailed(self):
        self.plugin.config["owned_games_notify_style"] = "compactt"
        self.assertEqual("detailed", self.service._style())


class _NotifyStub(NotificationTrackingMixin):
    def __init__(self, config):
        self.config = config
        self.data_dir = "."
        self.API_KEY = "k"
        self.SGDB_API_KEY = ""
        self.SGDB_API_BASE = ""
        self.STEAM_STORE_BASE = ""
        self.STEAM_API_BASE = ""
        self.proxy = None
        self._plugin_version = "4.12.0"
        self.context = self
        self.messages = []

    async def send_message(self, session, chain):
        self.messages.append((session, chain))

    def _get_notify_sessions(self, group_id, sid):
        return [f"session:{group_id}"]


class ImageOnlyRegressionTests(unittest.IsolatedAsyncioTestCase):
    """issue #66：仅图片模式下，主动不渲染的条目不得被误判为发送失败。"""

    IMAGE_ONLY = {"notify_send_text": False, "notify_send_image": True}

    async def test_owned_without_render_is_consumed(self):
        stub = _NotifyStub(dict(self.IMAGE_ONLY))
        noti = {
            "type": "owned", "sid": "s1", "gameid": "570", "added_at": 1,
            "name": "阿猫", "game": "Dota 2", "owned_count": 268, "render": False,
        }

        failed = await stub._send_merged_notification("111", [noti])

        self.assertEqual([], failed)  # 修复前这里会返回 1 条并进补发队列

    async def test_owned_render_failure_still_retries(self):
        stub = _NotifyStub(dict(self.IMAGE_ONLY))
        noti = {
            "type": "owned", "sid": "s1", "gameid": "570", "added_at": 1,
            "name": "阿猫", "game": "Dota 2", "owned_count": 268, "render": True,
        }

        with patch.object(
            NotificationTrackingMixin, "_render_notification_image", new=AsyncMock(return_value=None)
        ):
            failed = await stub._send_merged_notification("111", [noti])

        self.assertEqual(1, len(failed))

    async def test_batch_noti_renders_via_batch_renderer(self):
        stub = _NotifyStub({"notify_send_text": True, "notify_send_image": False})
        noti = {
            "type": BATCH_KIND, "sid": "s1", "name": "阿猫", "added_at": 1,
            "page": 2, "pages": 5, "added_count": 37, "owned_count": 268,
            "items": [{"appid": "570", "name": "Dota 2"}], "render": True,
        }

        with patch(
            "src.application.services.notification_tracking.render_owned_games_batch",
            new=AsyncMock(return_value=b"png"),
        ) as batch, patch(
            "src.application.services.notification_tracking.render_owned_game",
            new=AsyncMock(return_value=b"png"),
        ) as single:
            path = await stub._render_notification_image(noti)

        self.assertEqual(1, batch.await_count)
        self.assertEqual(0, single.await_count)
        self.assertIsNotNone(path)
        import os
        os.unlink(path)

    async def test_batch_event_key_distinguishes_pages(self):
        stub = _NotifyStub(dict(self.IMAGE_ONLY))
        base = {"type": BATCH_KIND, "sid": "s1", "added_at": 100, "pages": 3}
        first = stub._notification_event_key(dict(base, page=1), "session:111")
        second = stub._notification_event_key(dict(base, page=2), "session:111")

        self.assertNotEqual(first, second)
        self.assertEqual(5, len(first))

    async def test_owned_event_key_shape_unchanged(self):
        stub = _NotifyStub(dict(self.IMAGE_ONLY))
        key = stub._notification_event_key(
            {"type": "owned", "sid": "s1", "gameid": "570", "added_at": 123}, "session:111"
        )
        self.assertEqual(("session:111", "s1", "570", "owned", 123), key)


if __name__ == "__main__":
    unittest.main()
