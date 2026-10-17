"""新入库富卡渲染：结构、降级与资源缺失兜底（不依赖字体包与网络）。"""
import io
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image, ImageDraw

from src.presentation.renderers.owned_game import (
    _open_or_none,
    render_owned_game,
    render_owned_game_image,
)

CARD_DATA = {
    "name": "恸哭幽光",
    "short_description": "一段用于测试的商店描述文案，重复出现以验证多行换行与截断逻辑。" * 4,
    "genres": [{"description": "冒险"}, {"description": "独立"}],
    "categories": [{"description": "单人"}, {"description": "成就"}],
    "developers": ["Studio"],
    "publishers": ["Publisher"],
    "release_date": {"date": "2026-10-01"},
    "review_all": {"text": "特别好评", "percent": 92, "total": 12345},
    "review_schinese": {"text": "好评如潮", "percent": 96, "total": 321},
}


class RenderOwnedGameImageTests(unittest.TestCase):
    def _assert_png(self, image, width=820):
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        buffer.seek(0)
        loaded = Image.open(buffer)
        self.assertEqual(loaded.size[0], width)
        self.assertGreaterEqual(loaded.size[1], 280)  # 降级版约 298，富化版更高

    def test_full_card_with_store_data(self):
        image = render_owned_game_image(
            "猫猫猫猫猫耳丶", "恸哭幽光",
            card_data=CARD_DATA, owned_count=268, added_at=1760000000, version="4.10.0",
        )
        self._assert_png(image)

    def test_card_degrades_without_store_data(self):
        image = render_owned_game_image("玩家", "未知游戏", card_data=None, owned_count=0)
        self._assert_png(image)

    def test_long_text_and_missing_assets_do_not_crash(self):
        image = render_owned_game_image(
            "非常非常非常非常非常长的玩家名字用于测试换行",
            "超长游戏名称测试第二行展示超长游戏名称测试" * 2,
            card_data={"short_description": ""}, owned_count=1,
        )
        self._assert_png(image)


class RenderOwnedGameWrapperTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrapper_offline_fallbacks_to_placeholder(self):
        with patch("src.presentation.renderers.owned_game.get_avatar_path", new=AsyncMock(return_value=None)), \
             patch("src.presentation.renderers.owned_game.get_avatar_frame_path", new=AsyncMock(return_value=None)), \
             patch("src.presentation.renderers.owned_game.get_avatar_frame_url", new=AsyncMock(return_value=None)), \
             patch("src.presentation.renderers.owned_game._download_image", new=AsyncMock(return_value=None)), \
             patch("src.presentation.renderers.owned_game.get_horizontal_cover_path", new=AsyncMock(return_value=None)), \
             patch("src.presentation.renderers.owned_game.get_cover_path", new=AsyncMock(return_value=None)):
            data = await render_owned_game(
                ".", "76561198000000001", "玩家", None, 570, "Dota 2",
                owned_count=3, card_data=None, added_at=0, version="4.10.0",
            )

        self.assertTrue(data.startswith(b"\x89PNG"))


class AvatarFrameAlphaTests(unittest.TestCase):
    def test_frame_transparency_kept_and_avatar_not_black(self):
        """回归：头像框 PNG 透明区曾因转 RGB 变黑，导致头像位置被黑色方块覆盖。"""
        frame = Image.new("RGBA", (76, 76), (0, 0, 0, 0))
        ImageDraw.Draw(frame).ellipse((1, 1, 74, 74), outline=(255, 200, 0, 255), width=6)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "frame.png")
            frame.save(path)
            loaded = _open_or_none(path)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.mode, "RGBA")
        self.assertEqual(loaded.getpixel((38, 38))[3], 0)  # 中心保持透明

        avatar = Image.new("RGBA", (64, 64), (200, 30, 30, 255))
        image = render_owned_game_image(
            "玩家", "测试游戏", avatar=avatar, avatar_frame=loaded, card_data=None, owned_count=1,
        )
        center = image.getpixel((54, 54))
        self.assertGreater(center[0], 120)  # 头像红色可见
        self.assertLess(center[1], 90)      # 未被黑色覆盖


if __name__ == "__main__":
    unittest.main()
