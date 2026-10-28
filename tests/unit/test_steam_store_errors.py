import json
import unittest
from unittest.mock import patch

import httpx

from src.infrastructure.clients.errors import ProviderError
from src.infrastructure.clients.steam import SteamClientMixin, classify_steam_store_error


class FakeSteam(SteamClientMixin):
    def __init__(self):
        self.proxy = None
        self.STEAM_STORE_BASE = "https://store.steampowered.com"
        self.STEAM_API_BASE = "https://api.steampowered.com"
        self._steam_store_http_client = None
        self._steam_store_http_client_loop = None
        self.last_store_error = None


class _FakeResponse:
    def __init__(self, payload=None, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text
        self.request = httpx.Request("GET", "https://store.steampowered.com/api/appdetails")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=self.request,
                response=httpx.Response(self.status_code, request=self.request),
            )

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _ScriptedClient:
    responses = []
    calls = []
    scripted_regions = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def aclose(self):
        return None

    async def get(self, url, params=None, timeout=None):
        params = params or {}
        region = str(params.get("cc") or "").upper()
        _ScriptedClient.calls.append((url, params, params.get("cc"), params.get("l"), params.get("language")))
        if _ScriptedClient.scripted_regions and region not in _ScriptedClient.scripted_regions:
            raise httpx.ConnectError("unscripted region")
        if not _ScriptedClient.responses:
            raise AssertionError("unexpected Steam request")
        item = _ScriptedClient.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _payload(success, **data):
    body = {"data": data} if success else {}
    return _FakeResponse({"1034140": {"success": success, **body}})


class SteamStoreErrorClassificationTests(unittest.TestCase):
    def test_classifies_timeout_rate_limit_and_invalid_json(self):
        timeout = classify_steam_store_error(httpx.ReadTimeout("timeout"))
        request = httpx.Request("GET", "https://store.steampowered.com")
        rate = classify_steam_store_error(
            httpx.HTTPStatusError(
                "rate",
                request=request,
                response=httpx.Response(429, request=request),
            )
        )
        invalid = classify_steam_store_error(json.JSONDecodeError("bad", "x", 0))

        self.assertEqual("TIMEOUT", timeout.code)
        self.assertTrue(timeout.retryable)
        self.assertEqual("RATE_LIMITED", rate.code)
        self.assertEqual("INVALID_RESPONSE", invalid.code)
        self.assertIsInstance(timeout, ProviderError)


class SteamStoreStructuredErrorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _ScriptedClient.responses = []
        _ScriptedClient.calls = []
        _ScriptedClient.scripted_regions = None

    async def test_region_lock_continues_fallback_and_records_reason(self):
        _ScriptedClient.responses = [
            _payload(False),
            _payload(True, name="Subverse", price_overview={"currency": "HKD", "final": 24900, "initial": 24900}),
        ]
        client = FakeSteam()
        with patch("src.infrastructure.clients.steam.httpx.AsyncClient", _ScriptedClient):
            detail = await client.fetch_game_details("1034140", country="CN")

        self.assertEqual("HK", detail["_store_region"])
        self.assertEqual("REGION_LOCKED", detail["_store_status"])
        self.assertTrue(detail["_store_fallback"])
        self.assertEqual("CN", detail["_requested_region"])

    async def test_aliased_response_key_still_returns_price_and_details(self):
        _ScriptedClient.scripted_regions = {"CN"}
        _ScriptedClient.responses = [
            _FakeResponse({
                "2855530": {
                    "success": True,
                    "data": {
                        "name": "艾尔登法环",
                        "steam_appid": 1245620,
                        "release_date": {"date": "2022 年 2 月 24 日"},
                        "developers": ["FromSoftware, Inc."],
                        "price_overview": {
                            "currency": "CNY",
                            "final": 29800,
                            "initial": 29800,
                            "discount_percent": 0,
                        },
                    },
                }
            }),
            _FakeResponse({
                "2855530": {
                    "success": True,
                    "data": {
                        "name": "艾尔登法环",
                        "steam_appid": 1245620,
                        "price_overview": {
                            "currency": "CNY",
                            "final": 29800,
                            "initial": 29800,
                            "discount_percent": 0,
                        },
                    },
                }
            }),
        ]
        client = FakeSteam()
        with patch("src.infrastructure.clients.steam.httpx.AsyncClient", _ScriptedClient):
            detail = await client.fetch_game_details("1245620", country="CN")
            price = await client.fetch_region_price("1245620", "CN")

        self.assertEqual("艾尔登法环", detail["name"])
        self.assertEqual("2022 年 2 月 24 日", detail["release_date"]["date"])
        self.assertEqual("CN", detail["_store_region"])
        self.assertFalse(detail["_store_fallback"])
        self.assertEqual(298.0, price["current_price"])
        self.assertEqual("CNY", price["currency"])
        self.assertEqual("CN", price["region"])

    async def test_missing_product_does_not_look_like_network_failure(self):
        _ScriptedClient.responses = [_FakeResponse({}) for _ in range(10)]
        client = FakeSteam()
        with patch("src.infrastructure.clients.steam.httpx.AsyncClient", _ScriptedClient):
            detail = await client.fetch_game_details("1034140", country="CN")

        self.assertIsNone(detail)
        self.assertEqual("NOT_FOUND", client.last_store_error.code)

    async def test_network_failure_is_classified_and_still_tries_next_region(self):
        _ScriptedClient.responses = [
            httpx.ConnectError("down"),
            _payload(True, name="Subverse"),
        ]
        client = FakeSteam()
        with patch("src.infrastructure.clients.steam.httpx.AsyncClient", _ScriptedClient):
            detail = await client.fetch_game_details("1034140", country="CN")

        self.assertEqual("Subverse", detail["name"])
        self.assertEqual("HK", detail["_store_region"])
        self.assertEqual("UPSTREAM_ERROR", detail["_store_errors"][0]["code"])

    async def test_invalid_json_is_not_swallowed_as_empty_product(self):
        _ScriptedClient.scripted_regions = {"US"}
        _ScriptedClient.responses = [ _FakeResponse(json.JSONDecodeError("bad", "{", 0)) ]
        client = FakeSteam()
        with patch("src.infrastructure.clients.steam.httpx.AsyncClient", _ScriptedClient):
            price = await client.fetch_region_price("1034140", "US")

        self.assertIsNone(price)
        self.assertEqual("INVALID_RESPONSE", client.last_store_error.code)
        self.assertEqual(1, len(_ScriptedClient.calls))

    async def test_no_price_is_distinct_from_region_lock(self):
        _ScriptedClient.scripted_regions = {"US"}
        _ScriptedClient.responses = [
            _payload(True, name="Free Sample"),
        ]
        client = FakeSteam()
        with patch("src.infrastructure.clients.steam.httpx.AsyncClient", _ScriptedClient):
            price = await client.fetch_region_price("1034140", "US")

        self.assertIsNone(price)
        self.assertEqual("NO_PRICE", client.last_store_error.code)

    async def test_review_failure_does_not_block_the_other_language(self):
        _ScriptedClient.responses = [
            httpx.ReadTimeout("reviews"),
            _FakeResponse({
                "response": {
                    "query_summary": {"total_reviews": 10, "total_positive": 9},
                },
            }),
        ]
        client = FakeSteam()
        with patch("src.infrastructure.clients.steam.httpx.AsyncClient", _ScriptedClient):
            reviews = await client.fetch_game_reviews_both("1034140")

        self.assertIsNone(reviews["all"])
        self.assertEqual("特别好评", reviews["schinese"]["text"])
        self.assertEqual("TIMEOUT", client.last_store_error.code)


