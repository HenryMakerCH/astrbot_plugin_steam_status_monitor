"""『新入库』合并清单卡（compact 样式）：一页多款，逐行缩略图 + 游戏名。

与 owned_game.py 的单款富卡互补：
- 单页最多 items 行，行高固定，卡片高度只随行数变化（与游戏名长度无关）；
- 封面取图失败只降级为占位块，绝不隐藏游戏名、绝不影响整图产出；
- 头像/头像框经 owned_game._open_or_none 统一处理，保留 RGBA 透明通道。
"""
import asyncio
import io

from PIL import Image, ImageDraw, ImageOps

from ...shared.fonts import load_truetype
from .game_detail import (
    STEAM_BORDER,
    STEAM_CARD,
    STEAM_GREEN_BG,
    STEAM_GREEN_TOP,
    STEAM_MUTED,
    STEAM_PAGE,
    STEAM_WHITE,
    _wrap,
)
from .game_start import (
    get_avatar_frame_path,
    get_avatar_frame_url,
    get_avatar_path,
    get_cover_path,
    get_horizontal_cover_path,
)
from .owned_game import (
    BADGE_TEXT_COLOR,
    COVER_PLACEHOLDER,
    _format_ts,
    _open_or_none,
)

BATCH_WIDTH = 820
PAD = 22
AVATAR_SIZE = 64
FRAME_SIZE = 76
HEADER_H = 76
ROW_H = 64
THUMB_W, THUMB_H = 110, 52
THUMB_X = PAD
NAME_X = THUMB_X + THUMB_W + 14
NAME_MAX_W = 548
NAME_FONT_SIZE = 17
META_FONT_SIZE = 12
FOOTER_H = 34
ROW_GAP = 8
DEFAULT_COVER_CONCURRENCY = 4
MAX_COVER_CONCURRENCY = 8

THUMB_TEXT_COLOR = (172, 219, 245, 255)
WATERMARK_COLOR = (63, 82, 104, 255)
MIN_CARD_HEIGHT = 200


def _batch_card_height(rows):
    """总高只由行数决定：游戏名长短、封面有无都不影响。"""
    card_height = PAD + HEADER_H + 16 + rows * ROW_H + (rows - 1) * ROW_GAP + 12 + FOOTER_H
    return max(card_height, MIN_CARD_HEIGHT)


def _item_rows(items):
    return len(items) if items else 1


def _cover_concurrency(value):
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = DEFAULT_COVER_CONCURRENCY
    return max(1, min(MAX_COVER_CONCURRENCY, limit or DEFAULT_COVER_CONCURRENCY))


