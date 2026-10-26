"""静默时段（免打扰）判定。

配置项 ``notify_quiet_hours`` 形如「起始小时,结束小时」，只接受整点：

- ``0,8``  → 00:00–07:59 静默
- ``23,8`` → 23:00–次日 07:59 静默
- ``0,0`` / 两值相同 / 空值 → 关闭（不静默）
- 非整数、越界（非 0–23）、段数不为 2 → fail-open，按关闭处理

设计约束：静默只影响「播报」，不影响轮询与状态/统计记录；命中时通知直接丢弃，
不排队到静默结束，避免早上一次性补推。时间基准为服务器本地时间。
"""

from __future__ import annotations

from datetime import datetime

DEFAULT_QUIET_HOURS = "0,0"


def parse_quiet_hours(raw):
    """把配置解析成 ``(start, end)``；关闭或非法时返回 ``None``。"""
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        parts = list(raw)
    else:
        text = str(raw).strip()
        if not text:
            return None
        # 容忍中文逗号、顿号、分号与空格，避免手输格式差异被误判为静默
        for sep in ("，", "、", ";", "；", " "):
            text = text.replace(sep, ",")
        parts = [part for part in text.split(",") if part.strip()]
    if len(parts) != 2:
        return None
    try:
        start = int(str(parts[0]).strip())
        end = int(str(parts[1]).strip())
    except (TypeError, ValueError):
        return None
    if not (0 <= start <= 23 and 0 <= end <= 23):
        return None
    if start == end:
        return None
    return start, end


def hour_in_window(start, end, hour):
    """``hour`` 是否落在 ``[start, end)``；支持跨午夜，起止相同时恒为 ``False``。"""
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def in_quiet_hours(config, now=None):
    """当前是否处于静默时段；配置缺失、类型异常或非法一律 ``False``（fail-open）。"""
    if not hasattr(config, "get"):
        config = {}
    window = parse_quiet_hours(config.get("notify_quiet_hours", DEFAULT_QUIET_HOURS))
    if window is None:
        return False
    current = now or datetime.now()
    return hour_in_window(window[0], window[1], current.hour)
