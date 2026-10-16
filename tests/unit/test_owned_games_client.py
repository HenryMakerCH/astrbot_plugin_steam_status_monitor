"""fetch_owned_games 客户端单测：参数、隐私分支、限流、重试与日志安全。"""
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from src.infrastructure.clients.steam import (
    OwnedGamesInvisible,
    OwnedGamesRateLimited,
    SteamClientError,
    SteamClientMixin,
)

SECRET_KEY = "SECRET_KEY_123456"


class FakeSteam(SteamClientMixin):
    def __init__(self):
        self.API_KEY = SECRET_KEY
        self.RETRY_TIMES = 2
        self.proxy = None
        self.STEAM_API_BASE = "https://api.steampowered.com"


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    instances = []
    responses = []
    requests = []

    def __init__(self, *args, **kwargs):
        type(self).instances.append(kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, params=None):
        type(self).requests.append({"url": url, "params": params})
        if type(self).responses:
            item = type(self).responses.pop(0)
        else:
            item = _FakeResponse(200, {"response": {}})
        if isinstance(item, Exception):
            raise item
        return item


def _reset():
    _FakeClient.instances = []
    _FakeClient.responses = []
    _FakeClient.requests = []


class FetchOwnedGamesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _reset()

    async def test_minimal_params_and_parse(self):
        client = FakeSteam()
        _FakeClient.responses = [
            _FakeResponse(200, {"response": {
                "game_count": 2,
                "games": [
                    {"appid": 10, "playtime_forever": 5},
                    {"appid": 20, "playtime_forever": 0},
                ],
            }})
        ]
        with patch("src.infrastructure.clients.steam.httpx.AsyncClient", _FakeClient):
            games = await client.fetch_owned_games("76561198000000001")

        self.assertEqual([g["appid"] for g in games], [10, 20])
        request = _FakeClient.requests[0]
        # Key 必须随 params 传递、不得出现在 URL 中
        self.assertEqual(request["params"]["key"], SECRET_KEY)
        self.assertNotIn(SECRET_KEY, request["url"])
        self.assertEqual(request["params"]["steamid"], "76561198000000001")
        self.assertEqual(request["params"]["include_appinfo"], 0)
        self.assertEqual(request["params"]["include_played_free_games"], 1)

    async def test_include_appinfo_flag(self):
        client = FakeSteam()
        _FakeClient.responses = [
            _FakeResponse(200, {"response": {"game_count": 1, "games": [{"appid": 1}]}})
        ]
        with patch("src.infrastructure.clients.steam.httpx.AsyncClient", _FakeClient):
            await client.fetch_owned_games(
                "76561198000000001", include_appinfo=True, include_played_free_games=False
            )

        params = _FakeClient.requests[0]["params"]
        self.assertEqual(params["include_appinfo"], 1)
        self.assertEqual(params["include_played_free_games"], 0)

    async def test_count_mismatch_tolerated(self):
        client = FakeSteam()
        _FakeClient.responses = [
            _FakeResponse(200, {"response": {
                "game_count": 3,
                "games": [{"appid": 1}, {"appid": 2}],
            }})
        ]
        with patch("src.infrastructure.clients.steam.httpx.AsyncClient", _FakeClient):
            games = await client.fetch_owned_games("76561198000000001")

        self.assertEqual([g["appid"] for g in games], [1, 2])
        self.assertEqual(len(_FakeClient.requests), 1)

    async def test_empty_library_returns_empty_list(self):
        client = FakeSteam()
        _FakeClient.responses = [_FakeResponse(200, {"response": {"game_count": 0}})]
        with patch("src.infrastructure.clients.steam.httpx.AsyncClient", _FakeClient):
            games = await client.fetch_owned_games("76561198000000001")

        self.assertEqual(games, [])
        self.assertEqual(len(_FakeClient.requests), 1)

    async def test_invisible_raises_without_retry(self):
        client = FakeSteam()
        _FakeClient.responses = [_FakeResponse(200, {"response": {}})]
        with patch("src.infrastructure.clients.steam.httpx.AsyncClient", _FakeClient):
            with self.assertRaises(OwnedGamesInvisible) as ctx:
                await client.fetch_owned_games("76561198000000001")

        self.assertIn("游戏详情", str(ctx.exception))
        self.assertEqual(len(_FakeClient.requests), 1)

    async def test_rate_limited_raises_without_retry(self):
        client = FakeSteam()
        _FakeClient.responses = [_FakeResponse(429)]
        with patch("src.infrastructure.clients.steam.httpx.AsyncClient", _FakeClient):
            with self.assertRaises(OwnedGamesRateLimited):
                await client.fetch_owned_games("76561198000000001")

        self.assertEqual(len(_FakeClient.requests), 1)

    async def test_network_error_retries_then_fails_sanitized(self):
        client = FakeSteam()
        _FakeClient.responses = [httpx.ConnectError("boom"), httpx.ConnectError("boom2")]
        with patch("src.infrastructure.clients.steam.httpx.AsyncClient", _FakeClient):
            with patch("src.infrastructure.clients.steam.asyncio.sleep", new=AsyncMock()):
                with self.assertRaises(SteamClientError) as ctx:
                    await client.fetch_owned_games("76561198000000001")

        self.assertEqual(len(_FakeClient.requests), client.RETRY_TIMES)
        message = str(ctx.exception)
        self.assertNotIn(SECRET_KEY, message)
        self.assertNotIn("https://", message)

    async def test_500_retries_then_succeeds(self):
        client = FakeSteam()
        _FakeClient.responses = [
            _FakeResponse(500),
            _FakeResponse(200, {"response": {"game_count": 1, "games": [{"appid": 7}]}}),
        ]
        with patch("src.infrastructure.clients.steam.httpx.AsyncClient", _FakeClient):
            with patch("src.infrastructure.clients.steam.asyncio.sleep", new=AsyncMock()):
                games = await client.fetch_owned_games("76561198000000001")

        self.assertEqual([g["appid"] for g in games], [7])
        self.assertEqual(len(_FakeClient.requests), 2)

    async def test_missing_key_raises(self):
        client = FakeSteam()
        client.API_KEY = ""
        with self.assertRaises(SteamClientError):
            await client.fetch_owned_games("76561198000000001")
        self.assertEqual(len(_FakeClient.requests), 0)


if __name__ == "__main__":
    unittest.main()
