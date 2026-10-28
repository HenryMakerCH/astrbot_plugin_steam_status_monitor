"""简洁模式合并清单图：尺寸契约、行数决定高度、封面降级与并发上限。"""
import asyncio
import io
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image, ImageDraw

from src.presentation.renderers.owned_games_batch import (
    BATCH_WIDTH,
    render_owned_games_batch,
    render_owned_games_batch_image,
)

ITEMS = [{"appid": str(570 + index), "name": f"游戏{index}"} for index in range(8)]


class BatchImageGeometryTests(unittest.TestCase):
    def test_width_is_always_820(self):
        for count in (0, 1, 5, 8):
            items = ITEMS[:count]
            image = render_owned_games_batch_image("玩家", items, owned_count=100)
            self.assertEqual(image.size[0], BATCH_WIDTH, msg=f"count={count}")

    def test_height_depends_only_on_row_count(self):
        one = render_owned_games_batch_image("玩家", ITEMS[:1], owned_count=1)
        eight = render_owned_games_batch_image("玩家", ITEMS[:8], owned_count=1)
        self.assertLess(one.size[1], eight.size[1])
        # 1 行 ~224、8 行 ~728
        self.assertAlmostEqual(one.size[1], 224, delta=6)
        self.assertAlmostEqual(eight.size[1], 728, delta=6)

    def test_long_name_does_not_wrap_to_two_lines(self):
        """长名必须单行省略。

        直接对比「短名」与「超长名」在名称带区域的像素分布：若长名换行，
        名称带下方（appid 行上沿之前的空隙）会出现明显增多的文字像素。
        """
        long_name = "超长游戏名称测试" * 10
        short_img = render_owned_games_batch_image("玩家", [{"appid": "1", "name": "短名"}])
        long_img = render_owned_games_batch_image("玩家", [{"appid": "1", "name": long_name}])
        self.assertEqual(short_img.size[1], long_img.size[1])

        # 名称单行的字高约 17px，起点 row_top+12（rows_top = 22+76+16 = 114）
        # 若换行，第二行会挤进 row_top+34 之后、appid 行之前。
        def ink_in_gap(image):
            region = image.convert("RGB").crop((146, 114 + 34, 700, 114 + 46))
            return sum(1 for pixel in region.getdata() if max(pixel) > 150)

        self.assertLess(
            ink_in_gap(long_img), 20,
            msg="名称带下方空隙出现文字像素，说明长名被换行而非单行截断",
        )
        self.assertLessEqual(ink_in_gap(short_img), ink_in_gap(long_img) + 20)

    def test_covers_none_and_empty_entries_render(self):
        for covers in (None, [None] * 4, []):
            image = render_owned_games_batch_image(
                "玩家", ITEMS[:4], covers=covers, owned_count=10,
            )
            self.assertEqual(image.size[0], BATCH_WIDTH)

    def test_empty_items_still_produces_image(self):
        image = render_owned_games_batch_image("玩家", [], owned_count=0)
        self.assertGreaterEqual(image.size[1], 200)

    def test_page_footer_changes_pixels(self):
        single = render_owned_games_batch_image("玩家", ITEMS[:2], page=1, pages=1)
        paged = render_owned_games_batch_image("玩家", ITEMS[:2], page=2, pages=5)
        self.assertNotEqual(single.tobytes(), paged.tobytes())

    def test_avatar_frame_transparency_kept(self):
        """回归：头像框 PNG 透明区曾在转 RGB 后变黑，盖住头像。"""
        frame = Image.new("RGBA", (76, 76), (0, 0, 0, 0))
        ImageDraw.Draw(frame).ellipse((1, 1, 74, 74), outline=(255, 200, 0, 255), width=6)
        avatar = Image.new("RGBA", (64, 64), (200, 30, 30, 255))
        image = render_owned_games_batch_image(
            "玩家", ITEMS[:1], avatar=avatar, avatar_frame=frame, owned_count=1,
        ).convert("RGB")
        pixel = image.getpixel((54, 54))
        self.assertGreater(pixel[0], 120)  # 头像红色仍可见
        self.assertLess(pixel[1], 90)      # 未被黑色覆盖


class BatchWrapperTests(unittest.IsolatedAsyncioTestCase):
    async def test_cover_exception_still_returns_png(self):
        with patch(
            "src.presentation.renderers.owned_games_batch.get_horizontal_cover_path",
            new=AsyncMock(side_effect=RuntimeError("cover down")),
        ), patch(
            "src.presentation.renderers.owned_games_batch.get_cover_path",
            new=AsyncMock(side_effect=RuntimeError("cover down")),
        ), patch(
            "src.presentation.renderers.owned_games_batch.get_avatar_path",
            new=AsyncMock(return_value=None),
        ), patch(
            "src.presentation.renderers.owned_games_batch.get_avatar_frame_path",
            new=AsyncMock(return_value=None),
        ), patch(
            "src.presentation.renderers.owned_games_batch.get_avatar_frame_url",
            new=AsyncMock(return_value=None),
        ):
            data = await render_owned_games_batch(
                ".", "76561198000000001", "玩家", None,
                items=ITEMS[:3], owned_count=5, added_count=3,
            )

        self.assertTrue(data.startswith(b"\x89PNG"))

    async def test_cover_concurrency_is_capped(self):
        state = {"current": 0, "peak": 0}
        release = asyncio.Event()

        async def slow_horizontal(_data_dir, _appid, **_kwargs):
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
            try:
                await asyncio.sleep(0)
                await release.wait()
            finally:
                state["current"] -= 1
            return None

        with patch(
            "src.presentation.renderers.owned_games_batch.get_horizontal_cover_path",
            new=slow_horizontal,
        ), patch(
            "src.presentation.renderers.owned_games_batch.get_cover_path",
            new=AsyncMock(return_value=None),
        ), patch(
            "src.presentation.renderers.owned_games_batch.get_avatar_path",
            new=AsyncMock(return_value=None),
        ), patch(
            "src.presentation.renderers.owned_games_batch.get_avatar_frame_path",
            new=AsyncMock(return_value=None),
        ), patch(
            "src.presentation.renderers.owned_games_batch.get_avatar_frame_url",
            new=AsyncMock(return_value=None),
        ):
            task = asyncio.create_task(render_owned_games_batch(
                ".", "76561198000000001", "玩家", None,
                items=ITEMS, owned_count=8, added_count=8, cover_concurrency=2,
            ))
            await asyncio.sleep(0.05)
            release.set()
            await task

        self.assertLessEqual(state["peak"], 2)
        self.assertGreaterEqual(state["peak"], 1)

    async def test_all_resources_missing_still_rasterizes(self):
        with patch(
            "src.presentation.renderers.owned_games_batch.get_horizontal_cover_path",
            new=AsyncMock(return_value=None),
        ), patch(
            "src.presentation.renderers.owned_games_batch.get_cover_path",
            new=AsyncMock(return_value=None),
        ), patch(
            "src.presentation.renderers.owned_games_batch.get_avatar_path",
            new=AsyncMock(return_value=None),
        ), patch(
            "src.presentation.renderers.owned_games_batch.get_avatar_frame_path",
            new=AsyncMock(return_value=None),
        ), patch(
            "src.presentation.renderers.owned_games_batch.get_avatar_frame_url",
            new=AsyncMock(return_value=None),
        ):
            data = await render_owned_games_batch(
                ".", "76561198000000001", "玩家", None,
                items=[], owned_count=0, added_count=0,
            )

        loaded = Image.open(io.BytesIO(data))
        self.assertEqual(loaded.size[0], BATCH_WIDTH)


if __name__ == "__main__":
    unittest.main()
