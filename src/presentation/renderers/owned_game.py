"""『新入库』播报富卡（方案 A：Steam 深蓝详情风）。

元素：玩家头像 + 头像框 + 名称 + 游戏库总数 + 新入库游戏标题 + 商店描述 + 双语言评价。
商店数据（card_data）可选，缺失时自动降级为精简版；封面三级回退：
商店 header → covers_h 横版缓存 → covers_v 竖版/默认封面兜底。
"""
import asyncio
import io
import os
from datetime import datetime

from PIL import Image, ImageDraw, ImageOps

from ...shared.fonts import load_truetype
from .game_detail import (
    STEAM_BLUE,
    STEAM_BORDER,
    STEAM_CARD,
    STEAM_GREEN_BG,
    STEAM_GREEN_TOP,
    STEAM_MUTED,
    STEAM_PAGE,
    STEAM_TEXT,
    STEAM_WHITE,
    _download_image,
    _plain_text,
    _wrap,
)
from .game_start import (
    get_avatar_frame_path,
    get_avatar_frame_url,
    get_avatar_path,
    get_cover_path,
    get_horizontal_cover_path,
)

CARD_WIDTH = 820
LEFT_X = 22
LEFT_W = 300
RIGHT_X = 344
RIGHT_W = 454
AVATAR_SIZE = 64
FRAME_SIZE = AVATAR_SIZE + 12
BADGE_TEXT_COLOR = (210, 243, 76)
COVER_PLACEHOLDER = (31, 48, 65)


def _review_color(text):
    """评分等级 → 颜色（与价格卡保持一致）。"""
    if text == "好评如潮":
        return (129, 173, 81)
    if text in ("特别好评", "多半好评"):
        return (130, 200, 90)
    if text == "褒贬不一":
        return (222, 180, 60)
    if text == "多半差评":
        return (225, 120, 70)
    if text in ("差评", "差评如潮"):
        return (225, 80, 80)
    return STEAM_BLUE


def _format_ts(timestamp):
    try:
        return datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _open_or_none(image_or_path):
    """把『路径 / Image / 异常』统一成打开好的 RGBA Image 或 None（保留透明通道，避免头像框透明区变黑）。"""
    if image_or_path is None or isinstance(image_or_path, BaseException):
        return None
    if isinstance(image_or_path, Image.Image):
        return image_or_path
    path = str(image_or_path)
    if not path or not os.path.exists(path):
        return None
    try:
        return Image.open(path).convert("RGBA")
    except Exception:
        return None


