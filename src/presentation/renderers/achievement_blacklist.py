# -*- coding: utf-8 -*-
"""成就黑名单表格图片渲染（简易版）。

一张图输出全部条目，条目较多时自动分两栏，避免超长图片被缩放到看不清。
"""
import io

from PIL import Image, ImageDraw

from ...shared.fonts import load_truetype

BG_COLOR = (24, 32, 44)
TABLE_BG = (30, 37, 48)
HEADER_BG = (44, 62, 80)
ROW_ALT_BG = (34, 42, 54)
BORDER_COLOR = (52, 62, 76)

TITLE_COLOR = (232, 238, 245)
TEXT_COLOR = (216, 223, 232)
MUTED_COLOR = (143, 152, 160)
APPID_COLOR = (102, 192, 244)

REASON_COLORS = {
    "no_stats": (102, 192, 244),
    "fetch_failed_10x": (237, 159, 39),
    "manual": (164, 208, 7),
    "legacy": (143, 152, 160),
}
REASON_LABELS = {
    "no_stats": "无成就统计",
    "fetch_failed_10x": "当天失败10次",
    "manual": "手动添加",
    "legacy": "历史遗留",
}

PADDING = 24
COL_GAP = 18
ROW_HEIGHT = 32
HEADER_HEIGHT = 34
TITLE_HEIGHT = 58
FOOTER_HEIGHT = 44
TWO_COLUMN_THRESHOLD = 42

# 列宽：序号 / AppID / 游戏名 / 时间 / 原因
COLUMNS = (46, 116, 300, 132, 100)
TABLE_WIDTH = sum(COLUMNS)
HEADERS = ("#", "AppID", "游戏名", "加入时间", "原因")


def _text_width(draw, text, font):
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _fit_text(draw, text, font, max_width):
    text = str(text or "")
    if _text_width(draw, text, font) <= max_width:
        return text
    ellipsis = "…"
    while text and _text_width(draw, text + ellipsis, font) > max_width:
        text = text[:-1]
    return (text + ellipsis) if text else ellipsis


def _reason_label(reason):
    key = str(reason or "")
    return REASON_LABELS.get(key, key or "未知")


def _reason_color(reason):
    return REASON_COLORS.get(str(reason or ""), MUTED_COLOR)


def _format_time(stamp):
    from datetime import datetime

    try:
        value = int(stamp or 0)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        return "时间未知", True
    try:
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M"), False
    except (OSError, OverflowError, ValueError):
        return "时间未知", True


def _clean_name(name):
    text = str(name or "").strip().replace("\n", " ").replace("|", "／")
    return text or "名称未知"