class SteamReviewMigrationTests(unittest.IsolatedAsyncioTestCase):
    """issue #65：评价摘要迁移到 IUserReviewsService/GetAppReviews v1。"""

    def setUp(self):
        _ScriptedClient.responses = []
        _ScriptedClient.calls = []
        _ScriptedClient.scripted_regions = None

    @staticmethod
    def _reviews_payload(**summary):
        return _FakeResponse({"response": {"query_summary": summary}})

    async def test_uses_getappreviews_endpoint_with_get_only(self):
        _ScriptedClient.responses = [
            self._reviews_payload(
                review_score=9, review_score_desc="Overwhelmingly Positive",
                total_positive=102003, total_negative=3415, total_reviews=105418,
            )
        ]
        client = FakeSteam()
        with patch("src.infrastructure.clients.steam.httpx.AsyncClient", _ScriptedClient):
            result = await client._review_summary("238320", None)

        url, _params, _region, _lang, _language = _ScriptedClient.calls[0]
        self.assertIn("IUserReviewsService/GetAppReviews/v1/", url)
        self.assertNotIn("appreviews", url)
        self.assertEqual("好评如潮", result["text"])
        self.assertEqual(97, result["percent"])
        self.assertEqual(105418, result["total"])

    async def test_language_argument_maps_to_languages_array(self):
        _ScriptedClient.responses = [
            self._reviews_payload(total_reviews=10, total_positive=9),
            self._reviews_payload(total_reviews=10, total_positive=9),
        ]
        client = FakeSteam()
        with patch("src.infrastructure.clients.steam.httpx.AsyncClient", _ScriptedClient):
            await client._review_summary("238320", None)
            await client._review_summary("238320", "schinese")

        first = json.loads(_ScriptedClient.calls[0][1]["input_json"])
        second = json.loads(_ScriptedClient.calls[1][1]["input_json"])
        self.assertEqual(["all"], first["languages"])
        self.assertEqual(["schinese"], second["languages"])
        self.assertEqual(1, first["num_per_page"])
        self.assertEqual(0, first["review_type"])

    async def test_review_score_desc_drives_chinese_label(self):
        cases = [
            (9, "Overwhelmingly Positive", "好评如潮"),
            (8, "Very Positive", "特别好评"),
            (7, "Positive", "好评"),
            (6, "Mostly Positive", "多半好评"),
            (5, "Mixed", "褒贬不一"),
            (4, "Mostly Negative", "多半差评"),
            (3, "Negative", "差评"),
            (2, "Very Negative", "特别差评"),
            (1, "Overwhelmingly Negative", "差评如潮"),
        ]
        client = FakeSteam()
        for score, desc, expected in cases:
            _ScriptedClient.responses = [
                self._reviews_payload(
                    review_score=score, review_score_desc=desc,
                    total_positive=5, total_negative=5, total_reviews=10,
                )
            ]
            with patch("src.infrastructure.clients.steam.httpx.AsyncClient", _ScriptedClient):
                result = await client._review_summary("570", None)
            self.assertEqual(expected, result["text"], msg=f"score={score}")

    async def test_percent_is_fallback_when_score_missing(self):
        _ScriptedClient.responses = [
            self._reviews_payload(total_reviews=100, total_positive=82)
        ]
        client = FakeSteam()
        with patch("src.infrastructure.clients.steam.httpx.AsyncClient", _ScriptedClient):
            result = await client._review_summary("570", None)

        self.assertEqual("特别好评", result["text"])
        self.assertEqual(82, result["percent"])

    async def test_empty_response_becomes_no_reviews_not_error(self):
        _ScriptedClient.responses = [_FakeResponse({"response": {}})]
        client = FakeSteam()
        with patch("src.infrastructure.clients.steam.httpx.AsyncClient", _ScriptedClient):
            result = await client._review_summary("999999999", None)

        self.assertEqual({"text": "暂无评价", "percent": None, "total": 0}, result)
        self.assertIsNone(client.last_store_error)

    async def test_zero_total_becomes_no_reviews(self):
        _ScriptedClient.responses = [
            self._reviews_payload(total_reviews=0, total_positive=0)
        ]
        client = FakeSteam()
        with patch("src.infrastructure.clients.steam.httpx.AsyncClient", _ScriptedClient):
            result = await client._review_summary("570", None)

        self.assertEqual("暂无评价", result["text"])
        self.assertIsNone(result["percent"])

    async def test_rate_limit_is_classified(self):
        request = httpx.Request("GET", "https://api.steampowered.com")
        _ScriptedClient.responses = [
            _FakeResponse(
                {"response": {}},
                status_code=429,
            )
        ]
        client = FakeSteam()
        with patch("src.infrastructure.clients.steam.httpx.AsyncClient", _ScriptedClient):
            result = await client._review_summary("570", None)

        self.assertIsNone(result)
        self.assertEqual("RATE_LIMITED", client.last_store_error.code)
        self.assertTrue(client.last_store_error.retryable)

    async def test_non_numeric_appid_skips_request(self):
        client = FakeSteam()
        with patch("src.infrastructure.clients.steam.httpx.AsyncClient", _ScriptedClient):
            result = await client._review_summary("abc", None)

        self.assertIsNone(result)
        self.assertEqual([], _ScriptedClient.calls)
