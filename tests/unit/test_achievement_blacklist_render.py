"""成就黑名单表格渲染：分栏、列边界与文字截断。"""
import unittest
from collections import Counter

from PIL import Image

from src.presentation.renderers.achievement_blacklist import (
    BORDER_COLOR,
    HEADER_HEIGHT,
    ROW_HEIGHT,
    TABLE_WIDTH,
    TWO_COLUMN_THRESHOLD,
    render_achievement_blacklist_image,
)


def _entries(count, name="Game", reason="no_stats", added_at=1700000000):
    return [
        (str(1000 + i), {"game_name": name, "added_at": added_at, "reason": reason, "source": "auto"})
        for i in range(count)
    ]


def _render(entries):
    import io

    return Image.open(io.BytesIO(render_achievement_blacklist_image(entries)))


class AchievementBlacklistRenderTests(unittest.TestCase):
    def test_small_list_uses_single_column(self):
        image = _render(_entries(10))
        self.assertEqual(TABLE_WIDTH + 48, image.width)
        # 标题 + 表头 + 10 行 + 页脚
        self.assertGreater(image.height, HEADER_HEIGHT + 10 * ROW_HEIGHT)

    def test_large_list_uses_two_columns(self):
        image = _render(_entries(TWO_COLUMN_THRESHOLD + 5))
        self.assertGreater(image.width, TABLE_WIDTH * 2)
        # 双栏后高度应明显小于单栏
        single_height = _render(_entries(20)).height
        self.assertLess(image.height, single_height * 3)

    def test_row_separators_present(self):
        image = _render(_entries(5))
        pixels = image.load()
        hits = [y for y in range(image.height - 60) if pixels[40, y] == BORDER_COLOR]
        # 顶部边框 + 表头下 + 每行一条
        self.assertGreaterEqual(len(hits), 7)

    def test_long_name_truncated_within_column(self):
        image = _render(_entries(3, name="这是一个非常非常非常长的中文游戏名称用于测试截断效果是否生效"))
        pixels = image.load()
        # 双栏间隙区（不存在时取右边距）不应有文字像素
        gap_colors = Counter(pixels[x, y] for y in range(60, image.height - 60)
                             for x in range(image.width - 20, image.width))
        self.assertEqual({(24, 32, 44)}, set(gap_colors))

    def test_unknown_name_and_time_rendered(self):
        image = _render([("730", {"game_name": "", "added_at": 0, "reason": "legacy", "source": "legacy"})])
        pixels = image.load()
        colors = {pixels[x, y] for y in range(HEADER_HEIGHT + 58, HEADER_HEIGHT + 58 + ROW_HEIGHT)
                  for x in range(24, 24 + TABLE_WIDTH)}
        # 有文字像素（非底色/边框）
        self.assertTrue(len(colors) > 3)


if __name__ == "__main__":
    unittest.main()
