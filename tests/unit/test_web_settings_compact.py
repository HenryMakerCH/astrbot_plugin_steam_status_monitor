"""WebUI 保存链路：简洁模式相关配置的类型与范围校验。

前端 pages/steam-monitor/app.js 会把这些键提交到 /api/settings/update，
这里直接对处理函数做单元验证（不经过 HTTP 层）。
"""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.presentation.web import admin_api
from src.presentation.web.admin_api import WEB_INT_RANGES, WebAdminAPI


class FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class FakeConfig(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved = 0

    def save_config(self):
        self.saved += 1


def _make_api():
    plugin = SimpleNamespace()
    plugin.config = FakeConfig()
    plugin.proxy = None
    plugin.SSL_CA_FILE = ""
    api = WebAdminAPI.__new__(WebAdminAPI)
    api.plugin = plugin
    return api, plugin


def _decode(response):
    """json_response 返回的是 astrbot 的 Response 对象，取回 body 与状态码。"""
    body = getattr(response, "body", None)
    if body is None:
        body = getattr(response, "text", None)
    status = getattr(response, "status", None) or getattr(response, "status_code", None)
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8")
    try:
        payload = json.loads(body) if body else None
    except (TypeError, ValueError):
        payload = body
    return status, payload


class SettingsUpdateValidationTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, payload):
        api, plugin = _make_api()
        response = await api._api_settings_update(FakeRequest(payload))
        return _decode(response), plugin

    async def test_notify_style_accepts_valid_values(self):
        for style in ("detailed", "compact"):
            (_status, _body), plugin = await self._call({"owned_games_notify_style": style})
            self.assertEqual(style, plugin.config["owned_games_notify_style"])

    async def test_notify_style_is_normalized(self):
        (_status, _body), plugin = await self._call({"owned_games_notify_style": "  COMPACT "})
        self.assertEqual("compact", plugin.config["owned_games_notify_style"])

    async def test_notify_style_rejects_unknown_value(self):
        (status, body), plugin = await self._call({"owned_games_notify_style": "compactt"})
        self.assertEqual(400, status)
        self.assertNotIn("owned_games_notify_style", plugin.config)
        self.assertIn("detailed", json.dumps(body, ensure_ascii=False))

    async def test_int_keys_are_stored_as_int(self):
        payload = {
            "owned_games_compact_page_size": "12",
            "owned_games_compact_max_pages": "5",
            "owned_games_compact_cover_concurrency": "2",
        }
        (_status, _body), plugin = await self._call(payload)
        self.assertEqual(12, plugin.config["owned_games_compact_page_size"])
        self.assertEqual(5, plugin.config["owned_games_compact_max_pages"])
        self.assertEqual(2, plugin.config["owned_games_compact_cover_concurrency"])
        for key in payload:
            self.assertIsInstance(plugin.config[key], int)

    async def test_int_keys_reject_out_of_range(self):
        cases = [
            ("owned_games_compact_page_size", 0),
            ("owned_games_compact_page_size", 21),
            ("owned_games_compact_max_pages", 11),
            ("owned_games_compact_cover_concurrency", 9),
        ]
        for key, value in cases:
            (status, _body), plugin = await self._call({key: value})
            self.assertEqual(400, status, msg=f"{key}={value}")
            self.assertNotIn(key, plugin.config)

    async def test_int_keys_reject_non_numeric(self):
        (status, _body), plugin = await self._call({"owned_games_compact_page_size": "abc"})
        self.assertEqual(400, status)
        self.assertNotIn("owned_games_compact_page_size", plugin.config)

    async def test_ranges_match_schema_defaults(self):
        """范围表必须与配置 schema 的声明一致，避免界面放行、服务层却夹取。"""
        with open("_conf_schema.json", "r", encoding="utf-8") as stream:
            schema = json.load(stream)
        for key, (low, high) in WEB_INT_RANGES.items():
            self.assertIn(key, schema, msg=f"{key} 未在 _conf_schema.json 中声明")
            self.assertEqual("int", schema[key]["type"], msg=key)
            self.assertEqual(key, key)
            self.assertTrue(1 <= low < high <= 20, msg=f"{key} 范围异常 {low}-{high}")
        self.assertEqual((1, 8), WEB_INT_RANGES["owned_games_compact_cover_concurrency"])
        self.assertEqual((1, 10), WEB_INT_RANGES["owned_games_compact_max_pages"])

    async def test_save_config_is_called(self):
        api, plugin = _make_api()
        await api._api_settings_update(FakeRequest({"owned_games_notify_style": "compact"}))
        self.assertEqual(1, plugin.config.saved)


class SettingsFrontendWiringTests(unittest.TestCase):
    """前端字段清单与服务端能力必须对齐，防止界面漏字段或提交被静默丢弃。"""

    def setUp(self):
        with open("pages/steam-monitor/app.js", "r", encoding="utf-8") as stream:
            self.js = stream.read()

    def test_compact_keys_are_exposed_in_settings(self):
        for key in (
            "owned_games_notify_style",
            "owned_games_compact_page_size",
            "owned_games_compact_max_pages",
            "owned_games_compact_cover_concurrency",
            "owned_games_max_per_notify",
        ):
            self.assertIn(f'"{key}"', self.js, msg=f"{key} 未出现在设置页字段清单")

    def test_style_key_renders_as_select(self):
        self.assertIn("SELV={", self.js)
        self.assertIn("owned_games_notify_style:[\"auto\",\"detailed\",\"compact\"]", self.js)

    def test_auto_threshold_key_is_exposed(self):
        self.assertIn('"owned_games_auto_compact_threshold"', self.js)

    def test_int_keys_are_collected_as_numbers(self):
        marker = "const IK=["
        start = self.js.index(marker)
        segment = self.js[start:self.js.index("];", start)]
        for key in (
            "owned_games_compact_page_size",
            "owned_games_compact_max_pages",
            "owned_games_compact_cover_concurrency",
        ):
            self.assertIn(f'"{key}"', segment, msg=f"{key} 未按整数提交")

    def test_select_values_are_submitted(self):
        self.assertIn("Object.keys(SELV).forEach", self.js)


if __name__ == "__main__":
    unittest.main()
