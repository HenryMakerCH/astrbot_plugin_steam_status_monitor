"""在线状态（presence）判定：与游戏会话状态机正交，只看 Steam 快照本身。

`personastate` 语义：0=离线，1+=在线（忙碌/离开等均算在线）。
Steam 隐身（invisible）用户在本 API 下表现为 personastate=0，无法与真离线区分，
唯一例外是隐身状态下仍在游戏——此时 gameid 非空，视为在线。
"""

from dataclasses import dataclass
from typing import Any, Mapping, Optional

# 下线推送的防抖确认窗口（秒）：窗口内重新上线则取消推送，窗口结束仍离线才播报。
# 与游戏会话的 NETWORK_FLUCTUATION_WINDOW=180 对齐（同属 Steam API 抖动防护）。
PRESENCE_OFFLINE_DEBOUNCE_SECONDS = 180
# 防抖窗口内该玩家的复查轮询间隔（秒）：离线玩家的自然轮询档位是 3-30 分钟，
# 赶不上窗口节奏，须钳制 next_poll_time 强制复查。
PRESENCE_RECHECK_INTERVAL_SECONDS = 60
# 结算要求“最后一次离线观测”足够新鲜：容忍一次 60 秒复查延迟，
# 同时阻止重启/请求失败时用旧快照误结算。
PRESENCE_SETTLE_FRESHNESS_SECONDS = 75
# 超过该时长仍没有新鲜离线观测：静默丢弃待定，避免僵尸项长期占用。
PRESENCE_PENDING_MAX_STALE_SECONDS = 900
# 已结算离线事件的多群基线滞后抑制窗口（覆盖最大 30 分钟轮询档位）。
PRESENCE_OFFLINE_SUPPRESS_SECONDS = 3600


@dataclass(frozen=True)
class PresenceEvent:
    """一次在线状态转换；kind 取 "online" 或 "offline"。"""

    kind: str


def is_online(snapshot: Mapping[str, Any]) -> bool:
    """快照当前是否在线：gameid 非空（含隐身挂机）或 personastate > 0。"""
    gameid = snapshot.get("gameid")
    if gameid not in (None, "", 0, "0"):
        return True
    try:
        return int(snapshot.get("personastate") or 0) > 0
    except (TypeError, ValueError):
        return False


def _has_presence(snapshot: Mapping[str, Any]) -> bool:
    """旧版本持久化基线可能缺 personastate 字段，缺字段视为状态未知。"""
    return "personastate" in snapshot or snapshot.get("gameid") not in (None, "", 0, "0")


def presence_transition(previous: Optional[Mapping[str, Any]], current: Mapping[str, Any]) -> Optional[PresenceEvent]:
    """对比上一轮与当前快照，返回在线状态转换事件；无需推送时返回 None。

    - previous 为 None（首次观测）或状态未知：只建基线，不产生事件。
    - 离线→在线返回 PresenceEvent("online")，在线→离线返回 PresenceEvent("offline")。
    """
    if previous is None or not _has_presence(previous) or not _has_presence(current):
        return None
    was_online = is_online(previous)
    currently_online = is_online(current)
    if was_online == currently_online:
        return None
    return PresenceEvent("online" if currently_online else "offline")
