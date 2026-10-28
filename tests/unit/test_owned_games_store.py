"""OwnedGamesStore：加载校验、原子写、清理与体积轻量化。"""
import json
import os
import tempfile
import unittest

from src.infrastructure.persistence.owned_games_store import OwnedGamesStore


def _state(ids=None, **overrides):
    state = {
        "ids": list(ids or []),
        "baseline_at": 100,
        "last_ok_at": 200,
        "fail_count": 0,
        "grace": {},
        "pending": [],
    }
    state.update(overrides)
    return state


class OwnedGamesStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = self._tmp.name

    def test_missing_file_gives_empty_players(self):
        store = OwnedGamesStore(self.data_dir)
        self.assertEqual(store.players, {})

    def test_flush_and_reload_roundtrip_normalizes(self):
        store = OwnedGamesStore(self.data_dir)
        store.players["76561198000000001"] = _state([440, 220, 220])
        store.flush()

        again = OwnedGamesStore(self.data_dir)
        state = again.players["76561198000000001"]
        self.assertEqual(state["ids"], [220, 440])  # 去重且升序
        self.assertEqual(state["baseline_at"], 100)

    def test_corrupt_file_recovers_to_empty(self):
        with open(os.path.join(self.data_dir, "owned_games.json"), "w", encoding="utf-8") as stream:
            stream.write("{not json")
        store = OwnedGamesStore(self.data_dir)
        self.assertEqual(store.players, {})

    def test_invalid_player_state_dropped(self):
        payload = {"version": 1, "players": {
            "good": _state([1]),
            "bad_ids": {"ids": "oops"},
            "bad_node": 5,
        }}
        with open(os.path.join(self.data_dir, "owned_games.json"), "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
        store = OwnedGamesStore(self.data_dir)
        self.assertEqual(list(store.players), ["good"])

    def test_pending_and_grace_normalized(self):
        payload = {"version": 1, "players": {
            "s1": _state(
                [10],
                grace={"99": "1700000000", "bad": "x"},
                pending=[
                    {"appid": 570, "added_at": "1700000000", "groups": [222, "333"]},
                    {"name": "no appid"},
                ],
            ),
        }}
        with open(os.path.join(self.data_dir, "owned_games.json"), "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
        store = OwnedGamesStore(self.data_dir)
        state = store.players["s1"]
        self.assertEqual(state["grace"], {"99": 1700000000})
        self.assertEqual(len(state["pending"]), 1)
        item = state["pending"][0]
        self.assertEqual(item["appid"], "570")
        self.assertEqual(item["groups"], ["222", "333"])
        self.assertEqual(item["tries"], 0)

    def test_forget_clear_prune(self):
        store = OwnedGamesStore(self.data_dir)
        store.players["a"] = _state([1])
        store.players["b"] = _state([2])
        store.players["c"] = _state([3])

        self.assertTrue(store.forget("a"))
        self.assertFalse(store.forget("a"))
        self.assertEqual(list(store.players), ["b", "c"])

        removed = store.prune(["c"])
        self.assertEqual(removed, ["b"])
        self.assertEqual(list(store.players), ["c"])

        store.clear()
        self.assertEqual(store.players, {})

    def test_flush_is_compact_for_large_library(self):
        store = OwnedGamesStore(self.data_dir)
        store.players["x"] = _state(range(1000000, 1001000))
        store.flush()
        self.assertLess(os.path.getsize(store.path), 10240)  # 1000 款 < 10KB

    def test_flush_leaves_no_temp_files(self):
        store = OwnedGamesStore(self.data_dir)
        store.players["x"] = _state([1])
        store.flush()
        store.flush()
        leftovers = [name for name in os.listdir(self.data_dir) if name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_flush_creates_missing_dir(self):
        target = os.path.join(self.data_dir, "nested", "dir")
        store = OwnedGamesStore(target)
        store.players["x"] = _state([1])
        store.flush()
        self.assertTrue(os.path.exists(os.path.join(target, "owned_games.json")))

    # ---------- #66 简洁合并播报：owned_batch pending ----------

    def test_owned_batch_pending_roundtrip(self):
        batch = {
            "kind": "owned_batch",
            "added_at": 1700000000,
            "tries": 1,
            "render": True,
            "page": 2,
            "pages": 3,
            "added_count": 17,
            "groups": [111, "222"],
            "deliveries": [],
            "items": [{"appid": 570, "name": "Dota 2"}, {"appid": "730"}],
        }
        store = OwnedGamesStore(self.data_dir)
        store.players["s1"] = _state([10], pending=[batch])
        store.flush()

        again = OwnedGamesStore(self.data_dir)
        entry = again.players["s1"]["pending"][0]
        self.assertEqual(entry["kind"], "owned_batch")
        self.assertNotIn("appid", entry)
        self.assertEqual(entry["page"], 2)
        self.assertEqual(entry["pages"], 3)
        self.assertEqual(entry["added_count"], 17)
        self.assertEqual(entry["tries"], 1)
        self.assertEqual(entry["added_at"], 1700000000)
        self.assertTrue(entry["render"])
        self.assertEqual(entry["groups"], ["111", "222"])
        self.assertEqual(entry["items"], [
            {"appid": "570", "name": "Dota 2"},
            {"appid": "730", "name": "AppID 730"},
        ])

    def test_legacy_pending_without_kind_still_normalized(self):
        legacy = {
            "appid": 570,
            "name": "Dota 2",
            "added_at": "1700000000",
            "tries": 2,
            "render": False,
            "groups": ["333"],
            "card_data": {"title": "Dota 2", "reviews": {"zh": "好评"}},
            "deliveries": [{"group_id": "333", "noti": {"type": "card", "page": 1}}],
        }
        payload = {"version": 1, "players": {"s1": _state([10], pending=[legacy])}}
        with open(os.path.join(self.data_dir, "owned_games.json"), "w", encoding="utf-8") as stream:
            json.dump(payload, stream)

        entry = OwnedGamesStore(self.data_dir).players["s1"]["pending"][0]
        # 老字段一个都不能丢
        self.assertEqual(entry["card_data"], {"title": "Dota 2", "reviews": {"zh": "好评"}})
        self.assertIs(entry["render"], False)
        self.assertEqual(entry["deliveries"], [{"group_id": "333", "noti": {"type": "card", "page": 1}}])
        # 老数据不补写 kind，保证落盘字节与升级前一致
        self.assertNotIn("kind", entry)
        self.assertEqual(entry["appid"], "570")
        self.assertEqual(entry["tries"], 2)

    def test_legacy_pending_without_appid_dropped(self):
        payload = {"version": 1, "players": {"s1": _state([10], pending=[{"name": "no appid"}])}}
        with open(os.path.join(self.data_dir, "owned_games.json"), "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
        self.assertEqual(OwnedGamesStore(self.data_dir).players["s1"]["pending"], [])

    def test_owned_batch_invalid_items_dropped(self):
        bad_batches = [
            {"kind": "owned_batch", "items": []},
            {"kind": "owned_batch", "items": "oops"},
            {"kind": "owned_batch", "items": [{"name": "x"}]},
        ]
        payload = {"version": 1, "players": {
            "s1": _state(
                [10],
                pending=bad_batches + [{"appid": 440, "name": "TF2"}],
            ),
        }}
        with open(os.path.join(self.data_dir, "owned_games.json"), "w", encoding="utf-8") as stream:
            json.dump(payload, stream)

        pending = OwnedGamesStore(self.data_dir).players["s1"]["pending"]
        self.assertEqual(len(pending), 1)  # 三条脏 batch 全丢，其它 pending 不受影响
        self.assertEqual(pending[0]["appid"], "440")

    def test_mixed_pending_kinds_coexist(self):
        payload = {"version": 1, "players": {
            "s1": _state([10], pending=[
                {"appid": 440, "name": "TF2"},
                {"kind": "owned_batch", "page": 1, "pages": 2,
                 "items": [{"appid": 570, "name": "Dota 2"}]},
                {"appid": 730, "name": "CS2"},
            ]),
        }}
        with open(os.path.join(self.data_dir, "owned_games.json"), "w", encoding="utf-8") as stream:
            json.dump(payload, stream)

        pending = OwnedGamesStore(self.data_dir).players["s1"]["pending"]
        self.assertEqual([entry.get("kind", "owned") for entry in pending],
                         ["owned", "owned_batch", "owned"])
        self.assertEqual(pending[0]["appid"], "440")
        self.assertEqual(pending[1]["items"], [{"appid": "570", "name": "Dota 2"}])
        self.assertEqual(pending[2]["appid"], "730")

    def test_owned_batch_deliveries_preserved(self):
        batch = {
            "kind": "owned_batch",
            "items": [{"appid": 570, "name": "Dota 2"}],
            "deliveries": [
                {"group_id": 555, "noti": {"type": "compact_page", "page": 2}},
                {"group_id": "666", "noti": "not-a-dict"},  # 非法 noti 丢弃
            ],
        }
        store = OwnedGamesStore(self.data_dir)
        store.players["s1"] = _state([10], pending=[batch])
        store.flush()

        entry = OwnedGamesStore(self.data_dir).players["s1"]["pending"][0]
        self.assertEqual(entry["deliveries"], [
            {"group_id": "555", "noti": {"type": "compact_page", "page": 2}},
        ])


if __name__ == "__main__":
    unittest.main()
