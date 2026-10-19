"""`/steam achievement_blacklist` 列表、分页与删除。"""
import json
import os
import tempfile
import unittest

from src.application.services.achievement_tracking import AchievementTrackingMixin
from src.infrastructure.persistence.achievement_blacklist_store import (
    REASON_FETCH_FAILED,
    REASON_LEGACY,
    REASON_NO_STATS,
    SOURCE_AUTO,
    SOURCE_LEGACY,
    AchievementBlacklistStore,
)
from src.presentation.commands.achievement_admin import achievement_blacklist


class _Event:
    def __init__(self, message):
        self.message_str = message
        self.texts = []

    def get_message_str(self):
        return self.message_str

    def plain_result(self, text):
        self.texts.append(text)
        return text


class _MonitorStub:
    """与 AchievementMonitor 的黑名单 API 对齐。"""

    def __init__(self, store):
        self.blacklist_store = store
        self.details_cache = {}

    def list_entries(self):
        return self.blacklist_store.items()

    def remove_appids(self, appids):
        return self.blacklist_store.remove(appids)

    def clear_blacklist(self):
        return self.blacklist_store.clear()

    def is_blacklisted(self, appid):
        return self.blacklist_store.contains(appid)


class _Plugin(AchievementTrackingMixin):
    def __init__(self, monitor):
        self.achievement_monitor = monitor
        self.achievement_fail_count = {
            ("730", "2026-10-19"): 3,
            ("570", "2026-10-19"): 1,
        }
        self.achievement_snapshots = {}


async def _run(plugin, message, action="", value=""):
    event = _Event(message)
    texts = [item async for item in achievement_blacklist(plugin, event, action, value)]
    return event, texts


class AchievementBlacklistCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = self._tmp.name
        self.path = os.path.join(self.data_dir, "achievement_blacklist.json")

    def tearDown(self):
        self._tmp.cleanup()

    def _store(self, entries):
        with open(self.path, "w", encoding="utf-8") as stream:
            json.dump({"version": 2, "updated_at": 0, "entries": entries}, stream, ensure_ascii=False)
        return AchievementBlacklistStore(self.data_dir)

    def _plugin(self, store):
        return _Plugin(_MonitorStub(store))

    async def test_empty_list(self):
        store = self._store({})
        plugin = self._plugin(store)
        _, texts = await _run(plugin, "/steam achievement_blacklist")
        self.assertIn("成就黑名单为空", texts[0])
        self.assertIn("del all", texts[0])

    async def test_first_page_has_20_rows(self):
        entries = {str(1000 + i): {"game_name": f"G{i}", "added_at": 100 + i,
                                   "reason": REASON_NO_STATS, "source": SOURCE_AUTO}
                   for i in range(45)}
        store = self._store(entries)
        plugin = self._plugin(store)
        _, texts = await _run(plugin, "/steam achievement_blacklist")
        lines = texts[0].splitlines()
        rows = [line for line in lines if line[:1].isdigit() and ". " in line]
        self.assertEqual(20, len(rows))
        self.assertIn("共 45 条，第 1/3 页", texts[0])
        self.assertIn("list 2", texts[0])

    async def test_second_page_offsets_numbering(self):
        entries = {str(1000 + i): {"game_name": "", "added_at": 100 + i,
                                   "reason": REASON_NO_STATS, "source": SOURCE_AUTO}
                   for i in range(25)}
        store = self._store(entries)
        plugin = self._plugin(store)
        _, texts = await _run(plugin, "/steam achievement_blacklist list 2")
        lines = texts[0].splitlines()
        rows = [line for line in lines if line[:1].isdigit() and ". " in line]
        self.assertEqual(5, len(rows))
        self.assertTrue(rows[0].startswith("21. "))

    async def test_page_overflow_clamped(self):
        entries = {str(1000 + i): {"game_name": "", "added_at": 100 + i,
                                   "reason": REASON_NO_STATS, "source": SOURCE_AUTO}
                   for i in range(25)}
        store = self._store(entries)
        plugin = self._plugin(store)
        _, texts = await _run(plugin, "/steam achievement_blacklist list 99")
        self.assertIn("第 2/2 页", texts[0])

    async def test_invalid_page_rejected(self):
        store = self._store({})
        plugin = self._plugin(store)
        _, texts = await _run(plugin, "/steam achievement_blacklist list x")
        self.assertIn("页码应为正整数", texts[0])

    async def test_legacy_row_shows_unknown_time(self):
        store = self._store({"1905180": {"game_name": "OBS Studio", "added_at": 0,
                                        "reason": REASON_LEGACY, "source": SOURCE_LEGACY},
                             "3578660": {"game_name": "", "added_at": 0,
                                        "reason": REASON_LEGACY, "source": SOURCE_LEGACY}})
        plugin = self._plugin(store)
        _, texts = await _run(plugin, "/steam achievement_blacklist")
        self.assertIn("1905180 | OBS Studio | 时间未知 | 历史遗留", texts[0])
        self.assertIn("3578660 | 名称未知 | 时间未知 | 历史遗留", texts[0])

    async def test_pipe_in_name_escaped(self):
        store = self._store({"730": {"game_name": "A|B", "added_at": 1,
                                     "reason": REASON_NO_STATS, "source": SOURCE_AUTO}})
        plugin = self._plugin(store)
        _, texts = await _run(plugin, "/steam achievement_blacklist")
        self.assertIn("A／B", texts[0])

    async def test_delete_single_purges_caches(self):
        store = self._store({"730": {"game_name": "CS2", "added_at": 1,
                                     "reason": REASON_NO_STATS, "source": SOURCE_AUTO},
                             "570": {"game_name": "Dota", "added_at": 2,
                                     "reason": REASON_NO_STATS, "source": SOURCE_AUTO}})
        plugin = self._plugin(store)
        plugin.achievement_monitor.details_cache = {("g1", "730"): {"x": 1}, ("g1", "570"): {}}

        _, texts = await _run(plugin, "/steam achievement_blacklist del 730")

        self.assertNotIn("730", store.entries)
        self.assertNotIn(("730", "2026-10-19"), plugin.achievement_fail_count)
        self.assertNotIn(("g1", "730"), plugin.achievement_monitor.details_cache)
        self.assertIn("已从成就黑名单删除：730", texts[0])
        self.assertIn("剩余 1 条", texts[0])

    async def test_delete_multiple_with_missing(self):
        store = self._store({"730": {"game_name": "", "added_at": 1,
                                     "reason": REASON_NO_STATS, "source": SOURCE_AUTO},
                             "570": {"game_name": "", "added_at": 2,
                                     "reason": REASON_NO_STATS, "source": SOURCE_AUTO}})
        plugin = self._plugin(store)
        _, texts = await _run(plugin, "/steam achievement_blacklist del 730,999")
        self.assertIn("不在名单中：999", texts[0])
        self.assertEqual({"570"}, set(store.entries))

    async def test_delete_missing_does_not_write(self):
        store = self._store({"730": {"game_name": "", "added_at": 1,
                                     "reason": REASON_NO_STATS, "source": SOURCE_AUTO}})
        plugin = self._plugin(store)
        before = os.path.getmtime(self.path)
        _, texts = await _run(plugin, "/steam achievement_blacklist del 999")
        self.assertEqual(before, os.path.getmtime(self.path))
        self.assertIn("不在成就黑名单中", texts[0])

    async def test_delete_three_space_separated_ids(self):
        store = self._store({
            "730": {"game_name": "", "added_at": 1, "reason": REASON_NO_STATS, "source": SOURCE_AUTO},
            "570": {"game_name": "", "added_at": 2, "reason": REASON_NO_STATS, "source": SOURCE_AUTO},
            "431960": {"game_name": "", "added_at": 3, "reason": REASON_NO_STATS, "source": SOURCE_AUTO},
        })
        plugin = self._plugin(store)
        await _run(plugin, "/steam achievement_blacklist del 730 570 431960",
                   action="730", value="570")
        self.assertEqual({}, store.entries)

    async def test_delete_all(self):
        entries = {str(1000 + i): {"game_name": "", "added_at": 1,
                                   "reason": REASON_NO_STATS, "source": SOURCE_AUTO}
                   for i in range(5)}
        store = self._store(entries)
        plugin = self._plugin(store)
        plugin.achievement_fail_count = {("1000", "2026-10-19"): 2}

        _, texts = await _run(plugin, "/steam achievement_blacklist del all")

        self.assertEqual({}, store.entries)
        self.assertEqual({}, plugin.achievement_fail_count)
        self.assertIn("共移除 5 条", texts[0])

    async def test_delete_all_mixed_rejected(self):
        store = self._store({"730": {"game_name": "", "added_at": 1,
                                     "reason": REASON_NO_STATS, "source": SOURCE_AUTO}})
        plugin = self._plugin(store)
        _, texts = await _run(plugin, "/steam achievement_blacklist del all 730")
        self.assertIn("不能与其他 AppID 同时使用", texts[0])
        self.assertIn("730", store.entries)

    async def test_invalid_token_rejects_whole(self):
        store = self._store({"730": {"game_name": "", "added_at": 1,
                                     "reason": REASON_NO_STATS, "source": SOURCE_AUTO}})
        plugin = self._plugin(store)
        _, texts = await _run(plugin, "/steam achievement_blacklist del 730,abc")
        self.assertIn("无法识别为 AppID", texts[0])
        self.assertIn("730", store.entries)

    async def test_steamid64_hint(self):
        store = self._store({})
        plugin = self._plugin(store)
        _, texts = await _run(plugin, "/steam achievement_blacklist del 76561198099315021")
        self.assertIn("SteamID64", texts[0])

    async def test_alias_forms_parse(self):
        store = self._store({"730": {"game_name": "CS2", "added_at": 1,
                                     "reason": REASON_NO_STATS, "source": SOURCE_AUTO}})
        plugin = self._plugin(store)
        for command in ("/steam achbl", "/steam ABL", "/steam achievement_bl", "/steam abl"):
            _, texts = await _run(plugin, command)
            self.assertIn("成就黑名单（共 1 条", texts[0], command)

    async def test_delete_without_ids_shows_usage(self):
        store = self._store({})
        plugin = self._plugin(store)
        _, texts = await _run(plugin, "/steam achievement_blacklist del")
        self.assertIn("用法：/steam achievement_blacklist del", texts[0])

    async def test_unknown_subcommand_usage(self):
        store = self._store({})
        plugin = self._plugin(store)
        _, texts = await _run(plugin, "/steam achievement_blacklist foo")
        self.assertIn("未知子命令", texts[0])

    async def test_fetch_failed_reason_label(self):
        store = self._store({"730": {"game_name": "CS2", "added_at": 1700000000,
                                     "reason": REASON_FETCH_FAILED, "source": SOURCE_AUTO}})
        plugin = self._plugin(store)
        _, texts = await _run(plugin, "/steam achievement_blacklist")
        self.assertIn("当天失败10次", texts[0])


if __name__ == "__main__":
    unittest.main()