def _draw_table(draw, entries, start_index, origin_x, origin_y, fonts):
    font_header, font_cell, font_small = fonts
    y = origin_y

    # 表头
    draw.rectangle(
        [origin_x, y, origin_x + TABLE_WIDTH, y + HEADER_HEIGHT], fill=HEADER_BG
    )
    x = origin_x
    for index, title in enumerate(HEADERS):
        width = COLUMNS[index]
        align = "center" if index == 0 else None
        if align == "center":
            offset = (width - _text_width(draw, title, font_header)) // 2
            draw.text((x + offset, y + 9), title, font=font_header, fill=TITLE_COLOR)
        else:
            draw.text((x + 10, y + 9), title, font=font_header, fill=TITLE_COLOR)
        x += width
    y += HEADER_HEIGHT

    # 数据行
    for row_index, (appid, meta) in enumerate(entries):
        row_no = start_index + row_index
        row_bg = ROW_ALT_BG if row_index % 2 else TABLE_BG
        draw.rectangle([origin_x, y, origin_x + TABLE_WIDTH, y + ROW_HEIGHT], fill=row_bg)

        added_text, unknown = _format_time(meta.get("added_at"))
        name = _fit_text(draw, _clean_name(meta.get("game_name")), font_cell, COLUMNS[2] - 20)
        reason = _fit_text(draw, _reason_label(meta.get("reason")), font_cell, COLUMNS[4] - 20)

        x = origin_x
        # 序号
        no_text = str(row_no)
        offset = (COLUMNS[0] - _text_width(draw, no_text, font_small)) // 2
        draw.text((x + offset, y + 9), no_text, font=font_small, fill=MUTED_COLOR)
        x += COLUMNS[0]
        # AppID
        draw.text((x + 10, y + 7), str(appid), font=font_cell, fill=APPID_COLOR)
        x += COLUMNS[1]
        # 游戏名
        draw.text((x + 10, y + 7), name, font=font_cell, fill=TEXT_COLOR)
        x += COLUMNS[2]
        # 时间
        draw.text(
            (x + 10, y + 9),
            added_text,
            font=font_small,
            fill=MUTED_COLOR if unknown else TEXT_COLOR,
        )
        x += COLUMNS[3]
        # 原因
        draw.text(
            (x + 10, y + 7), reason, font=font_cell, fill=_reason_color(meta.get("reason"))
        )

        y += ROW_HEIGHT

    # 行分隔线单独一轮绘制，避免被下一行背景覆盖
    for index in range(len(entries) + 1):
        line_y = origin_y + HEADER_HEIGHT + index * ROW_HEIGHT
        draw.line(
            [origin_x, line_y, origin_x + TABLE_WIDTH, line_y], fill=BORDER_COLOR, width=1
        )

    # 外框
    draw.rectangle(
        [origin_x, origin_y, origin_x + TABLE_WIDTH, y], outline=BORDER_COLOR, width=1
    )
    return y


def render_achievement_blacklist_image(entries, title_suffix="", font_path=None):
    """把成就黑名单条目渲染成一张表格图片，返回 PNG bytes。"""
    entries = list(entries or [])
    fallbacks = (font_path,) if font_path else ()
    font_title = load_truetype("NotoSansHans-Medium.otf", 22, fallbacks=fallbacks)
    font_header = load_truetype("NotoSansHans-Medium.otf", 15, fallbacks=fallbacks)
    font_cell = load_truetype("NotoSansHans-Regular.otf", 15, fallbacks=fallbacks)
    font_small = load_truetype("NotoSansHans-Regular.otf", 12, fallbacks=fallbacks)

    measure_img = Image.new("RGB", (1, 1))
    measure_draw = ImageDraw.Draw(measure_img)

    two_columns = len(entries) > TWO_COLUMN_THRESHOLD
    if two_columns:
        per_column = (len(entries) + 1) // 2
        left = entries[:per_column]
        right = entries[per_column:]
        rows = max(len(left), len(right))
    else:
        left, right, rows = entries, [], len(entries)

    table_height = HEADER_HEIGHT + rows * ROW_HEIGHT
    width = TABLE_WIDTH * (2 if two_columns else 1) + COL_GAP * (1 if two_columns else 0) + PADDING * 2
    height = TITLE_HEIGHT + table_height + FOOTER_HEIGHT + PADDING

    img = Image.new("RGB", (width, height), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # 标题
    count_text = f"共 {len(entries)} 条"
    draw.text((PADDING, 16), "成就黑名单", font=font_title, fill=TITLE_COLOR)
    count_w = _text_width(draw, count_text, font_header)
    draw.text((width - PADDING - count_w, 24), count_text, font=font_header, fill=MUTED_COLOR)
    if title_suffix:
        suffix_w = _text_width(draw, title_suffix, font_small)
        draw.text((width - PADDING - count_w - suffix_w - 12, 26), title_suffix, font=font_small, fill=MUTED_COLOR)

    fonts = (font_header, font_cell, font_small)
    table_top = TITLE_HEIGHT
    _draw_table(draw, left, 1, PADDING, table_top, fonts)
    if two_columns:
        _draw_table(draw, right, len(left) + 1, PADDING + TABLE_WIDTH + COL_GAP, table_top, fonts)

    # 页脚提示
    hint = "删除：/steam achievement_blacklist del <AppID>　清空：del all"
    draw.text((PADDING, height - FOOTER_HEIGHT + 10), hint, font=font_small, fill=MUTED_COLOR)

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer.getvalue()
