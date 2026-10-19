"""`/steam BL` 强制黑名单命令。"""
import unittest
from types import SimpleNamespace

from src.presentation.commands.ops import blacklist


class _Config(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved = 0

    def save_config(self):
        self.saved += 1


class _Event:
    def __init__(self, message):
        self.message_str = message
        self.texts = []

    def get_message_str(self):
        return self.message_str

    def plain_result(self, text):
        self.texts.append(text)
        return text


async def _run(plugin, message, action="", value=""):
    event = _Event(message)
    texts = [item async for item in blacklist(plugin, event, action, value)]
    return event, texts


class BlacklistCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_add_from_all_mode_switches_and_saves_once(self):
        plugin = SimpleNamespace(config=_Config(game_filter_mode="全部游戏", game_filter_ids=""))

        _, texts = await _run(plugin, "/steam BL 730，431960 570")

        self.assertEqual("730,431960,570", plugin.config["game_filter_ids"])
        self.assertEqual("黑名单", plugin.config["game_filter_mode"])
        self.assertEqual(1, plugin.config.saved)
        self.assertIn("已自动切换过滤模式", texts[0])

    async def test_add_from_whitelist_warns_about_side_effect(self):
        plugin = SimpleNamespace(config=_Config(game_filter_mode="白名单", game_filter_ids="570"))

        _, texts = await _run(plugin, "/steam BL 730")

        self.assertEqual("570,730", plugin.config["game_filter_ids"])
        self.assertIn("原白名单不再生效", texts[0])

    async def test_three_space_separated_ids_are_not_dropped(self):
        plugin = SimpleNamespace(config=_Config(game_filter_mode="黑名单", game_filter_ids=""))

        await _run(plugin, "/steam BL 730 431960 570", action="730", value="431960")

        self.assertEqual("730,431960,570", plugin.config["game_filter_ids"])

    async def test_duplicate_still_forces_mode_without_second_save(self):
        plugin = SimpleNamespace(config=_Config(game_filter_mode="全部游戏", game_filter_ids="730"))

        _, texts = await _run(plugin, "/steam BL 0730")

        self.assertEqual("730", plugin.config["game_filter_ids"])
        self.assertEqual("黑名单", plugin.config["game_filter_mode"])
        self.assertEqual(1, plugin.config.saved)
        self.assertIn("已在黑名单中", texts[0])

    async def test_invalid_token_rejects_whole_command(self):
        plugin = SimpleNamespace(config=_Config(game_filter_mode="黑名单", game_filter_ids="730"))

        _, texts = await _run(plugin, "/steam BL 730,abc")

        self.assertEqual("730", plugin.config["game_filter_ids"])
        self.assertEqual(0, plugin.config.saved)
        self.assertIn("无法识别为 AppID", texts[0])

    async def test_steamid64_gets_specific_hint(self):
        plugin = SimpleNamespace(config=_Config(game_filter_mode="黑名单", game_filter_ids=""))

        _, texts = await _run(plugin, "/steam BL 76561198099315021")

        self.assertIn("SteamID64", texts[0])
        self.assertEqual(0, plugin.config.saved)

    async def test_list_shows_mode_and_ids(self):
        plugin = SimpleNamespace(config=_Config(game_filter_mode="黑名单", game_filter_ids="730,431960"))

        _, texts = await _run(plugin, "/steam BL list")

        self.assertIn("游戏过滤模式：黑名单", texts[0])
        self.assertIn("730, 431960", texts[0])

    async def test_list_in_other_mode_hints_auto_switch(self):
        plugin = SimpleNamespace(config=_Config(game_filter_mode="白名单", game_filter_ids="730"))

        _, texts = await _run(plugin, "/steam BL")

        self.assertIn("当前不是黑名单模式", texts[0])

    async def test_delete_existing_saves_once(self):
        plugin = SimpleNamespace(config=_Config(game_filter_mode="黑名单", game_filter_ids="730,431960"))

        _, texts = await _run(plugin, "/steam BL del 730")

        self.assertEqual("431960", plugin.config["game_filter_ids"])
        self.assertEqual(1, plugin.config.saved)
        self.assertIn("已从黑名单删除", texts[0])

    async def test_delete_missing_does_not_write(self):
        plugin = SimpleNamespace(config=_Config(game_filter_mode="黑名单", game_filter_ids="730"))

        _, texts = await _run(plugin, "/steam BL del 999")

        self.assertEqual(0, plugin.config.saved)
        self.assertIn("不在黑名单中", texts[0])

    async def test_delete_without_ids_shows_usage(self):
        plugin = SimpleNamespace(config=_Config(game_filter_mode="黑名单", game_filter_ids="730"))

        _, texts = await _run(plugin, "/steam BL del")

        self.assertIn("用法：/steam BL del", texts[0])
        self.assertEqual(0, plugin.config.saved)


if __name__ == "__main__":
    unittest.main()
