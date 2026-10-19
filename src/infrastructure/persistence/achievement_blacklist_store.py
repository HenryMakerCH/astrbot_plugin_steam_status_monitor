"""成就查询黑名单存储（v2）。

v1: ``["730", 570, ...]``（无元数据）
v2::

    {"version": 2, "updated_at": 1760000000,
     "entries": {"730": {"game_name": "Counter-Strike 2",
                         "added_at": 1760000000,
                         "reason": "no_stats", "source": "auto"}}}

约定：
- appid 统一为去前导零的 1–10 位数字字符串；
- added_at=0 表示未知，展示层显示「时间未知」，不伪造；
- reason: no_stats / fetch_failed_10x / manual / legacy；
- source: auto / manual / legacy。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time

from ...domain.monitoring.game_filter import normalize_appid
from ...shared.logging import logger

FILE_NAME = "achievement_blacklist.json"
SCHEMA_VERSION = 2

REASON_NO_STATS = "no_stats"
REASON_FETCH_FAILED = "fetch_failed_10x"
REASON_MANUAL = "manual"
REASON_LEGACY = "legacy"

SOURCE_AUTO = "auto"
SOURCE_MANUAL = "manual"
SOURCE_LEGACY = "legacy"

_TMP_PREFIX = "achievement_blacklist_"


class AchievementBlacklistStore:
    """黑名单文件读写边界；事件循环内串行调用，写入为原子替换。"""

    def __init__(self, data_dir: str, name_resolver=None):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, FILE_NAME)
        self.entries: dict[str, dict] = {}
        self._resolver = (
            name_resolver if name_resolver is not None else self._build_offline_resolver()
        )
        self._load()

    # ---------- 加载 / 迁移 ----------

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as stream:
                data = json.load(stream)
        except (OSError, ValueError) as exc:
            logger.warning(f"[成就黑名单] 读取失败，原文件已隔离: {exc}")
            self._quarantine()
            return

        version, raw = self._detect(data)
        if raw is None:
            logger.warning("[成就黑名单] 结构无法识别，原文件已隔离")
            self._quarantine()
            return

        legacy = version < SCHEMA_VERSION
        self.entries = self._normalize_entries(raw, legacy=legacy)
        filled = self.backfill_names()
        if legacy:
            self._backup_v1()
            self.save()
            logger.info(
                f"[成就黑名单] 已迁移 v1 -> v2: {len(self.entries)} 条，离线回填名称 {filled} 条"
            )
        elif filled:
            self.save()

    @staticmethod
    def _detect(data):
        if isinstance(data, dict) and isinstance(data.get("entries"), dict):
            try:
                version = int(data.get("version") or SCHEMA_VERSION)
            except (TypeError, ValueError):
                version = SCHEMA_VERSION
            return version, data["entries"]
        if isinstance(data, list):
            return 1, data
        if isinstance(data, dict):
            return 1, data
        return 0, None

    def _normalize_entries(self, raw, legacy: bool):
        entries: dict[str, dict] = {}
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    key = item.get("appid", item.get("gameid"))
                    meta = item
                else:
                    key, meta = item, None
                self._put(entries, key, meta, legacy)
        else:
            for key, value in raw.items():
                if isinstance(value, dict) and value.get("appid") is not None:
                    key = value.get("appid")
                self._put(entries, key, value, legacy)
        return entries

    @staticmethod
    def _put(entries, key, value, legacy: bool):
        appid = normalize_appid(key)
        if appid is None:
            return
        entries[appid] = AchievementBlacklistStore._normalize_meta(value, legacy)

    @staticmethod
    def _normalize_meta(value, legacy: bool):
        if legacy:
            name = ""
            if isinstance(value, dict):
                name = str(value.get("game_name") or value.get("name") or "").strip()
            elif isinstance(value, str):
                name = value.strip()
            # v1 没有可信时间：统一置 0，不伪造
            return {
                "game_name": name,
                "added_at": 0,
                "reason": REASON_LEGACY,
                "source": SOURCE_LEGACY,
            }

        if not isinstance(value, dict):
            return {
                "game_name": "",
                "added_at": 0,
                "reason": REASON_LEGACY,
                "source": SOURCE_LEGACY,
            }
        try:
            added_at = max(0, int(value.get("added_at") or 0))
        except (TypeError, ValueError):
            added_at = 0
        reason = str(value.get("reason") or REASON_MANUAL).strip() or REASON_MANUAL
        source = str(value.get("source") or SOURCE_AUTO).strip() or SOURCE_AUTO
        return {
            "game_name": str(value.get("game_name") or "").strip(),
            "added_at": added_at,
            "reason": reason,
            "source": source,
        }

    # ---------- 离线名称回填 ----------

    def _build_offline_resolver(self):
        names: dict[str, str] = {}
        self._merge_play_records(names)
        self._merge_session_records(names)
        return names

    def _merge_play_records(self, names):
        path = os.path.join(self.data_dir, "play_records.json")
        try:
            with open(path, "r", encoding="utf-8") as stream:
                data = json.load(stream)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        # 按日期旧→新遍历，后写的名称覆盖先写的
        for date in sorted(data):
            players = data.get(date)
            if not isinstance(players, dict):
                continue
            for games in players.values():
                if not isinstance(games, dict):
                    continue
                for appid, info in games.items():
                    if not isinstance(info, dict):
                        continue
                    name = str(info.get("name") or "").strip()
                    key = normalize_appid(appid)
                    if key and name:
                        names[key] = name

    def _merge_session_records(self, names):
        path = os.path.join(self.data_dir, "session_records.json")
        try:
            with open(path, "r", encoding="utf-8") as stream:
                data = json.load(stream)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        for sessions in data.values():
            if not isinstance(sessions, list):
                continue
            valid = [item for item in sessions if isinstance(item, dict)]
            valid.sort(key=lambda item: self._as_int(item.get("start_time")))
            for item in valid:
                key = normalize_appid(item.get("gameid"))
                name = str(item.get("game_name") or "").strip()
                if key and name:
                    names[key] = name

    @staticmethod
    def _as_int(value) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def backfill_names(self, resolver=None) -> int:
        names = resolver if resolver is not None else self._resolver
        if not hasattr(names, "get"):
            return 0
        filled = 0
        for appid, meta in self.entries.items():
            if meta.get("game_name"):
                continue
            name = str(names.get(appid) or "").strip()
            if name:
                meta["game_name"] = name
                filled += 1
        return filled

    def name_of(self, appid) -> str:
        key = normalize_appid(appid)
        if key is None:
            return ""
        meta = self.entries.get(key) or {}
        if meta.get("game_name"):
            return meta["game_name"]
        return str(self._resolver.get(key) or "")

    def missing_name_appids(self):
        """返回当前没有游戏名的 AppID 列表（按加入时间倒序）。"""
        return [appid for appid, meta in self.items() if not meta.get("game_name")]

    def update_names(self, mapping) -> int:
        """批量写入游戏名，只填充空缺（已有名称不覆盖）。返回实际写入条数。"""
        if not mapping or not hasattr(mapping, "items"):
            return 0
        filled = 0
        for raw_appid, raw_name in mapping.items():
            key = normalize_appid(raw_appid)
            name = str(raw_name or "").strip()
            if not key or not name:
                continue
            meta = self.entries.get(key)
            if meta is None or meta.get("game_name"):
                continue
            meta["game_name"] = name
            filled += 1
        if filled:
            self.save()
        return filled

    # ---------- 原子写 ----------

    def save(self) -> bool:
        os.makedirs(self.data_dir, exist_ok=True)
        payload = {
            "version": SCHEMA_VERSION,
            "updated_at": int(time.time()),
            "entries": self.entries,
        }
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.data_dir,
                prefix=_TMP_PREFIX,
                suffix=".tmp",
                delete=False,
            ) as stream:
                temp_path = stream.name
                json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            os.replace(temp_path, self.path)
            temp_path = None
            return True
        except OSError as exc:
            logger.warning(f"[成就黑名单] 保存失败: {exc}")
            return False
        finally:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    # ---------- 增删查 ----------

    def contains(self, appid) -> bool:
        key = normalize_appid(appid)
        return key is not None and key in self.entries

    def add(
        self,
        appid,
        reason=REASON_MANUAL,
        game_name="",
        source=SOURCE_MANUAL,
        added_at=None,
    ) -> bool:
        key = normalize_appid(appid)
        if key is None:
            return False
        name = str(game_name or "").strip()
        existing = self.entries.get(key)
        if existing is not None:
            changed = False
            if name and not existing.get("game_name"):
                existing["game_name"] = name
                changed = True
            # 手动加入优先：改为 manual 以便历史校验跳过，但保留首次时间
            if source == SOURCE_MANUAL and existing.get("source") != SOURCE_MANUAL:
                existing["source"] = SOURCE_MANUAL
                existing["reason"] = REASON_MANUAL
                changed = True
            if changed:
                self.save()
            return False
        self.entries[key] = {
            "game_name": name,
            "added_at": self._as_int(added_at) if added_at is not None else int(time.time()),
            "reason": str(reason or REASON_MANUAL),
            "source": str(source or SOURCE_MANUAL),
        }
        self.save()
        return True

    def remove(self, appids):
        requested = []
        for raw in appids or []:
            key = normalize_appid(raw)
            if key and key not in requested:
                requested.append(key)
        removed, missing = [], []
        for key in requested:
            if self.entries.pop(key, None) is not None:
                removed.append(key)
            else:
                missing.append(key)
        if removed:
            self.save()
        return removed, missing

    def clear(self) -> int:
        count = len(self.entries)
        if count:
            self.entries.clear()
            self.save()
        return count

    def items(self):
        """按加入时间倒序；未知时间（0）排最后，再按 AppID 升序。"""

        def sort_key(item):
            appid, meta = item
            added = self._as_int(meta.get("added_at"))
            return (0 if added else 1, -added, int(appid))

        return sorted(self.entries.items(), key=sort_key)

    def appid_set(self):
        return set(self.entries)

    # ---------- 备份 ----------

    def _backup_v1(self):
        target = f"{self.path}.bak-v1"
        if os.path.exists(target):
            return
        try:
            shutil.copy2(self.path, target)
            logger.info(f"[成就黑名单] 已备份 v1 原文件到 {target}")
        except OSError as exc:
            logger.warning(f"[成就黑名单] v1 备份失败: {exc}")

    def _quarantine(self):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = f"{self.path}.corrupt-{stamp}"
        if os.path.exists(target):
            target = f"{target}-{int(time.time() * 1000) % 1000}"
        try:
            os.replace(self.path, target)
            logger.warning(f"[成就黑名单] 原文件已备份到 {target}")
        except OSError as exc:
            logger.warning(f"[成就黑名单] 损坏文件备份失败: {exc}")
