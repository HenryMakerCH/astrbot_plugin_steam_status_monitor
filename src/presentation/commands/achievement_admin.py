"""`/steam achievement_blacklist` 成就查询黑名单管理。"""
import re
import tempfile
from datetime import datetime

from ...domain.monitoring.game_filter import parse_appids
from ...presentation.renderers.achievement_blacklist import render_achievement_blacklist_image
from ...shared.fonts import resolve_font_path
from ...shared.logging import logger

PAGE_SIZE = 20

_ABL_TAIL_RE = re.compile(
    r"^[/.。／]*\s*(?:steam\s+)?"
    r"(?:achievement_blacklist|achievement_bl|achbl|abl)\b\s*(.*)$",
    re.IGNORECASE,
)

_REASON_LABELS = {
    "no_stats": "无成就统计",
    "fetch_failed_10x": "当天失败10次",
    "manual": "手动添加",
    "legacy": "历史遗留",
}

_USAGE = (
    "用法：/steam achievement_blacklist [list [text]] ｜ "
    "/steam achievement_blacklist names ｜ "
    "/steam achievement_blacklist del 730,570 ｜ "
    "/steam achievement_blacklist del all"
)
_HINT = "提示：删除单个用 del <AppID>；清空全部用 del all；补全游戏名用 names。"


def _event_message_text(event) -> str:
    raw = getattr(event, "message_str", None)
    if raw is None:
        getter = getattr(event, "get_message_str", None)
        raw = getter() if callable(getter) else ""
    return str(raw or "").strip()


def _args_text(event, action: str = "", value: str = "") -> str:
    """优先读原始消息尾部，避免框架按声明参数个数静默丢弃 token。"""
    match = _ABL_TAIL_RE.match(_event_message_text(event))
    if match:
        return match.group(1).strip()
    return " ".join(str(x).strip() for x in (action, value) if str(x).strip())


def _reason_label(reason) -> str:
    key = str(reason or "")
    return _REASON_LABELS.get(key, key or "未知原因")


def _format_time(stamp) -> str:
    try:
        value = int(stamp or 0)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        return "时间未知"
    try:
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "时间未知"


def _safe_name(name) -> str:
    text = re.sub(r"\s+", " ", str(name or "")).strip().replace("|", "／")
    if not text:
        return "名称未知"
    return text if len(text) <= 40 else text[:39] + "…"


def _purge(plugin, appids) -> None:
    purge = getattr(plugin, "purge_achievement_runtime_state", None)
    if callable(purge):
        purge(appids)


def _render_list(monitor, page: int) -> str:
    entries = monitor.list_entries()
    if not entries:
        return "成就黑名单为空。\n" + _USAGE + "\n" + _HINT

    total = len(entries)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, pages))
    start = (page - 1) * PAGE_SIZE

    lines = [f"成就黑名单（共 {total} 条，第 {page}/{pages} 页）"]
    for index in range(start, min(start + PAGE_SIZE, total)):
        appid, meta = entries[index]
        lines.append(
            f"{index + 1}. {appid} | {_safe_name(meta.get('game_name'))} | "
            f"{_format_time(meta.get('added_at'))} | {_reason_label(meta.get('reason'))}"
        )
    if pages > 1:
        next_page = page + 1 if page < pages else 1
        lines.append(f"翻页：/steam achievement_blacklist list {next_page}")
    lines.append(_HINT)
    return "\n".join(lines)


