"""启动游戏卡的版本号与 APPID 徽标渲染。"""
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image, ImageChops

from src.presentation.renderers import game_start as gs
from src.presentation.renderers.game_start import render_game_start, render_game_start_image
from src.shared.fonts import load_truetype


def _render(**kwargs):
    base = dict(player_name="Alice", avatar_path=None, game_name="CS2", cover_path=None)
    base.update(kwargs)
    return render_game_start_image(**base)


class GameStartBadgeTests(unittest.TestCase):
    def test_version_uses_7px_and_is_bright(self):
        with patch.object(gs, "load_truetype", side_effect=load_truetype) as mocked:
            image = _render(version="4.11.2")
        sizes = [call.args[1] for call in mocked.call_args_list if len(call.args) > 1]
        self.assertIn(7, sizes)
        crop = image.crop((image.width - 50, image.height - 32, image.width, image.height))
        self.assertGreater(max(max(pixel) for pixel in crop.getdata()), 120)

    def test_gameid_badge_drawn_at_bottom_right(self):
        plain = _render()
        with_id = _render(game_id="570")
        bbox = ImageChops.difference(plain, with_id).getbbox()
        self.assertIsNotNone(bbox)
        self.assertGreaterEqual(bbox[0], with_id.width - 90)
        self.assertLessEqual(bbox[2], with_id.width - 4)
        self.assertGreaterEqual(bbox[1], 180)
        self.assertLessEqual(bbox[3], 191)

    def test_version_stacked_above_gameid(self):
        only_id = _render(game_id="570")
        both = _render(game_id="570", version="4.11.2")
        bbox = ImageChops.difference(only_id, both).getbbox()
        self.assertIsNotNone(bbox)
        self.assertGreaterEqual(bbox[1], 165)
        self.assertLessEqual(bbox[3], 181)
        self.assertGreaterEqual(bbox[2], both.width - 40)

    def test_empty_game_id_draws_nothing(self):
        diff = ImageChops.difference(_render(game_id=None), _render(game_id=""))
        self.assertIsNone(diff.getbbox())

    def test_long_name_640_canvas_keeps_badge_at_bottom(self):
        with tempfile.TemporaryDirectory() as tmp:
            cover = os.path.join(tmp, "cover.jpg")
            Image.new("RGB", (600, 900), (20, 30, 40)).save(cover)
            base = dict(
                player_name="W" * 24,
                game_name="CS2",
                cover_path=cover,
                playtime_hours=100.5,
                version=None,
            )
            plain = _render(game_id=None, **base)
            with_id = _render(game_id="1091500", **base)
        self.assertEqual((640, 192), with_id.size)
        bbox = ImageChops.difference(plain, with_id).getbbox()
        self.assertIsNotNone(bbox)
        self.assertGreaterEqual(bbox[0], 550)
        self.assertGreaterEqual(bbox[1], 181)
        self.assertLessEqual(bbox[3], 191)


class RenderGameStartWiringTests(unittest.IsolatedAsyncioTestCase):
    async def _forwarded_game_id(self, **kwargs):
        with patch.object(
            gs, "render_game_start_image", return_value=Image.new("RGB", (512, 192))
        ) as renderer, patch.object(
            gs, "get_avatar_path", new=AsyncMock(return_value=None)
        ), patch.object(
            gs, "get_cover_path", new=AsyncMock(return_value=None)
        ), patch.object(
            gs, "get_horizontal_cover_path", new=AsyncMock(return_value=None)
        ), patch.object(
            gs, "get_avatar_frame_path", new=AsyncMock(return_value=None)
        ), patch.object(
            gs, "get_avatar_frame_url", new=AsyncMock(return_value=None)
        ):
            await render_game_start(
                ".", "76561198000000001", "Alice", None, 730, "CS2",
                version="4.11.2", **kwargs
            )
        return renderer.call_args.kwargs["game_id"]

    async def test_appid_is_preferred(self):
        self.assertEqual("570", await self._forwarded_game_id(appid=570))

    async def test_gameid_is_fallback_when_appid_missing(self):
        self.assertEqual("730", await self._forwarded_game_id())


if __name__ == "__main__":
    unittest.main()
