"""presence 运行时状态清理：删除玩家 / 删除群 / 清空全部 ID。"""
import unittest

from src.application.services.monitor_admin import MonitorAdminService

from .test_monitor_admin import PluginStub


class PresenceCleanupTests(unittest.TestCase):
    def _plugin(self):
        plugin = PluginStub({"g1": ["s1", "s2"], "g2": ["s1"]})
        plugin._presence_online_since = {"s1": 1000, "s2": 900}
        plugin._presence_pending_offline = {
            "s1": {"detected_at": 1000},
            "s2": {"detected_at": 900},
        }
        plugin._presence_settled_offline_at = {"s1": 1000}
        return plugin

    def test_remove_player_clears_presence_state(self):
        plugin = self._plugin()
        admin = MonitorAdminService(plugin)

        admin.remove_player("g1", "s1")

        self.assertNotIn("s1", plugin._presence_online_since)
        self.assertNotIn("s1", plugin._presence_pending_offline)
        self.assertNotIn("s1", plugin._presence_settled_offline_at)
        # 其他玩家不受影响
        self.assertIn("s2", plugin._presence_online_since)

    def test_remove_group_keeps_presence_for_shared_sid(self):
        plugin = self._plugin()
        admin = MonitorAdminService(plugin)

        admin.remove_group("g1")

        # s1 仍被 g2 监控，presence 必须保留
        self.assertIn("s1", plugin._presence_online_since)
        self.assertIn("s1", plugin._presence_pending_offline)
        # s2 只在 g1，删除后清理
        self.assertNotIn("s2", plugin._presence_online_since)
        self.assertNotIn("s2", plugin._presence_pending_offline)

    def test_clear_all_ids_clears_presence_state(self):
        plugin = self._plugin()
        admin = MonitorAdminService(plugin)

        admin.clear_all_ids()

        self.assertEqual({}, plugin._presence_online_since)
        self.assertEqual({}, plugin._presence_pending_offline)
        self.assertEqual({}, plugin._presence_settled_offline_at)


if __name__ == "__main__":
    unittest.main()
