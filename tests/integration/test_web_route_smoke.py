"""WebUI GET 接口通路冒烟（只读）。

用实盘 plugin_data 构造桩插件，自动枚举插件注册的全部 GET 路由，
逐个调用 handler 并打印 route/status 表；依赖网络的路径用桩降级。
运行：pytest tests/integration/test_web_route_smoke.py -q -s
"""
import asyncio
import json
import os
import sys
import time
import unittest
from types import SimpleNamespace

PLUGIN_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

import src.presentation.web.admin_api as admin_module  # noqa: E402
from src.presentation.renderers import rank as rank_renderer  # noqa: E402
from src.presentation.web.admin_api import WebAdminAPI  # noqa: E402

DEFAULT_DATA_DIR = os.path.abspath(
    os.path.join(PLUGIN_DIR, "..", "..", "plugin_data", "steam_status_monitor_V3")
)


def _load(data_dir, name, default):
    path = os.path.join(data_dir, name)
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


class RecordingContext:
    """记录 register_web_api 注册的全部路由。"""

    def __init__(self):
        self.routes = {}

    def register_web_api(self, path, handler, methods, description):
        for method in methods:
            self.routes[(method, path)] = handler


class FakeRequest:
    def __init__(self, query=None, match_info=None):
        self.query = dict(query or {})
        self.match_info = dict(match_info or {})


async def _async_none(*_args, **_kwargs):
    return None


def _make_plugin(data_dir):
    plugin = SimpleNamespace()
    plugin.data_dir = data_dir
    plugin.config = {}
    plugin.proxy = None
    plugin.API_KEY = ""
    plugin.group_steam_ids = _load(data_dir, "steam_groups.json", {})
    plugin.push_groups = _load(data_dir, "push_groups.json", {})
    plugin._bind_data = _load(data_dir, "bind_data.json", {})
    plugin.session_records = _load(data_dir, "session_records.json", {})
    plugin.play_records = _load(data_dir, "play_records.json", {})
    plugin.notify_sessions = _load(data_dir, "notify_sessions.json", {})
    plugin.running_groups = set(plugin.group_steam_ids)
    plugin.group_monitor_enabled = {gid: True for gid in plugin.group_steam_ids}
    plugin.ranking_service = None
    plugin.group_last_states = {}
    for name in os.listdir(data_dir):
        if name.startswith("group_") and name.endswith("_states.json"):
            gid = name[len("group_"):-len("_states.json")]
            data = _load(data_dir, name, {})
            if isinstance(data, dict):
                plugin.group_last_states[gid] = {
                    str(sid): state for sid, state in data.items() if isinstance(state, dict)
                }
    plugin.context = SimpleNamespace(get_platform_inst=lambda _platform: None)

    async def _no_cover(gameid):
        return None

    plugin.get_game_cover_url = _no_cover
    return plugin


class WebRouteSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_get_routes_are_reachable(self):
        data_dir = os.environ.get("STEAM_SMOKE_DATA_DIR", DEFAULT_DATA_DIR)
        if not os.path.isdir(data_dir):
            self.skipTest(f"plugin_data 不存在: {data_dir}")

        plugin = _make_plugin(data_dir)
        context = RecordingContext()

        admin_stub = SimpleNamespace()

        def _list_group_players(group_id):
            gid = str(group_id)
            direct = [str(sid) for sid in (plugin.group_steam_ids or {}).get(gid, [])]
            pushed = [
                str(sid)
                for sid, targets in (plugin.push_groups or {}).items()
                if gid in {str(target) for target in targets}
            ]
            states = (plugin.group_last_states or {}).get(gid, {})
            players = []
            for sid in dict.fromkeys(direct + pushed):
                state = states.get(sid)
                if not state:
                    state = next(
                        (
                            group_states.get(sid, {})
                            for group_states in (plugin.group_last_states or {}).values()
                            if sid in group_states
                        ),
                        {},
                    )
                players.append({"sid": sid, "state": state})
            return players

        admin_stub.list_group_players = _list_group_players
        api = WebAdminAPI(plugin, admin_service=admin_stub)
        api.register_routes(context)

        original_fetch_avatar = rank_renderer._fetch_avatar
        original_list_commands = admin_module.list_commands
        rank_renderer._fetch_avatar = _async_none

        async def _empty_commands(*_args, **_kwargs):
            return []

        admin_module.list_commands = _empty_commands
        try:
            sid = next(iter(plugin.session_records), "") or "76561198000000000"
            gid = next(iter(plugin.group_steam_ids), "") or "10000"
            routes = sorted(
                (path, handler)
                for (method, path), handler in context.routes.items()
                if method == "GET"
            )
            self.assertTrue(routes, "未捕获到任何 GET 路由")

            rows = []
            failures = []
            for path, handler in routes:
                if path.endswith("/test/steam"):
                    rows.append((path, "SKIP(network)", 0.0, 0))
                    continue

                concrete = (
                    path.replace("<steamid>", sid)
                    .replace("<group_id>", gid)
                    .replace("<gameid>", "730")
                )
                match_info = {"steamid": sid, "group_id": gid, "gameid": "730"}
                query = {}
                if path.endswith("/dashboard/rank-image"):
                    query = {"days": "30", "offset": "0"}
                elif path.endswith("/gantt/data"):
                    query = {"days": "30", "offset": "0"}
                elif path.endswith("/heatmap/data"):
                    query = {"period": "30"}
                elif path.endswith("/heatmap/player/<steamid>"):
                    query = {"period": "90"}
                elif path.endswith("/players/search"):
                    query = {"q": ""}

                started = time.perf_counter()
                try:
                    response = await asyncio.wait_for(
                        handler.__wrapped__(FakeRequest(query, match_info)), timeout=30
                    )
                    status = getattr(response, "status_code", 200)
                    body = getattr(response, "body", b"") or b""
                    elapsed = (time.perf_counter() - started) * 1000
                    rows.append((concrete, status, elapsed, len(body)))
                    if status >= 500:
                        failures.append((concrete, status, body[:200]))
                except Exception as exc:  # noqa: BLE001
                    elapsed = (time.perf_counter() - started) * 1000
                    rows.append((concrete, f"EXC:{type(exc).__name__}", elapsed, 0))
                    failures.append((concrete, type(exc).__name__, str(exc)[:300]))

            print("\n=== GET 接口通路冒烟 ===")
            for path, status, elapsed, size in rows:
                print(f"{path:<66} {str(status):<10} {elapsed:8.1f}ms {size:>10}B")
            print(f"合计 {len(rows)} 条，失败 {len(failures)} 条")

            self.assertFalse(failures, f"存在失败路由: {failures}")
        finally:
            rank_renderer._fetch_avatar = original_fetch_avatar
            admin_module.list_commands = original_list_commands


if __name__ == "__main__":
    unittest.main()