async def achievement_blacklist(plugin, event, action: str = "", value: str = ""):
    '''查看/删除成就查询黑名单；list [页码] / del <AppID...> / del all。'''
    monitor = getattr(plugin, "achievement_monitor", None)
    if monitor is None or not hasattr(monitor, "list_entries"):
        yield event.plain_result("成就黑名单存储不可用。")
        return

    raw = _args_text(event, action, value)
    tokens = [token for token in re.split(r"[,，\s]+", raw) if token]
    verb = tokens[0].lower() if tokens else ""

    # 列表：空 / list / ls / show（默认出图，加 text 走纯文本）
    if not tokens or verb in ("list", "ls", "show"):
        rest = tokens[1:]
        text_mode = any(token.lower() == "text" for token in rest)
        page = 1
        for token in rest:
            if token.lower() == "text":
                continue
            if not token.isdigit():
                yield event.plain_result("页码应为正整数。\n" + _USAGE)
                return
            page = int(token)

        if text_mode:
            yield event.plain_result(_render_list(monitor, page))
            return

        entries = monitor.list_entries()
        if not entries:
            yield event.plain_result("成就黑名单为空。\n" + _HINT)
            return
        try:
            img_bytes = render_achievement_blacklist_image(
                entries,
                title_suffix="",
                font_path=resolve_font_path("NotoSansHans-Regular.otf"),
            )
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                tmp.write(img_bytes)
                tmp_path = tmp.name
            yield event.image_result(tmp_path)
        except Exception as exc:
            logger.error(f"[成就黑名单] 图片渲染失败: {exc}")
            yield event.plain_result(
                "图片渲染失败，已退回文本输出：\n" + _render_list(monitor, page)
            )
        return

    # 补全游戏名：names / refresh / 补名
    if verb in ("names", "name", "refresh", "补名"):
        store = getattr(monitor, "blacklist_store", None)
        if store is None or not hasattr(store, "missing_name_appids"):
            yield event.plain_result("当前版本不支持补全游戏名。")
            return

        missing = store.missing_name_appids()
        if not missing:
            yield event.plain_result(
                f"成就黑名单共 {len(monitor.list_entries())} 条，全部已有游戏名，无需补全。"
            )
            return

        fetcher = getattr(plugin, "get_game_names_batch", None)
        if not callable(fetcher):
            yield event.plain_result("当前运行环境不支持批量查询游戏名。")
            return

        try:
            names = await fetcher(missing)
        except Exception as exc:
            logger.error(f"[成就黑名单] 批量查询游戏名失败: {exc}")
            yield event.plain_result("查询 Steam 商店失败，请稍后重试。")
            return

        filled = store.update_names(names or {})
        remaining = len(store.missing_name_appids())
        lines = [f"已补全 {filled} 条游戏名（本次尝试 {len(missing)} 条）。"]
        if remaining:
            lines.append(
                f"仍有 {remaining} 条未查到，可能是已下架、AppID 无效或商店接口暂时不可用，"
                "可稍后再执行一次。"
            )
        else:
            lines.append("所有条目均已有游戏名。")
        lines.append("可发送 /steam achievement_blacklist 查看最新表格。")
        yield event.plain_result("\n".join(lines))
        return

    # 删除：del / delete / rm / remove
    if verb in ("del", "delete", "rm", "remove"):
        targets = tokens[1:]
        if not targets:
            yield event.plain_result(
                "用法：/steam achievement_blacklist del 730,570 ｜ "
                "/steam achievement_blacklist del all"
            )
            return

        if any(token.lower() == "all" for token in targets):
            if len(targets) > 1:
                yield event.plain_result("del all 不能与其他 AppID 同时使用，已取消本次操作。")
                return
            appids = [appid for appid, _ in monitor.list_entries()]
            removed_count = monitor.clear_blacklist()
            _purge(plugin, appids)
            if removed_count == 0:
                yield event.plain_result("成就黑名单已经为空。")
                return
            yield event.plain_result(
                f"已清空成就黑名单，共移除 {removed_count} 条。\n"
                "相关游戏会在下一次成就查询（通常为下次开始游玩）时重新尝试。"
            )
            return

        appids, invalid = parse_appids(" ".join(targets))
        if invalid:
            steamids = [t for t in invalid if t.isascii() and t.isdigit() and len(t) == 17]
            others = [t for t in invalid if t not in steamids]
            lines = []
            if others:
                lines.append(f"无法识别为 AppID：{', '.join(others)}")
                lines.append("AppID 应为 1–10 位纯数字（如 730）。")
            if steamids:
                lines.append(f"{', '.join(steamids)} 是 SteamID64，不是游戏 AppID。")
                lines.append("请填写商店链接中的 AppID，如 store.steampowered.com/app/730 → 730。")
            yield event.plain_result("\n".join(lines))
            return
        if not appids:
            yield event.plain_result("没有可处理的 AppID。")
            return

        removed, missing = monitor.remove_appids(appids)
        remaining = len(monitor.list_entries())
        if removed:
            _purge(plugin, removed)
        if not removed:
            yield event.plain_result(
                f"以下 AppID 不在成就黑名单中：{', '.join(missing)}\n"
                f"当前共 {remaining} 条。\n" + _HINT
            )
            return

        lines = [f"已从成就黑名单删除：{', '.join(removed)}（剩余 {remaining} 条）"]
        if missing:
            lines.append(f"不在名单中：{', '.join(missing)}")
        lines.append("已同步清理失败计数与成就详情缓存；相关游戏会在下次成就查询时重新尝试。")
        lines.append(_HINT)
        yield event.plain_result("\n".join(lines))
        return

    yield event.plain_result("未知子命令。\n" + _USAGE)
