"""排行榜渲染：分段进度条宽度、Top15 截断与真实渲染冒烟（不依赖网络）。"""
import io
import unittest

from PIL import Image, ImageDraw

from src.presentation.renderers.rank import (
    MAX_GAMES_PER_PLAYER,
    _draw_segment_bar,
    _prepare_display_games,
    _split_segment_widths,
    render_rank_image,
)


class SegmentWidthTests(unittest.TestCase):
    def test_single_game_takes_all_pixels(self):
        self.assertEqual(_split_segment_widths([10], 50), [50])

    def test_proportional_split_is_exact(self):
        self.assertEqual(_split_segment_widths([5, 3, 2], 100), [50, 30, 20])

    def test_thirty_games_with_full_width(self):
        self.assertEqual(_split_segment_widths([1] * 30, 30), [1] * 30)

    def test_thirty_games_with_tiny_width(self):
        widths = _split_segment_widths([1] * 30, 5)
        self.assertEqual(sum(widths), 5)
        self.assertTrue(all(width >= 0 for width in widths))
        self.assertEqual(sum(1 for width in widths if width == 1), 5)

    def test_zero_width_returns_all_zero(self):
        self.assertEqual(_split_segment_widths([3, 2], 0), [0, 0])

    def test_zero_minutes_returns_all_zero(self):
        self.assertEqual(_split_segment_widths([0, 0], 40), [0, 0])

    def test_widths_are_monotonic_when_minutes_descend(self):
        widths = _split_segment_widths([100, 60, 30, 9, 1], 137)
        self.assertEqual(widths, sorted(widths, reverse=True))


class SegmentDrawTests(unittest.TestCase):
    def _draw(self, games, max_total, width=312):
        image = Image.new("RGBA", (400, 40))
        draw = ImageDraw.Draw(image)
        colors = [(10, 20, 30)] * len(games)
        return _draw_segment_bar(draw, 10, 10, width, 7, games, colors, max_total=max_total)

    def test_multi_game_segments_are_non_negative_and_exact(self):
        fill_w, _, segments = self._draw(
            [{"name": "A", "minutes": 900}, {"name": "B", "minutes": 1}], 1000
        )
        self.assertEqual(sum(item["width"] for item in segments), fill_w)
        self.assertTrue(all(item["width"] >= 0 for item in segments))
        self.assertEqual(segments[0]["x0"], 10)
        self.assertEqual(segments[-1]["x1"], 10 + fill_w)
        for previous, current in zip(segments, segments[1:]):
            self.assertEqual(previous["x1"], current["x0"])

    def test_thirty_short_games_do_not_crash(self):
        games = [{"name": f"G{i}", "minutes": 1} for i in range(30)]
        fill_w, _, segments = self._draw(games, 1000)
        self.assertGreater(fill_w, 0)
        self.assertEqual(sum(item["width"] for item in segments), fill_w)
        self.assertTrue(all(item["width"] >= 0 for item in segments))

    def test_fill_width_zero_returns_no_segments(self):
        fill_w, total, segments = self._draw(
            [{"name": "A", "minutes": 1}], 10 ** 9, width=312
        )
        self.assertEqual(fill_w, 0)
        self.assertEqual(segments, [])
        self.assertGreater(total, 0)

    def test_zero_minute_game_keeps_exact_total(self):
        fill_w, _, segments = self._draw(
            [{"name": "A", "minutes": 10}, {"name": "B", "minutes": 0}], 10
        )
        self.assertEqual(sum(item["width"] for item in segments), fill_w)
        self.assertEqual(segments[1]["width"], 0)


class DisplayGamesTests(unittest.TestCase):
    def test_truncates_and_merges_overflow(self):
        games = [{"name": f"G{i}", "minutes": 100 - i} for i in range(20)]
        prepared = _prepare_display_games(games)
        self.assertEqual(len(prepared), MAX_GAMES_PER_PLAYER + 1)
        self.assertTrue(prepared[-1]["name"].startswith("其他"))
        self.assertEqual(
            prepared[-1]["minutes"],
            sum(game["minutes"] for game in games[MAX_GAMES_PER_PLAYER:]),
        )

    def test_filters_zero_and_does_not_mutate_input(self):
        games = [{"name": "A", "minutes": 0}, {"name": "B", "minutes": 5}]
        prepared = _prepare_display_games(games)
        self.assertEqual([item["name"] for item in prepared], ["B"])
        self.assertEqual(games[0]["name"], "A")


class RankRenderSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_small_render_returns_png(self):
        rank_data = [
            {
                "sid": f"s{i}",
                "name": f"P{i}",
                "total_minutes": 600 - i,
                "avatar_url": None,
                "games": [
                    {"name": f"G{j}", "minutes": max(1, 60 - j)}
                    for j in range(3)
                ],
            }
            for i in range(3)
        ]
        payload = await render_rank_image(
            "", rank_data, "最近7天", font_path=None, cover_fetcher=None
        )
        self.assertTrue(payload.startswith(b"\x89PNG"))
        with Image.open(io.BytesIO(payload)) as image:
            self.assertEqual(image.width, 680)
            self.assertGreater(image.height, 0)

    async def test_twenty_five_players_with_thirty_games_stay_bounded(self):
        rank_data = []
        for i in range(25):
            games = [{"name": f"G{j}", "minutes": 100 - j} for j in range(30)]
            rank_data.append({
                "sid": f"s{i}",
                "name": f"P{i}",
                "total_minutes": sum(game["minutes"] for game in games),
                "avatar_url": None,
                "games": games,
            })
        payload = await render_rank_image(
            "", rank_data, "最近30天", font_path=None, cover_fetcher=None
        )
        self.assertTrue(payload.startswith(b"\x89PNG"))
        with Image.open(io.BytesIO(payload)) as image:
            # Top15+其他 = 16 行 → 430px/人；25 人约 11.2k px，远低于修复前的 ~18k
            self.assertLessEqual(image.height, 11200)
            self.assertLessEqual(image.width * image.height, 8_000_000)


if __name__ == "__main__":
    unittest.main()
