"""游戏库快照存储：owned_games.json 的加载、校验与「一轮一次」原子落盘。

设计目标（对照社区 fork 的改进）：
- 所有玩家共用一个文件、每轮只写一次，消除逐玩家全量重写的 O(N²) 写放大；
- 只持久化 appid 集合与必要元数据，不存游戏名/封面（新增项懒加载）；
- 写入用 temp + os.replace 原子替换，失败不残留临时文件。
"""
import json
import os
import tempfile

from ...shared.logging import logger

FILE_NAME = "owned_games.json"
SCHEMA_VERSION = 1


class OwnedGamesStore:
    """owned_games.json 的读写边界；线程/协程安全由调用方（服务层）保证串行。"""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, FILE_NAME)
        self.players = self._load()

    # ---------- 加载与校验 ----------

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as stream:
                data = json.load(stream)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logger.warning(f"[游戏库] 快照加载失败，将重新建立基线: {exc}")
            return {}
        if not isinstance(data, dict):
            logger.warning("[游戏库] 快照根节点不是对象，将重新建立基线")
            return {}
        players = data.get("players")
        if not isinstance(players, dict):
            return {}
        cleaned = {}
        for sid, state in players.items():
            normalized = self._normalize_state(state)
            if normalized is not None:
                cleaned[str(sid)] = normalized
        return cleaned

    @staticmethod
    def _normalize_state(state):
        """校验并规范单玩家状态；非法返回 None（该玩家按无基线处理）。"""
        if not isinstance(state, dict):
            return None
        ids = state.get("ids")
        if not isinstance(ids, list):
            return None
        try:
            ids = sorted({int(appid) for appid in ids})
        except (TypeError, ValueError):
            return None
        pending = []
        for item in state.get("pending") or []:
            if not isinstance(item, dict):
                continue
            # 按 kind 分派：老数据没有 kind 字段 → 视为 "owned"，走原逻辑且不补写 kind，
            # 保证升级后落盘字节与升级前一致；"owned_batch" 是简洁合并播报的分页批。
            if str(item.get("kind") or "owned").strip() == "owned_batch":
                entry = OwnedGamesStore._normalize_batch_entry(item)
                if entry is not None:
                    pending.append(entry)
                continue
            if item.get("appid") is None:
                continue
            try:
                entry = {
                    "appid": str(item["appid"]),
                    "name": str(item.get("name") or f"AppID {item['appid']}"),
                    "added_at": int(item.get("added_at") or 0),
                    "tries": int(item.get("tries") or 0),
                    "render": bool(item.get("render", True)),
                    "groups": [str(g) for g in (item.get("groups") or [])],
                    "deliveries": [],
                }
            except (TypeError, ValueError):
                continue
            for delivery in item.get("deliveries") or []:
                if isinstance(delivery, dict) and isinstance(delivery.get("noti"), dict):
                    entry["deliveries"].append({
                        "group_id": str(delivery.get("group_id") or ""),
                        "noti": delivery["noti"],
                    })
            if isinstance(item.get("card_data"), dict):
                entry["card_data"] = item["card_data"]
            pending.append(entry)
        grace = {}
        for appid, stamp in (state.get("grace") or {}).items():
            try:
                grace[str(appid)] = int(stamp)
            except (TypeError, ValueError):
                continue
        try:
            baseline_at = int(state.get("baseline_at") or 0)
            last_ok_at = int(state.get("last_ok_at") or 0)
            fail_count = int(state.get("fail_count") or 0)
        except (TypeError, ValueError):
            baseline_at = last_ok_at = fail_count = 0
        recent = []
        for entry in state.get("recent") or []:
            if not isinstance(entry, dict) or entry.get("appid") is None:
                continue
            try:
                recent.append({
                    "appid": str(entry["appid"]),
                    "name": str(entry.get("name") or f"AppID {entry['appid']}"),
                    "added_at": int(entry.get("added_at") or 0),
                })
            except (TypeError, ValueError):
                continue
        recent = recent[-20:]
        return {
            "ids": ids,
            "baseline_at": baseline_at,
            "last_ok_at": last_ok_at,
            "fail_count": fail_count,
            "grace": grace,
            "pending": pending,
            "recent": recent,
        }

    @staticmethod
    def _normalize_batch_entry(item):
        """校验并重建一条 owned_batch 分页记录；非法返回 None（只丢该页，不影响其它 pending）。"""
        items = item.get("items")
        if not isinstance(items, list) or not items:
            return None
        normalized_items = []
        for raw in items[:50]:  # 上限 50 项，防御脏数据
            if not isinstance(raw, dict) or raw.get("appid") is None:
                continue
            appid = raw["appid"]
            normalized_items.append({
                "appid": str(appid),
                "name": str(raw.get("name") or f"AppID {appid}"),
            })
        if not normalized_items:
            return None
        try:
            entry = {
                "kind": "owned_batch",
                "added_at": int(item.get("added_at") or 0),
                "tries": int(item.get("tries") or 0),
                "render": bool(item.get("render", True)),
                "page": max(1, int(item.get("page") or 1)),
                "pages": max(1, int(item.get("pages") or 1)),
                "added_count": max(0, int(item.get("added_count") or 0)),
                "shown_count": max(0, int(item.get("shown_count") or 0)),
                "page_size": max(0, int(item.get("page_size") or 0)),
                "groups": [str(g) for g in (item.get("groups") or [])],
                "deliveries": [],
                "items": normalized_items,
            }
        except (TypeError, ValueError):
            return None
        for delivery in item.get("deliveries") or []:
            if isinstance(delivery, dict) and isinstance(delivery.get("noti"), dict):
                entry["deliveries"].append({
                    "group_id": str(delivery.get("group_id") or ""),
                    "noti": delivery["noti"],
                })
        return entry

    @staticmethod
    def new_state():
        """新建玩家状态（无基线）。"""
        return {
            "ids": [],
            "baseline_at": 0,
            "last_ok_at": 0,
            "fail_count": 0,
            "grace": {},
            "pending": [],
            "recent": [],
        }

    # ---------- 保存 ----------

    def flush(self):
        """整文件原子写；由服务在每轮结束时调用一次。"""
        os.makedirs(self.data_dir, exist_ok=True)
        payload = {"version": SCHEMA_VERSION, "players": self.players}
        path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.data_dir,
                prefix="owned_games_", suffix=".tmp", delete=False,
            ) as stream:
                path = stream.name
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            os.replace(path, self.path)
            path = None
        finally:
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass

    # ---------- 清理 ----------

    def forget(self, sid) -> bool:
        """移除单个玩家的快照（玩家/绑定被删除时调用）。"""
        return self.players.pop(str(sid), None) is not None

    def clear(self) -> None:
        """清空全部快照（清空名单时调用）。"""
        self.players.clear()

    def prune(self, active_sids) -> list:
        """删除不在活动名单中的玩家快照，返回被清理的 sid 列表。"""
        active = {str(sid) for sid in (active_sids or [])}
        stale = [sid for sid in self.players if sid not in active]
        for sid in stale:
            self.players.pop(sid, None)
        return stale