def render_owned_games_batch_image(
    player_name,
    items,
    *,
    page=1,
    pages=1,
    owned_count=0,
    added_count=0,
    added_at=None,
    avatar=None,
    avatar_frame=None,
    covers=None,
    start_index=1,
    version=None,
):
    """同步绘制（纯内存，无网络无 IO）。

    items: [{"appid": "570", "name": "Dota 2"}, ...]
    covers: list[Image | None]，与 items 等长；None / 缺项 / 该位为 None 时画占位块。
    返回 PIL.Image（RGB）。
    """
    entries = [item if isinstance(item, dict) else {} for item in (items or [])]
    cover_list = list(covers) if covers else []
    rows = _item_rows(entries)
    card_height = _batch_card_height(rows)
    try:
        added_value = int(added_count or 0)
    except (TypeError, ValueError):
        added_value = 0
    try:
        owned_value = int(owned_count or 0)
    except (TypeError, ValueError):
        owned_value = 0

    font_name = load_truetype("NotoSansHans-Medium.otf", 22)
    font_sub = load_truetype("NotoSansHans-Regular.otf", 14)
    font_badge = load_truetype("NotoSansHans-Regular.otf", 14)
    font_row = load_truetype("NotoSansHans-Medium.otf", NAME_FONT_SIZE)
    font_meta = load_truetype("NotoSansHans-Regular.otf", META_FONT_SIZE)
    font_thumb = load_truetype("NotoSansHans-Medium.otf", 13)
    font_small = load_truetype("NotoSansHans-Regular.otf", 11)
    font_watermark = load_truetype("NotoSansHans-Regular.otf", 9)

    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    # ---------------- 头部测量 ----------------
    avatar_x = PAD
    avatar_y = PAD
    text_x = avatar_x + AVATAR_SIZE + 16
    badge_text = f"共 {owned_value} 款"
    badge_w = measure.textbbox((0, 0), badge_text, font=font_badge)[2] + 20
    badge_x = BATCH_WIDTH - PAD - badge_w
    badge_y = PAD + 12
    name_max_w = max(120, badge_x - 14 - text_x)
    name_lines = _wrap(
        measure, str(player_name or "Steam 玩家"), font_name, name_max_w, max_lines=1,
    )
    subtitle = f"新入库 {added_value} 款  ·  游戏库 {owned_value} 款"

    # ---------------- 行区几何 ----------------
    rows_top = PAD + HEADER_H + 16
    footer_top = rows_top + rows * ROW_H + (rows - 1) * ROW_GAP + 12

    image = Image.new("RGBA", (BATCH_WIDTH, card_height), STEAM_PAGE + (255,))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (0, 0, BATCH_WIDTH - 1, card_height - 1),
        radius=10, fill=STEAM_CARD + (255,), outline=STEAM_BORDER + (255,), width=2,
    )

    # ---------------- 头部绘制 ----------------
    if avatar is not None:
        try:
            avatar_img = avatar.convert("RGBA").resize(
                (AVATAR_SIZE, AVATAR_SIZE), Image.Resampling.LANCZOS
            )
            mask = Image.new("L", (AVATAR_SIZE, AVATAR_SIZE), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                (0, 0, AVATAR_SIZE, AVATAR_SIZE), radius=AVATAR_SIZE // 5, fill=255
            )
            avatar_img.putalpha(mask)
            image.alpha_composite(avatar_img, (avatar_x, avatar_y))
        except Exception:
            pass
    if avatar_frame is not None:
        try:
            frame_img = avatar_frame.convert("RGBA").resize(
                (FRAME_SIZE, FRAME_SIZE), Image.Resampling.LANCZOS
            )
            offset = (FRAME_SIZE - AVATAR_SIZE) // 2
            image.alpha_composite(frame_img, (avatar_x - offset, avatar_y - offset))
        except Exception:
            pass

    draw.text((text_x, PAD + 6), name_lines[0], font=font_name, fill=STEAM_WHITE + (255,))
    draw.text((text_x, PAD + 38), subtitle, font=font_sub, fill=STEAM_MUTED + (255,))

    draw.rounded_rectangle(
        (badge_x, badge_y, badge_x + badge_w, badge_y + 26),
        radius=4, fill=STEAM_GREEN_BG + (255,), outline=STEAM_GREEN_TOP + (255,),
    )
    draw.text((badge_x + 10, badge_y + 5), badge_text, font=font_badge,
              fill=BADGE_TEXT_COLOR + (255,))

    draw.line(
        (PAD, PAD + HEADER_H, BATCH_WIDTH - PAD, PAD + HEADER_H),
        fill=STEAM_BORDER + (255,), width=1,
    )

    # ---------------- 行绘制 ----------------
    name_line_h = max(1, measure.textbbox((0, 0), "Ag", font=font_row)[3])
    meta_line_h = max(1, measure.textbbox((0, 0), "Ag", font=font_meta)[3])

    for index, entry in enumerate(entries):
        row_y = rows_top + index * (ROW_H + ROW_GAP)
        thumb_y = row_y + (ROW_H - THUMB_H) // 2
        draw.rectangle(
            (THUMB_X, thumb_y, THUMB_X + THUMB_W, thumb_y + THUMB_H),
            fill=COVER_PLACEHOLDER + (255,), outline=STEAM_BORDER + (255,),
        )
        cover = cover_list[index] if index < len(cover_list) else None
        if isinstance(cover, Image.Image):
            try:
                fitted = ImageOps.contain(
                    cover.convert("RGB"), (THUMB_W - 2, THUMB_H - 2),
                    method=Image.Resampling.LANCZOS,
                )
                image.alpha_composite(
                    fitted.convert("RGBA"),
                    (
                        THUMB_X + (THUMB_W - fitted.width) // 2,
                        thumb_y + (THUMB_H - fitted.height) // 2,
                    ),
                )
            except Exception:
                cover = None
        if not isinstance(cover, Image.Image):
            draw.text(
                (THUMB_X + THUMB_W / 2, thumb_y + THUMB_H / 2), "STEAM",
                font=font_thumb, fill=THUMB_TEXT_COLOR, anchor="mm",
            )

        appid = str(entry.get("appid") or "").strip()
        game_name = str(entry.get("name") or "").strip() or f"AppID {appid or '未知'}"
        name_text = _wrap(measure, game_name, font_row, NAME_MAX_W, max_lines=1)[0]
        block_h = name_line_h + (meta_line_h + 2 if appid else 0)
        text_top = row_y + max(0, (ROW_H - block_h) // 2)
        draw.text((NAME_X, text_top), name_text, font=font_row, fill=STEAM_WHITE + (255,))
        if appid:
            draw.text(
                (NAME_X, text_top + name_line_h + 2), f"appid {appid}",
                font=font_meta, fill=STEAM_MUTED + (255,),
            )
        draw.text(
            (BATCH_WIDTH - PAD, row_y + ROW_H / 2), f"#{start_index + index}",
            font=font_meta, fill=STEAM_MUTED + (255,), anchor="ra",
        )
        if index < len(entries) - 1:
            divider_y = row_y + ROW_H + ROW_GAP // 2
            draw.line((NAME_X, divider_y, BATCH_WIDTH - PAD, divider_y),
                      fill=STEAM_BORDER + (255,), width=1)

    if not entries:
        draw.text(
            (BATCH_WIDTH / 2, rows_top + ROW_H / 2), "暂无新入库游戏",
            font=font_meta, fill=STEAM_MUTED + (255,), anchor="mm",
        )

    # ---------------- 底部：入库时间 / 页码 / 版本水印 ----------------
    added_text = _format_ts(added_at) if added_at else ""
    footer_text_y = footer_top + (FOOTER_H - 14) // 2 - 2
    if added_text:
        draw.text((PAD, footer_text_y), f"入库时间 {added_text}",
                  font=font_small, fill=STEAM_MUTED + (255,))

    watermark_x = BATCH_WIDTH - 14
    if version:
        v_text = f"v{version}"
        v_w = draw.textbbox((0, 0), v_text, font=font_watermark)[2]
        watermark_x = BATCH_WIDTH - v_w - 14
        draw.text((watermark_x, footer_text_y), v_text,
                  font=font_watermark, fill=WATERMARK_COLOR)

    try:
        page_no = int(page or 1)
        page_total = int(pages or 1)
    except (TypeError, ValueError):
        page_no, page_total = 1, 1
    if page_total > 1:
        draw.text(
            (watermark_x - 16, footer_text_y), f"第 {page_no}/{page_total} 页",
            font=font_small, fill=STEAM_MUTED + (255,), anchor="ra",
        )

    return image.convert("RGB")


async def _resolve_frame(data_dir, sid, proxy):
    path = await get_avatar_frame_path(data_dir, str(sid), proxy=proxy)
    if path:
        return path
    url = await get_avatar_frame_url(str(sid), proxy=proxy)
    if not url:
        return None
    return await get_avatar_frame_path(data_dir, str(sid), url, proxy=proxy)


async def _resolve_cover(
    data_dir,
    entry,
    *,
    api_key=None,
    sgdb_api_key=None,
    sgdb_api_base=None,
    steam_store_base=None,
    steam_api_base=None,
    proxy=None,
):
    """单款封面：先横版缓存，未命中再竖版/SGDB 兜底；任何异常都降级为 None。"""
    appid = entry.get("appid")
    game_name = str(entry.get("name") or "").strip()
    key = str(appid or "")
    if not key:
        return None
    path = await get_horizontal_cover_path(
        data_dir, key, appid=appid, proxy=proxy, steam_store_base=steam_store_base,
    )
    image = _open_or_none(path)
    if image is not None:
        return image
    path = await get_cover_path(
        data_dir, key, game_name, sgdb_api_key=sgdb_api_key,
        appid=appid, proxy=proxy, api_key=api_key,
        sgdb_api_base=sgdb_api_base, steam_api_base=steam_api_base,
    )
    return _open_or_none(path)


async def render_owned_games_batch(
    data_dir,
    sid,
    player_name,
    avatar_url,
    *,
    items,
    page=1,
    pages=1,
    owned_count=0,
    added_count=None,
    added_at=None,
    api_key=None,
    sgdb_api_key=None,
    sgdb_api_base=None,
    steam_store_base=None,
    steam_api_base=None,
    proxy=None,
    version=None,
    page_size=0,
    cover_concurrency=DEFAULT_COVER_CONCURRENCY,
):
    """异步入口：并发取头像/头像框/本页封面后绘制，返回 PNG bytes。

    page_size 用于推算本页起始序号（跨页连续编号）；不传则按本页行数推算。
    """
    entries = [item if isinstance(item, dict) else {} for item in (items or [])]
    if added_count is None:
        added_count = len(entries)
    limit = _cover_concurrency(cover_concurrency)
    semaphore = asyncio.Semaphore(limit)

    async def one_cover(entry):
        async with semaphore:
            try:
                return await _resolve_cover(
                    data_dir, entry,
                    api_key=api_key, sgdb_api_key=sgdb_api_key,
                    sgdb_api_base=sgdb_api_base, steam_store_base=steam_store_base,
                    steam_api_base=steam_api_base, proxy=proxy,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                return None

    async def one_frame():
        try:
            return await _resolve_frame(data_dir, sid, proxy)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None

    results = await asyncio.gather(
        get_avatar_path(data_dir, str(sid), avatar_url, proxy=proxy),
        one_frame(),
        *(one_cover(entry) for entry in entries),
        return_exceptions=True,
    )

    avatar_value = results[0]
    frame_value = results[1]
    covers = []
    for value in results[2:]:
        if isinstance(value, BaseException):
            covers.append(None)
            continue
        covers.append(_open_or_none(value))

    # 序号跨页连续：按每页容量推算本页起始序号，末页只有 partial 行也不会错位。
    # 调用方不传 page_size 时退化为「按本页行数推算」，仍优于每页从 #1 重来。
    per_page = len(entries)
    try:
        declared = int(page_size or 0)
    except (TypeError, ValueError):
        declared = 0
    if declared > 0:
        per_page = declared
    start_index = (max(1, int(page or 1)) - 1) * max(1, per_page) + 1

    image = render_owned_games_batch_image(
        player_name,
        entries,
        page=page,
        pages=pages,
        owned_count=owned_count,
        added_count=added_count,
        added_at=added_at,
        avatar=_open_or_none(avatar_value),
        avatar_frame=_open_or_none(frame_value),
        covers=covers,
        start_index=start_index,
        version=version,
    )
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
