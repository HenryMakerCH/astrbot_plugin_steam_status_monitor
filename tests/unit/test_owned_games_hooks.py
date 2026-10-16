"""名单删除/清空时联动清理游戏库快照（monitor_admin 钩子）。"""
import unittest

from src.application.services.monitor_admin import MonitorAdminService
from src.domain.monitoring import MonitorStateStore


class _OwnedStub:
    def __init__(self):
        self.forgotten = []
        self.pruned = 0
        self.cleared = 0

    def forget_and_flush(self, sid):
        self.forgotten.append(str(sid))
        return True

    def prune_orphans(self):
        self.pruned += 1
        return []

    def clear_and_flush(self):
        self.cleared += 1


class _PluginStub:
    max_group_size = 5

    def __init__(self, groups, push_groups=None):
        self.monitor_state = MonitorStateStore(group_steam_ids=groups)
        self.push_groups = push_groups or {}
        self._bind_data = {}
        self.group_monitor_enabled = {}
        self.group_achievement_enabled = {}
        self.running_groups = set()
        self.notify_sessions = {}
        self.achievement_poll_tasks = {}
        self.achievement_snapshots = {}
        self.achievement_fail_count = {}
        self._session_meta = {}
        self.config = {}
        self.owned_games = _OwnedStub()

    @property
    def group_steam_ids(self):
        return self.monitor_state.group_steam_ids

    @property
    def group_last_states(self):
        return self.monitor_state.group_last_states

    @property
    def group_last_quit_times(self):
        return self.monitor_state.group_last_quit_times

    @property
    def group_pending_logs(self):
        return self.monitor_state.group_pending_logs

    @property
    def group_recent_games(self):
        return self.monitor_state.group_recent_games

    @property
    def playing_sessions(self):
        return self.monitor_state.playing_sessions

    @property
    def next_poll_time(self):
        return self.monitor_state.next_poll_time

    @property
    def _pending_end_notifications(self):
        return self.monitor_state.pending_end_notifications

    def _save_group_steam_ids(self):
        pass

    def _save_push_groups(self):
        pass

    def _save_persistent_data(self, force=False):
        pass

    def _save_bind_data(self):
        pass

    def _save_notify_session(self):
        pass

    def _save_group_switches(self):
        pass

    @property
    def session_service(self):
        class _Stub:
            def discard_player(self, steam_id):
                pass

            def discard_group(self, group_id):
                pass

        return _Stub()


class OwnedGamesCleanupHookTests(unittest.TestCase):
    def test_full_delete_forgets_snapshot(self):
        plugin = _PluginStub({"111": ["s1"]}, {})
        service = MonitorAdminService(plugin)

        result = service.remove_player("111", "s1")

        self.assertTrue(result.changed)
        self.assertEqual(plugin.owned_games.forgotten, ["s1"])

    def test_push_route_removal_keeps_snapshot(self):
        plugin = _PluginStub({"111": ["s1"]}, {"s1": ["222"]})
        service = MonitorAdminService(plugin)

        service.remove_player("222", "s1")

        self.assertEqual(plugin.owned_games.forgotten, [])

    def test_promotion_keeps_snapshot(self):
        plugin = _PluginStub({"111": ["s1"]}, {"s1": ["222"]})
        service = MonitorAdminService(plugin)

        result = service.remove_player("111", "s1")

        self.assertIn("promoted", result.message)
        self.assertEqual(plugin.owned_games.forgotten, [])

    def test_remove_group_prunes_orphan_snapshots(self):
        plugin = _PluginStub({"111": ["s1"]}, {})
        service = MonitorAdminService(plugin)

        service.remove_group("111")

        self.assertEqual(plugin.owned_games.pruned, 1)

    def test_clear_all_ids_clears_snapshots(self):
        plugin = _PluginStub({"111": ["s1"]}, {})
        plugin.monitor_state.group_last_states = {"111": {"s1": {"gameid": "1"}}}
        service = MonitorAdminService(plugin)

        service.clear_all_ids()

        self.assertEqual(plugin.owned_games.cleared, 1)


if __name__ == "__main__":
    unittest.main()
