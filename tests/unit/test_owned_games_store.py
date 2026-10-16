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


if __name__ == "__main__":
    unittest.main()