def render_owned_game_image(
    player_name,
    game_name,
    *,
    avatar=None,
    avatar_frame=None,
    cover=None,
    card_data=None,
    owned_count=0,
    added_at=None,
    version=None,
):
    """同步绘制（纯内存，无网络）；失败降级由调用方保证传入 None。"""
    card_data = card_data or {}
    font_name = load_truetype("NotoSansHans-Medium.otf", 24)
    font_label = load_truetype("NotoSansHans-Regular.otf", 14)
    font_body = load_truetype("NotoSansHans-Regular.otf", 16)
    font_review = load_truetype("NotoSansHans-Regular.otf", 16)
    font_tag = load_truetype("NotoSansHans-Regular.otf", 13)
    font_mini = load_truetype("NotoSansHans-Regular.otf", 11)
    font_badge = load_truetype("NotoSansHans-Regular.otf", 14)
    font_new = load_truetype("NotoSansHans-Regular.otf", 18)

    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    # ---------------- 左列测量 ----------------
    name_x = LEFT_X + (AVATAR_SIZE + 16 if avatar is not None else 0)
    name_lines = _wrap(measure, str(player_name or "Steam 玩家"), font_name,
                       max(120, LEFT_X + LEFT_W - name_x), max_lines=2)
    badge_text = f"游戏库 {int(owned_count or 0)} 款"
    badge_w = measure.textbbox((0, 0), badge_text, font=font_badge)[2] + 20

    title = str(game_name or "未知游戏")
    title_font = None
    title_lines = []
    for size in (26, 22, 18):
        candidate = load_truetype("NotoSansHans-Medium.otf", size)
        lines = _wrap(measure, title, candidate, LEFT_W - 4)
        if len(lines) <= 2:
            title_font, title_lines = candidate, lines
            break
    if title_font is None:
        title_font = load_truetype("NotoSansHans-Medium.otf", 18)
        title_lines = _wrap(measure, title, title_font, LEFT_W - 4, max_lines=2)

    english_name = str(card_data.get("english_name") or card_data.get("original_name") or "")
    review_all = card_data.get("review_all") or {}
    review_zh = card_data.get("review_schinese") or {}
    release_date = str((card_data.get("release_date") or {}).get("date") or "").strip()
    has_reviews = bool(
        (review_all.get("text") or review_all.get("percent") is not None)
        or (review_zh.get("text") or review_zh.get("percent") is not None)
    )

    # ---------------- 右列测量 ----------------
    description = _plain_text(card_data.get("short_description") or "") if card_data else ""
    desc_lines = _wrap(measure, description, font_body, RIGHT_W - 8, max_lines=6) if description else []
    tags = []
    if card_data:
        genres = [x.get("description", "") for x in card_data.get("genres") or [] if x.get("description")]
        categories = [x.get("description", "") for x in card_data.get("categories") or [] if x.get("description")]
        tags = list(dict.fromkeys(genres + categories))[:8]
    developer = "、".join(card_data.get("developers") or []) if card_data else ""
    publisher = "、".join(card_data.get("publishers") or []) if card_data else ""

    cover_w = RIGHT_W
    cover_h = 216
    if cover is not None:
        ratio = cover.height / max(1, cover.width)
        cover_h = max(120, min(240, round(cover_w * ratio)))

    right_bottom = 22 + cover_h + 14
    if desc_lines:
        right_bottom += len(desc_lines) * 24 + 8
    tag_rows = 0
    tag_x = 0
    for tag in tags:
        tag_width = measure.textbbox((0, 0), tag, font=font_tag)[2] + 18
        if tag_x and tag_x + tag_width > RIGHT_W:
            tag_rows += 1
            tag_x = 0
        tag_x += tag_width + 6
    if tags:
        tag_rows += 1
        right_bottom += tag_rows * 28 + 6
    if developer or publisher:
        right_bottom += 22

    # ---------------- 左列堆叠 ----------------
    left_y = 22 + max(AVATAR_SIZE, len(name_lines) * 34)
    badge_top = left_y + 16
    left_y = badge_top + 26 + 12
    divider_y = left_y
    left_y += 12 + 24  # 「新入库」
    title_top = left_y
    line_h = title_font.size + 8
    left_y += len(title_lines) * line_h
    if english_name:
        left_y += 22
    if has_reviews:
        left_y += 16 + 24 * 2
    if release_date:
        left_y += 8 + 22

    card_height = max(left_y, right_bottom) + 46  # 底部留时间/水印条

    image = Image.new("RGBA", (CARD_WIDTH, card_height), STEAM_PAGE + (255,))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (0, 0, CARD_WIDTH - 1, card_height - 1),
        radius=10, fill=STEAM_CARD + (255,), outline=STEAM_BORDER + (255,), width=2,
    )

    # ---------------- 左列绘制 ----------------
    avatar_y = 22
    if avatar is not None:
        try:
            avatar_img = avatar.convert("RGBA").resize((AVATAR_SIZE, AVATAR_SIZE), Image.Resampling.LANCZOS)
            mask = Image.new("L", (AVATAR_SIZE, AVATAR_SIZE), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                (0, 0, AVATAR_SIZE, AVATAR_SIZE), radius=AVATAR_SIZE // 5, fill=255
            )
            avatar_img.putalpha(mask)
            image.alpha_composite(avatar_img, (LEFT_X, avatar_y))
        except Exception:
            pass
    if avatar_frame is not None:
        try:
            frame_img = avatar_frame.convert("RGBA").resize((FRAME_SIZE, FRAME_SIZE), Image.Resampling.LANCZOS)
            offset = (FRAME_SIZE - AVATAR_SIZE) // 2
            image.alpha_composite(frame_img, (LEFT_X - offset, avatar_y - offset))
        except Exception:
            pass
    for index, line in enumerate(name_lines):
        draw.text((name_x, 26 + index * 34), line, font=font_name, fill=STEAM_WHITE + (255,))

    draw.rounded_rectangle(
        (LEFT_X, badge_top, LEFT_X + badge_w, badge_top + 26),
        radius=4, fill=STEAM_GREEN_BG + (255,), outline=STEAM_GREEN_TOP + (255,),
    )
    draw.text((LEFT_X + 10, badge_top + 5), badge_text, font=font_badge, fill=BADGE_TEXT_COLOR + (255,))

    draw.line((LEFT_X, divider_y, LEFT_X + LEFT_W, divider_y), fill=STEAM_BORDER + (255,), width=1)
    draw.text((LEFT_X, divider_y + 12), "新入库", font=font_new, fill=STEAM_BLUE + (255,))

    text_y = title_top
    for line in title_lines:
        draw.text((LEFT_X, text_y), line, font=title_font, fill=STEAM_WHITE + (255,))
        text_y += line_h
    if english_name:
        draw.text((LEFT_X, text_y + 2), english_name[:44], font=font_label, fill=STEAM_MUTED + (255,))
        text_y += 22
    if has_reviews:
        text_y += 16
        for label, review in (("全部评测", review_all), ("中文评测", review_zh)):
            verdict = review.get("text") or "暂无评价"
            extra = []
            if review.get("percent") is not None:
                extra.append(f"{review['percent']}%")
            if review.get("total"):
                extra.append(f"({review['total']:,})")
            draw.text((LEFT_X, text_y), label, font=font_label, fill=STEAM_MUTED + (255,))
            draw.text((LEFT_X + 76, text_y - 1), verdict, font=font_review, fill=_review_color(verdict) + (255,))
            if extra:
                draw.text((LEFT_X + 168, text_y), " ".join(extra), font=font_label, fill=STEAM_MUTED + (255,))
            text_y += 24
    if release_date:
        text_y += 8
        draw.text((LEFT_X, text_y), "发行日期", font=font_label, fill=STEAM_MUTED + (255,))
        draw.text((LEFT_X + 66, text_y), release_date, font=font_label, fill=STEAM_TEXT + (255,))

    # ---------------- 右列绘制 ----------------
    cover_top = 22
    draw.rectangle(
        (RIGHT_X, cover_top, RIGHT_X + cover_w, cover_top + cover_h),
        fill=COVER_PLACEHOLDER + (255,), outline=STEAM_BORDER + (255,),
    )
    if cover is not None:
        try:
            fitted = ImageOps.contain(
                cover.convert("RGB"), (cover_w - 2, cover_h - 2), method=Image.Resampling.LANCZOS
            )
            image.alpha_composite(
                fitted.convert("RGBA"),
                (RIGHT_X + (cover_w - fitted.width) // 2, cover_top + (cover_h - fitted.height) // 2),
            )
        except Exception:
            pass
    else:
        draw.text(
            (RIGHT_X + cover_w / 2, cover_top + cover_h / 2), "STEAM",
            font=load_truetype("NotoSansHans-Medium.otf", 25), fill=(172, 219, 245, 255), anchor="mm",
        )

    y = cover_top + cover_h + 14
    if desc_lines:
        for line in desc_lines:
            draw.text((RIGHT_X, y), line, font=font_body, fill=STEAM_TEXT + (255,))
            y += 24
        y += 8
    if tags:
        tag_x = RIGHT_X
        for tag in tags:
            tag_width = measure.textbbox((0, 0), tag, font=font_tag)[2] + 18
            if tag_x + tag_width > RIGHT_X + RIGHT_W:
                tag_x, y = RIGHT_X, y + 28
            draw.rounded_rectangle(
                (tag_x, y, tag_x + tag_width, y + 23), radius=3,
                fill=(31, 54, 72, 255), outline=(55, 91, 116, 255),
            )
            draw.text(
                (tag_x + tag_width / 2, y + 11), tag, font=font_tag,
                fill=(172, 219, 245, 255), anchor="mm",
            )
            tag_x += tag_width + 6
        y += 34
    if developer or publisher:
        line = f"开发商：{developer or '未知'}    发行商：{publisher or '未知'}"
        draw.text((RIGHT_X, y), line[:64], font=font_label, fill=STEAM_MUTED + (255,))

    # ---------------- 底部：入库时间 + 版本号水印 ----------------
    added_text = f"入库时间 {_format_ts(added_at)}" if added_at else ""
    if added_text:
        draw.text((LEFT_X, card_height - 32), added_text, font=font_mini, fill=STEAM_MUTED + (255,))
    if version:
        v_text = f"v{version}"
        v_font = load_truetype("NotoSansHans-Regular.otf", 9)
        v_w = draw.textbbox((0, 0), v_text, font=v_font)[2]
        draw.text((CARD_WIDTH - v_w - 14, card_height - 30), v_text, font=v_font, fill=(63, 82, 104, 255))

    return image.convert("RGB")


async def render_owned_game(
    data_dir,
    sid,
    player_name,
    avatar_url,
    appid,
    game_name,
    *,
    owned_count=0,
    card_data=None,
    added_at=None,
    api_key=None,
    sgdb_api_key=None,
    sgdb_api_base=None,
    steam_store_base=None,
    steam_api_base=None,
    proxy=None,
    version=None,
):
    """异步入口：并发取头像/头像框/封面后绘制，返回 PNG bytes。"""

    async def resolve_frame():
        path = await get_avatar_frame_path(data_dir, str(sid), proxy=proxy)
        if path:
            return path
        url = await get_avatar_frame_url(str(sid), proxy=proxy)
        if not url:
            return None
        return await get_avatar_frame_path(data_dir, str(sid), url, proxy=proxy)

    async def resolve_cover():
        header = (card_data or {}).get("header_image") or (card_data or {}).get("image")
        if header:
            image = await _download_image(header, proxy)
            if image is not None:
                return image
        path = await get_horizontal_cover_path(
            data_dir, str(appid), appid=appid, proxy=proxy, steam_store_base=steam_store_base,
        )
        if path and os.path.exists(path):
            try:
                return Image.open(path).convert("RGB")
            except Exception:
                pass
        path = await get_cover_path(
            data_dir, str(appid), str(game_name or ""), sgdb_api_key=sgdb_api_key,
            appid=appid, proxy=proxy, api_key=api_key,
            sgdb_api_base=sgdb_api_base, steam_api_base=steam_api_base,
        )
        if path and os.path.exists(path):
            try:
                return Image.open(path).convert("RGB")
            except Exception:
                pass
        return None

    avatar_value, frame_value, cover_value = await asyncio.gather(
        get_avatar_path(data_dir, str(sid), avatar_url, proxy=proxy),
        resolve_frame(),
        resolve_cover(),
        return_exceptions=True,
    )
    image = render_owned_game_image(
        player_name,
        game_name,
        avatar=_open_or_none(avatar_value),
        avatar_frame=_open_or_none(frame_value),
        cover=None if isinstance(cover_value, BaseException) else cover_value,
        card_data=card_data,
        owned_count=owned_count,
        added_at=added_at,
        version=version,
    )
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
