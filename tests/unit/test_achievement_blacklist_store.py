"""成就黑名单 v2 存储：迁移、原子写、增删与离线回填。"""
import json
import os
import tempfile
import unittest

from src.infrastructure.persistence.achievement_blacklist_store import (
    REASON_FETCH_FAILED,
    REASON_LEGACY,
    REASON_MANUAL,
    SOURCE_AUTO,
    SOURCE_LEGACY,
    SOURCE_MANUAL,
    AchievementBlacklistStore,
)


class BlacklistStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = self._tmp.name
        self.path = os.path.join(self.data_dir, "achievement_blacklist.json")

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, payload):
        with open(self.path, "w", encoding="utf-8") as stream:
            if isinstance(payload, str):
                stream.write(payload)
            else:
                json.dump(payload, stream, ensure_ascii=False)

    def _read(self):
        with open(self.path, "r", encoding="utf-8") as stream:
            return json.load(stream)

    def test_missing_file_is_empty(self):
        store = AchievementBlacklistStore(self.data_dir)
        self.assertEqual({}, store.entries)
        self.assertFalse(os.path.exists(self.path))

    def test_v1_list_migrates_to_v2(self):
        self._write(["730", "570"])
        store = AchievementBlacklistStore(self.data_dir)

        self.assertEqual({"730", "570"}, set(store.entries))
        self.assertEqual(REASON_LEGACY, store.entries["730"]["reason"])
        self.assertEqual(SOURCE_LEGACY, store.entries["730"]["source"])
        self.assertEqual(0, store.entries["730"]["added_at"])

        payload = self._read()
        self.assertEqual(2, payload["version"])
        self.assertIn("730", payload["entries"])

    def test_v1_int_and_mixed_ids_normalized(self):
        self._write([730, "0730", "570"])
        store = AchievementBlacklistStore(self.data_dir)
        self.assertEqual({"730", "570"}, set(store.entries))

    def test_v1_dict_keeps_name(self):
        self._write({"730": {"game_name": "Counter-Strike 2"}})
        store = AchievementBlacklistStore(self.data_dir)
        self.assertEqual("Counter-Strike 2", store.entries["730"]["game_name"])

    def test_v1_backup_created(self):
        self._write(["730"])
        AchievementBlacklistStore(self.data_dir)
        backup = f"{self.path}.bak-v1"
        self.assertTrue(os.path.exists(backup))
        with open(backup, "r", encoding="utf-8") as stream:
            self.assertEqual(["730"], json.load(stream))

    def test_corrupt_file_quarantined(self):
        self._write("{not json")
        store = AchievementBlacklistStore(self.data_dir)
        self.assertEqual({}, store.entries)
        names = [name for name in os.listdir(self.data_dir) if ".corrupt-" in name]
        self.assertEqual(1, len(names))

    def test_save_is_atomic_and_leaves_no_tmp(self):
        store = AchievementBlacklistStore(self.data_dir)
        store.add("730", reason=REASON_MANUAL, source=SOURCE_MANUAL)
        leftovers = [name for name in os.listdir(self.data_dir) if name.endswith(".tmp")]
        self.assertEqual([], leftovers)

    def test_add_new_persists_metadata(self):
        store = AchievementBlacklistStore(self.data_dir)
        self.assertTrue(store.add("730", reason=REASON_MANUAL, game_name="CS2", source=SOURCE_MANUAL))

        meta = self._read()["entries"]["730"]
        self.assertEqual("CS2", meta["game_name"])
        self.assertEqual(REASON_MANUAL, meta["reason"])
        self.assertEqual(SOURCE_MANUAL, meta["source"])
        self.assertGreater(meta["added_at"], 0)

    def test_add_is_idempotent_keeps_first_time(self):
        store = AchievementBlacklistStore(self.data_dir)
        store.add("730", reason=REASON_FETCH_FAILED, game_name="CS2", source=SOURCE_AUTO)
        first = store.entries["730"]["added_at"]

        self.assertFalse(
            store.add("730", reason=REASON_FETCH_FAILED, game_name="CS2", source=SOURCE_AUTO)
        )
        self.assertEqual(first, store.entries["730"]["added_at"])

    def test_add_fills_missing_name(self):
        self._write(["730"])
        store = AchievementBlacklistStore(self.data_dir)
        store.add("730", reason=REASON_MANUAL, game_name="CS2", source=SOURCE_MANUAL)
        self.assertEqual("CS2", store.entries["730"]["game_name"])

    def test_manual_add_upgrades_legacy(self):
        self._write(["730"])
        store = AchievementBlacklistStore(self.data_dir)
        store.add("730", reason=REASON_MANUAL, source=SOURCE_MANUAL)
        self.assertEqual(SOURCE_MANUAL, store.entries["730"]["source"])
        self.assertEqual(REASON_MANUAL, store.entries["730"]["reason"])
        self.assertEqual(0, store.entries["730"]["added_at"])

    def test_invalid_appid_rejected(self):
        store = AchievementBlacklistStore(self.data_dir)
        for token in ("", "abc", "0", "76561198099315021", None):
            self.assertFalse(store.add(token, reason=REASON_MANUAL))

    def test_remove_returns_removed_and_missing(self):
        self._write(["730", "570"])
        store = AchievementBlacklistStore(self.data_dir)
        removed, missing = store.remove(["730", "999"])
        self.assertEqual(["730"], removed)
        self.assertEqual(["999"], missing)
        self.assertEqual({"570"}, set(store.entries))

    def test_remove_all_missing_does_not_write(self):
        self._write(["730"])
        store = AchievementBlacklistStore(self.data_dir)
        before = os.path.getmtime(self.path)
        removed, missing = store.remove(["999"])
        self.assertEqual([], removed)
        self.assertEqual(["999"], missing)
        self.assertEqual(before, os.path.getmtime(self.path))

    def test_clear_returns_count(self):
        self._write(["730", "570"])
        store = AchievementBlacklistStore(self.data_dir)
        self.assertEqual(2, store.clear())
        self.assertEqual({}, store.entries)
        self.assertEqual({}, self._read()["entries"])

    def test_v2_roundtrip_preserves_manual(self):
        store = AchievementBlacklistStore(self.data_dir)
        store.add("730", reason=REASON_MANUAL, game_name="CS2", source=SOURCE_MANUAL)
        stamp = store.entries["730"]["added_at"]

        reloaded = AchievementBlacklistStore(self.data_dir)
        self.assertEqual("CS2", reloaded.entries["730"]["game_name"])
        self.assertEqual(SOURCE_MANUAL, reloaded.entries["730"]["source"])
        self.assertEqual(stamp, reloaded.entries["730"]["added_at"])

    def test_offline_backfill_prefers_latest(self):
        with open(os.path.join(self.data_dir, "play_records.json"), "w", encoding="utf-8") as stream:
            json.dump(
                {
                    "2026-09-01": {"s1": {"730": {"name": "旧名字", "minutes": 1}}},
                    "2026-09-20": {"s1": {"730": {"name": "新名字", "minutes": 2}}},
                },
                stream,
                ensure_ascii=False,
            )
        with open(os.path.join(self.data_dir, "session_records.json"), "w", encoding="utf-8") as stream:
            json.dump(
                {
                    "s1": [
                        {"gameid": "730", "game_name": "会话旧", "start_time": 100},
                        {"gameid": "730", "game_name": "会话新", "start_time": 200},
                    ]
                },
                stream,
                ensure_ascii=False,
            )
        self._write(["730"])

        store = AchievementBlacklistStore(self.data_dir)
        self.assertEqual("会话新", store.entries["730"]["game_name"])

    def test_items_sorted_by_added_at_desc(self):
        store = AchievementBlacklistStore(self.data_dir)
        store.add("730", reason=REASON_MANUAL, added_at=100)
        store.add("570", reason=REASON_MANUAL, added_at=300)
        store.add("999", reason=REASON_LEGACY, added_at=0)

        self.assertEqual(["570", "730", "999"], [appid for appid, _ in store.items()])


if __name__ == "__main__":
    unittest.main()
