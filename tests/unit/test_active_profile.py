"""用户平均活跃热度：144 桶、去重、跨日、周期截断与边界。"""
import unittest
from datetime import datetime, timedelta

from src.presentation.web.statistics import build_active_profile


class FixturePlugin:
    def __init__(self):
        self.group_steam_ids = {"g1": ["sid-1"], "g2": ["sid-2", "sid-3"]}
        self.push_groups = {}
        self.session_records = {}


def _session(start, end):
    return {
        "start_time": int(start.timestamp()),
        "end_time": int(end.timestamp()),
        "duration_min": (end - start).total_seconds() / 60,
        "date": (start - timedelta(hours=4)).strftime("%Y-%m-%d"),
        "gameid": "730",
        "game_name": "CS2",
    }


class ActiveProfileTests(unittest.TestCase):
    def setUp(self):
        self.plugin = FixturePlugin()

    def test_returns_144_buckets_with_hour_labels(self):
        self.plugin.session_records["sid-1"] = [
            _session(datetime(2026, 8, 21, 20, 0), datetime(2026, 8, 21, 21, 0))
        ]

        result = build_active_profile(self.plugin, 1, datetime(2026, 8, 21, 22, 0))

        self.assertEqual(len(result["buckets"]), 144)
        self.assertEqual(result["buckets"][0]["label"], "00:00")
        self.assertEqual(result["buckets"][143]["label"], "23:50")
        for index in range(120, 126):
            self.assertEqual(result["buckets"][index]["avg"], 1.0)
        self.assertEqual(result["buckets"][119]["avg"], 0.0)
        self.assertEqual(result["summary"]["sessions"], 1)
        self.assertEqual(result["summary"]["peak"], 1)
        self.assertEqual(result["summary"]["peak_label"], "20:00")
        self.assertEqual(result["summary"]["avg_online"], 0.04)

    def test_user_example_counts_concurrent_players(self):
        base = datetime(2026, 8, 21, 20, 0)
        self.plugin.session_records["sid-1"] = [_session(base, base + timedelta(minutes=20))]
        self.plugin.session_records["sid-2"] = [_session(base, base + timedelta(minutes=30))]
        self.plugin.session_records["sid-3"] = [_session(base, base + timedelta(minutes=10))]

        result = build_active_profile(self.plugin, 1, datetime(2026, 8, 21, 22, 0))

        self.assertEqual(
            [result["buckets"][index]["avg"] for index in (120, 121, 122)],
            [3.0, 2.0, 1.0],
        )
        self.assertEqual(
            [result["buckets"][index]["peak"] for index in (120, 121, 122)],
            [3, 2, 1],
        )

    def test_average_divides_all_days_in_window(self):
        self.plugin.session_records["sid-1"] = [
            _session(datetime(2026, 8, 21, 20, 0), datetime(2026, 8, 21, 21, 0))
        ]

        result = build_active_profile(self.plugin, 2, datetime(2026, 8, 21, 22, 0))

        self.assertEqual(result["days"], 2)
        self.assertEqual(result["days_with_data"], 1)
        self.assertEqual(result["buckets"][120]["avg"], 0.5)
        self.assertEqual(result["buckets"][120]["peak"], 1)

    def test_cross_midnight_splits_by_natural_day(self):
        self.plugin.session_records["sid-1"] = [
            _session(datetime(2026, 8, 21, 23, 50), datetime(2026, 8, 22, 0, 10))
        ]

        result = build_active_profile(self.plugin, 2, datetime(2026, 8, 22, 12, 0))

        self.assertEqual(result["buckets"][143]["avg"], 0.5)
        self.assertEqual(result["buckets"][0]["avg"], 0.5)
        self.assertEqual(result["days_with_data"], 2)

    def test_missing_end_time_falls_back_to_duration(self):
        start = datetime(2026, 8, 21, 20, 0)
        self.plugin.session_records["sid-1"] = [
            {
                "start_time": int(start.timestamp()),
                "end_time": 0,
                "duration_min": 60,
                "date": "2026-08-21",
            }
        ]

        result = build_active_profile(self.plugin, 1, datetime(2026, 8, 21, 22, 0))

        for index in range(120, 126):
            self.assertEqual(result["buckets"][index]["avg"], 1.0)

    def test_overlapping_sessions_dedupe_same_user(self):
        self.plugin.session_records["sid-1"] = [
            _session(datetime(2026, 8, 21, 20, 0), datetime(2026, 8, 21, 21, 0)),
            _session(datetime(2026, 8, 21, 20, 30), datetime(2026, 8, 21, 21, 30)),
        ]

        result = build_active_profile(self.plugin, 1, datetime(2026, 8, 21, 22, 0))

        self.assertEqual(result["buckets"][123]["avg"], 1.0)
        self.assertEqual(result["buckets"][126]["avg"], 1.0)

    def test_group_filter_includes_push_groups(self):
        self.plugin.session_records["sid-1"] = [
            _session(datetime(2026, 8, 21, 20, 0), datetime(2026, 8, 21, 21, 0))
        ]
        self.plugin.session_records["sid-2"] = [
            _session(datetime(2026, 8, 21, 20, 0), datetime(2026, 8, 21, 21, 0))
        ]
        self.plugin.session_records["sid-3"] = [
            _session(datetime(2026, 8, 21, 20, 0), datetime(2026, 8, 21, 21, 0))
        ]
        now = datetime(2026, 8, 21, 22, 0)

        self.assertEqual(build_active_profile(self.plugin, 1, now, "g1")["buckets"][120]["avg"], 1.0)
        self.assertEqual(build_active_profile(self.plugin, 1, now)["buckets"][120]["avg"], 3.0)

        self.plugin.push_groups = {"sid-2": ["g1"]}
        self.assertEqual(build_active_profile(self.plugin, 1, now, "g1")["buckets"][120]["avg"], 2.0)

    def test_period_clamped_to_max_profile_days(self):
        result = build_active_profile(self.plugin, 180, datetime(2026, 8, 21, 22, 0))
        self.assertEqual(result["days"], 90)
        self.assertEqual(result["requested_period"], 180)
        self.assertIn("90", result["note"])

        result = build_active_profile(self.plugin, 30, datetime(2026, 8, 21, 22, 0))
        self.assertEqual(result["note"], "")

    def test_invalid_and_ongoing_sessions_are_skipped(self):
        start = int(datetime(2026, 8, 21, 20, 0).timestamp())
        self.plugin.session_records["sid-1"] = [
            {"start_time": 0, "end_time": 0, "duration_min": 10},
            {"start_time": start, "end_time": 0, "duration_min": 0},
        ]

        result = build_active_profile(self.plugin, 1, datetime(2026, 8, 21, 22, 0))

        self.assertEqual(result["summary"]["sessions"], 0)
        self.assertEqual(result["summary"]["skipped"], 2)
        self.assertEqual(result["days_with_data"], 0)
        self.assertTrue(all(bucket["avg"] == 0.0 for bucket in result["buckets"]))

    def test_bucket_boundary_is_left_closed_right_open(self):
        self.plugin.session_records["sid-1"] = [
            _session(datetime(2026, 8, 21, 20, 0), datetime(2026, 8, 21, 20, 10))
        ]
        result = build_active_profile(self.plugin, 1, datetime(2026, 8, 21, 22, 0))
        self.assertEqual(result["buckets"][120]["avg"], 1.0)
        self.assertEqual(result["buckets"][121]["avg"], 0.0)

        self.plugin.session_records["sid-1"] = [
            _session(datetime(2026, 8, 21, 20, 10), datetime(2026, 8, 21, 20, 20))
        ]
        result = build_active_profile(self.plugin, 1, datetime(2026, 8, 21, 22, 0))
        self.assertEqual(result["buckets"][120]["avg"], 0.0)
        self.assertEqual(result["buckets"][121]["avg"], 1.0)


if __name__ == "__main__":
    unittest.main()
