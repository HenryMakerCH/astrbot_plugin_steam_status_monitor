from __future__ import annotations

import re

FILTER_MODE_ALL = "全部游戏"
FILTER_MODE_WHITELIST = "白名单"
FILTER_MODE_BLACKLIST = "黑名单"

APPID_MAX = 4294967295  # Steam AppID 为 uint32
_TOKEN_SEP = re.compile(r"[,，\s]+")


def _strip_leading_zeros(token) -> str:
    text = str(token or "").strip()
    if text.isascii() and text.isdigit():
        return text.lstrip("0") or "0"
    return text


def _read_ids(raw) -> list[str]:
    """宽松读取：只做分隔符拆分、去重、去前导零，不丢弃历史遗留 token。"""
    ids, seen = [], set()
    for token in _TOKEN_SEP.split(str(raw or "")):
        token = token.strip()
        if not token:
            continue
        key = _strip_leading_zeros(token)
        if key not in seen:
            seen.add(key)
            ids.append(key)
    return ids


def normalize_appid(token) -> str | None:
    """单个 token -> 规范 AppID；非法返回 None。"""
    text = str(token or "").strip()
    if not text or not text.isascii() or not text.isdigit():
        return None
    normalized = text.lstrip("0")
    if not normalized:
        return None
    if len(normalized) > 10 or int(normalized) > APPID_MAX:
        return None
    return normalized


def parse_appids(raw) -> tuple[list[str], list[str]]:
    """按逗号/全角逗号/空白拆分，返回 (合法去重列表, 非法原始 token 列表)。"""
    valid, invalid, seen = [], [], set()
    for token in _TOKEN_SEP.split(str(raw or "").strip()):
        token = token.strip()
        if not token:
            continue
        appid = normalize_appid(token)
        if appid is None:
            if token not in invalid:
                invalid.append(token)
            continue
        if appid not in seen:
            seen.add(appid)
            valid.append(appid)
    return valid, invalid


def format_appids(appids) -> str:
    return ",".join(str(x).strip() for x in (appids or []) if str(x).strip())


def add_appids(current, appids) -> tuple[str, list[str]]:
    """返回 (新列表字符串, 实际新增项)。已存在项不会重复追加。"""
    merged = _read_ids(current)
    added = []
    for appid in appids or []:
        key = _strip_leading_zeros(appid)
        if key and key not in merged:
            merged.append(key)
            added.append(key)
    return format_appids(merged), added


def remove_appids(current, appids) -> tuple[str, list[str], list[str]]:
    """返回 (新列表字符串, 实际删除项, 不在名单中的请求项)。"""
    wanted = []
    for appid in appids or []:
        key = _strip_leading_zeros(appid)
        if key and key not in wanted:
            wanted.append(key)
    current_ids = _read_ids(current)
    removed = [item for item in current_ids if item in wanted]
    kept = [item for item in current_ids if item not in wanted]
    missing = [item for item in wanted if item not in removed]
    return format_appids(kept), removed, missing


def should_skip_game(config, gameid) -> bool:
    """按黑白名单判断是否跳过该游戏的监控/播报。"""
    if not gameid:
        return False
    mode = (config or {}).get("game_filter_mode", FILTER_MODE_ALL)
    if mode == FILTER_MODE_ALL:
        return False
    ids_str = (config or {}).get("game_filter_ids", "") or ""
    if not str(ids_str).strip():
        return False
    filter_ids = _read_ids(ids_str)
    target = _strip_leading_zeros(gameid)
    if mode == FILTER_MODE_WHITELIST:
        return target not in filter_ids
    if mode == FILTER_MODE_BLACKLIST:
        return target in filter_ids
    return False
