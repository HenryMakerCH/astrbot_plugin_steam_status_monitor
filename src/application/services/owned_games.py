"""Steam 游戏库同步、新入库差分检测与播报编排。

独立于主状态轮询的专用后台路径（不阻塞 bot 正常运行）：
- 独立 asyncio task，默认 30 分钟一轮（可配），与主循环零共享调度状态；
- 每轮对被监控玩家去重后各拉取一次 GetOwnedGames（仅取 appid），差分出新增入库；
- 仅新增项懒加载游戏名/商店详情/评价（复用价格查询管线与缓存）；
- 复用既有通知管线（_send_merged_notification）推送，失败按会话粒度持久化补发。
"""
import asyncio
import time
from datetime import datetime, timedelta

from ...domain.monitoring.game_filter import should_skip_game
from ...infrastructure.clients.itad import ITADGame
from ...infrastructure.clients.steam import (
    OwnedGamesInvisible,
    OwnedGamesRateLimited,
    SteamClientError,
)
from ...infrastructure.persistence.owned_games_store import OwnedGamesStore
from ...shared.logging import logger

PENDING_MAX_TRIES = 5
MIN_INTERVAL_SECONDS = 600
DEFAULT_INTERVAL_SECONDS = 1800
DEFAULT_DAILY_BUDGET = 3000
DEFAULT_MAX_PER_NOTIFY = 5
DEFAULT_GRACE_DAYS = 7
RECENT_HISTORY_LIMIT = 20
DAY_ROLLOVER_HOUR = 4


class OwnedGamesService:
    """游戏库新入库检测服务：独立循环、差分、补发与查询支撑。"""

    def __init__(self, plugin):
        self.plugin = plugin
        self.store = OwnedGamesStore(plugin.data_dir)
        self._locks = {}
        self._semaphore = asyncio.Semaphore(2)
        self._enrich_lock = asyncio.Lock()
        self._fail_until = {}
        self._rate_limit_streak = 0
        self._backoff_until = 0.0
        self._day_key = None
        self._day_calls = 0
        self._dirty = False

    # ================= 对外数据访问（命令 / 管理钩子） =================

    @property
    def players(self):
        return self.store.players

    def forget(self, sid) -> bool:
        return self.store.forget(sid)

    def forget_many(self, sids) -> bool:
        changed = False
        for sid in sids or []:
            changed = self.store.forget(sid) or changed
        return changed

    def clear(self) -> None:
        self.store.clear()

    def forget_and_flush(self, sid) -> bool:
        """移除单个玩家快照并立即落盘（名单删除钩子用）。"""
        changed = self.store.forget(sid)
        if changed:
            self._flush_now()
        return changed

    def prune_orphans(self) -> list:
        """删除不在监控名单中的玩家快照并落盘（群删除钩子用）。"""
        stale = self.store.prune(self._known_sids())
        if stale:
            self._dirty = True
            self._flush_now()
        return stale

    def clear_and_flush(self) -> None:
        """清空全部快照并立即落盘（清空名单/重置钩子用）。"""
        self.store.clear()
        self._flush_now()

    def _flush_now(self) -> None:
        try:
            self.store.flush()
            self._dirty = False
        except Exception:
            logger.exception("[游戏库] 快照落盘失败")

    # ================= 配置读取 =================

    def _enabled(self) -> bool:
        return bool((self.plugin.config or {}).get("enable_owned_games_notify", False))

    def _interval(self) -> int:
        try:
            seconds = int(self.plugin.config.get("owned_games_poll_interval", DEFAULT_INTERVAL_SECONDS))
        except (TypeError, ValueError):
            seconds = DEFAULT_INTERVAL_SECONDS
        return max(MIN_INTERVAL_SECONDS, seconds or DEFAULT_INTERVAL_SECONDS)

    def _max_per_notify(self) -> int:
        try:
            value = int(self.plugin.config.get("owned_games_max_per_notify", DEFAULT_MAX_PER_NOTIFY))
        except (TypeError, ValueError):
            value = DEFAULT_MAX_PER_NOTIFY
        return min(20, max(1, value or DEFAULT_MAX_PER_NOTIFY))

    def _include_free(self) -> bool:
        return bool(self.plugin.config.get("owned_games_include_free", True))

    def _grace_seconds(self) -> int:
        try:
            days = int(self.plugin.config.get("owned_games_grace_days", DEFAULT_GRACE_DAYS))
        except (TypeError, ValueError):
            days = DEFAULT_GRACE_DAYS
        return max(0, days) * 86400

    def _ignore_appids(self) -> set:
        raw = str(self.plugin.config.get("owned_games_ignore_appids", "") or "")
        return {token.strip() for token in raw.split(",") if token.strip()}

    # ================= 后台循环 =================

    async def run_loop(self):
        """独立后台循环：60s 后建立首轮基线，随后按周期同步（默认 1800s）。"""
        await asyncio.sleep(60)
        try:
            await self.sync_all(baseline_only=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[游戏库] 首轮基线建立失败，将按周期重试")
        while True:
            try:
                if time.time() < self._backoff_until:
                    await asyncio.sleep(min(300.0, max(5.0, self._backoff_until - time.time())))
                    continue
                if not self._budget_available():
                    await asyncio.sleep(self._seconds_to_next_day())
                    continue
                await self.retry_pending()
                await self.sync_all()
            except asyncio.CancelledError:
                logger.info("[游戏库] 后台循环已取消")
                raise
            except Exception:
                logger.exception("[游戏库] 本轮异常，将在下轮继续")
            finally:
                if self._dirty:
                    try:
                        self.store.flush()
                        self._dirty = False
                    except Exception:
                        logger.exception("[游戏库] 快照落盘失败")
            await asyncio.sleep(self._interval())

    # ================= 同步与差分 =================

    def _routing(self):
        """{sid: [group_id, ...]}：仅统计已启动且启用监控的群（与主轮询口径一致）。"""
        plugin = self.plugin
        running = {str(gid) for gid in (getattr(plugin, "running_groups", set()) or set())}
        enabled = getattr(plugin, "group_monitor_enabled", {}) or {}
        routing = {}
        for group_id, ids in (getattr(plugin, "group_steam_ids", {}) or {}).items():
            gid = str(group_id)
            if gid not in running or not enabled.get(gid, True):
                continue
            for sid in ids or []:
                sid = str(sid)
                targets = routing.setdefault(sid, [])
                if gid not in targets:
                    targets.append(gid)
        return routing

    def _known_sids(self):
        sids = set()
        for ids in (getattr(self.plugin, "group_steam_ids", {}) or {}).values():
            for sid in ids or []:
                sids.add(str(sid))
        return sids

    async def sync_all(self, baseline_only=False):
        routing = self._routing()
        stale = self.store.prune(self._known_sids())
        if stale:
            self._dirty = True
            logger.info(f"[游戏库] 已清理 {len(stale)} 个不在监控名单中的玩家快照")
        sids = [sid for sid in routing if self._sid_ready(sid)]
        summary = {"players": len(sids), "added": 0, "baselines": 0, "failed": 0}
        if not sids:
            return summary

        async def one(sid):
            async with self._semaphore:
                return await self.sync_player(sid, routing[sid], baseline_only=baseline_only)

        results = await asyncio.gather(*(one(sid) for sid in sids), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                summary["failed"] += 1
                continue
            if not isinstance(result, dict):
                continue
            summary["added"] += int(result.get("added", 0))
            summary["baselines"] += int(bool(result.get("baseline")))
            summary["failed"] += int(bool(result.get("failed")))
        if summary["added"] or summary["failed"]:
            logger.info(
                f"[游戏库] 本轮同步：玩家 {summary['players']}，新入库 {summary['added']}，"
                f"新建基线 {summary['baselines']}，失败 {summary['failed']}"
            )
        return summary

    async def sync_player(self, sid, groups, baseline_only=False):
        sid = str(sid)
        lock = self._locks.setdefault(sid, asyncio.Lock())
        async with lock:
            return await self._sync_player(sid, groups, baseline_only)

    async def _sync_player(self, sid, groups, baseline_only):
        sid = str(sid)
        try:
            games = await self.plugin.fetch_owned_games(
                sid, include_played_free_games=self._include_free(),
            )
        except OwnedGamesRateLimited:
            self._on_rate_limited()
            self._mark_fail(sid)
            return {"baseline": False, "added": 0, "failed": True}
        except OwnedGamesInvisible as exc:
            self._mark_fail(sid)
            logger.info(f"[游戏库] {sid} 游戏详情不可见，本轮跳过且不影响已有基线: {exc}")
            return {"baseline": False, "added": 0, "failed": True}
        except SteamClientError as exc:
            self._mark_fail(sid)
            logger.warning(f"[游戏库] {sid} 读取失败，本轮跳过: {exc}")
            return {"baseline": False, "added": 0, "failed": True}

        self._count_call()
        self._rate_limit_streak = 0
        now = int(time.time())
        state = self.store.players.setdefault(sid, OwnedGamesStore.new_state())
        state["fail_count"] = 0
        state["last_ok_at"] = now
        self._fail_until.pop(sid, None)

        current_ids = sorted({
            int(game["appid"]) for game in games
            if isinstance(game, dict) and isinstance(game.get("appid"), int)
        })
        if not state.get("baseline_at"):
            state["ids"] = current_ids
            state["baseline_at"] = now
            self._dirty = True
            logger.info(f"[游戏库] {sid} 首轮基线已建立：{len(current_ids)} 款（不播报）")
            return {"baseline": True, "added": 0, "failed": False}

        previous = {int(appid) for appid in state.get("ids") or []}
        current = set(current_ids)
        added_ids = sorted(current - previous)
        removed_ids = previous - current

        grace = state.setdefault("grace", {})
        grace_seconds = self._grace_seconds()
        effective = self._filter_new_appids(added_ids, grace, now, grace_seconds)
        for appid in removed_ids:
            grace[str(appid)] = now
        self._prune_grace(grace, now, grace_seconds)

        state["ids"] = current_ids
        self._dirty = True
        if effective:
            self._record_recent(state, effective, now)
        if baseline_only or not effective:
            return {"baseline": False, "added": 0, "failed": False}
        announced = await self._announce(sid, groups, effective, state)
        return {"baseline": False, "added": announced, "failed": False}

    def _filter_new_appids(self, added_ids, grace, now, grace_seconds):
        """过滤抖动重现与黑白名单/忽略名单，返回真正需要播报的新增。"""
        effective = []
        ignored = self._ignore_appids()
        for appid in added_ids:
            key = str(appid)
            stamp = grace.get(key)
            if stamp is not None and (now - int(stamp)) <= grace_seconds:
                continue  # 抖动窗口内重现（如免费周末），不重复播报
            if key in ignored:
                continue
            if should_skip_game(self.plugin.config or {}, appid):
                continue
            effective.append(appid)
        return effective

    @staticmethod
    def _prune_grace(grace, now, grace_seconds):
        keep = max(grace_seconds * 2, 14 * 86400)
        for key in [k for k, v in grace.items() if now - int(v) > keep]:
            grace.pop(key, None)

    @staticmethod
    def _record_recent(state, appids, now):
        recent = state.setdefault("recent", [])
        for appid in appids:
            recent.append({"appid": str(appid), "name": f"AppID {appid}", "added_at": now})
        del recent[:-RECENT_HISTORY_LIMIT]

    # ================= 播报与补发 =================

    async def _announce(self, sid, groups, appids, state):
        """把新增项转为播报任务：懒加载名称与商店数据，立即投递，失败进持久化补发队列。"""
        if not self._enabled():
            return 0
        max_cards = self._max_per_notify()
        now = int(time.time())
        items = []
        for index, appid in enumerate(appids):
            render = index < max_cards
            card_data = await self._enrich_card(appid) if render else None
            name = (card_data or {}).get("name")
            if not name:
                try:
                    name, _english = await self.plugin.get_game_names(appid)
                except Exception as exc:
                    logger.warning(f"[游戏库] 获取游戏名失败（appid={appid}）: {exc}")
                    name = None
            name = str(name or f"AppID {appid}")
            items.append({
                "appid": str(appid),
                "name": name,
                "added_at": now,
                "tries": 0,
                "render": render,
                "groups": [str(group_id) for group_id in groups],
                "deliveries": [],
                "card_data": card_data if isinstance(card_data, dict) else None,
            })
            for entry in state.get("recent") or []:
                if entry.get("appid") == str(appid):
                    entry["name"] = name
        state.setdefault("pending", []).extend(items)
        self._dirty = True
        for item in items:
            try:
                await self._flush_item_once(sid, state, item)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(f"[游戏库] 首次播报异常（sid={sid}, appid={item.get('appid')}）")
        state["pending"] = [
            item for item in state["pending"]
            if item.get("groups") or item.get("deliveries")
        ]
        return len(items)

    async def _enrich_card(self, appid):
        """懒加载商店详情 + 双语言评价（复用价格查询管线与缓存）；失败降级返回 None。"""
        service = getattr(self.plugin, "price_query", None)
        if service is None:
            return None
        async with self._enrich_lock:
            try:
                game = ITADGame(id="", title="", appid=str(appid))
                card = await service.build_card(game, include_itad=False, include_reviews=True)
                return card.card_data or None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[游戏库] 商店详情/评价获取失败（appid={appid}），卡片将降级: {exc}")
                return None

    async def _flush_item_once(self, sid, state, item):
        """按 item['groups'] 执行首次投递；失败按会话粒度记入 item['deliveries']。"""
        groups = [str(group_id) for group_id in item.get("groups") or []]
        if not groups:
            return
        plugin = self.plugin
        owned_count = len(state.get("ids") or [])
        for group_id in groups:
            status = ((getattr(plugin, "group_last_states", {}) or {}).get(group_id) or {}).get(str(sid)) or {}
            noti = {
                "type": "owned",
                "sid": str(sid),
                "name": plugin._resolve_bind_name(sid, status.get("name") or str(sid)),
                "gameid": str(item.get("appid")),
                "game": item.get("name") or f"AppID {item.get('appid')}",
                "status": status,
                "avatar_url": status.get("avatarfull") or status.get("avatar"),
                "added_at": int(item.get("added_at") or 0),
                "owned_count": owned_count,
                "render": bool(item.get("render", True)),
                "card_data": item.get("card_data"),
            }
            failed = await self._send_to_group(group_id, noti)
            for failed_noti in failed:
                item.setdefault("deliveries", []).append({
                    "group_id": str(group_id),
                    "noti": failed_noti,
                })
        item["groups"] = []
        self._dirty = True

    async def _send_to_group(self, group_id, noti):
        """把单条通知交给既有通知管线；返回失败的会话级重试副本。"""
        try:
            failed = await self.plugin._send_merged_notification(str(group_id), [dict(noti)])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"[游戏库] 播报发送异常 (group_id={group_id})")
            return [dict(noti)]
        return [dict(item) for item in (failed or [])]

    async def retry_pending(self):
        """补发上一轮失败/未完成的播报（持久化队列，重启后照常补发）。"""
        if not self._enabled():
            return 0
        delivered = 0
        for sid, state in list(self.store.players.items()):
            pending = state.get("pending") or []
            if not pending:
                continue
            lock = self._locks.setdefault(sid, asyncio.Lock())
            async with lock:
                remaining = []
                for item in pending:
                    try:
                        still_pending = await self._retry_item(sid, state, item)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception(f"[游戏库] 补发播报异常 (sid={sid})")
                        still_pending = True
                    if not still_pending:
                        delivered += 1
                        continue
                    item["tries"] = int(item.get("tries") or 0) + 1
                    if item["tries"] > PENDING_MAX_TRIES:
                        logger.warning(
                            f"[游戏库] {sid} 的新入库播报（appid={item.get('appid')}）"
                            f"连续失败 {item['tries']} 次，放弃补发"
                        )
                        continue
                    remaining.append(item)
                state["pending"] = remaining
                self._dirty = True
        return delivered

    async def _retry_item(self, sid, state, item):
        if item.get("groups"):
            # 崩溃前尚未完成首次投递的孤儿任务：本轮补做首次投递，不立即二次发送
            await self._flush_item_once(sid, state, item)
            return bool(item.get("groups")) or bool(item.get("deliveries"))
        new_deliveries = []
        for delivery in item.get("deliveries") or []:
            failed = await self._send_to_group(delivery.get("group_id"), delivery.get("noti") or {})
            for failed_noti in failed:
                new_deliveries.append({
                    "group_id": str(delivery.get("group_id") or ""),
                    "noti": failed_noti,
                })
        item["deliveries"] = new_deliveries
        return bool(item.get("deliveries"))

    # ================= 手动同步与查询 =================

    async def sync_group_with_report(self, group_id, announce=False):
        """手动同步当前群的主监控玩家；默认只更新基线不播报（announce=True 立即播报）。"""
        gid = str(group_id)
        sids = list(dict.fromkeys(
            str(sid) for sid in (getattr(self.plugin, "group_steam_ids", {}).get(gid) or [])
        ))
        result = {"players": len(sids), "baselines": 0, "added": 0, "failed": 0}
        for sid in sids:
            try:
                outcome = await self.sync_player(sid, [gid], baseline_only=not announce)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(f"[游戏库] 手动同步异常 (sid={sid})")
                result["failed"] += 1
                continue
            if not isinstance(outcome, dict) or outcome.get("failed"):
                result["failed"] += 1
                continue
            result["baselines"] += int(bool(outcome.get("baseline")))
            result["added"] += int(outcome.get("added", 0))
        self.store.flush()
        self._dirty = False
        return result

    async def list_for_sender(self, sender_id, query="", offset=0, limit=20):
        """只允许查询消息发送者本人绑定；绝不接受外部传入的其他 QQ/SteamID。"""
        binding = (getattr(self.plugin, "_bind_data", {}) or {}).get(str(sender_id))
        sid = str(binding.get("sid")) if isinstance(binding, dict) and binding.get("sid") else None
        if not sid:
            return {"error": "当前用户尚未绑定 Steam 账号；请在群内使用 /steam addid SteamID @用户 绑定。"}
        state = self.store.players.get(sid)
        cached_total = len(state.get("ids") or []) if state else None
        synced_at = int(state.get("last_ok_at") or 0) if state else 0
        query_text = str(query or "").strip()
        if not query_text and cached_total is not None:
            return {
                "steam_id": sid,
                "total_owned": cached_total,
                "synced_at": synced_at,
                "recent": list(reversed((state.get("recent") or [])[-RECENT_HISTORY_LIMIT:])),
                "games": [],
                "note": "如需按名称检索具体游戏，请附带关键词。",
            }
        try:
            games = await self.plugin.fetch_owned_games(sid, include_appinfo=True)
        except SteamClientError as exc:
            if cached_total is None:
                return {"error": f"无法读取 Steam 游戏库：{exc}"}
            return {
                "steam_id": sid,
                "total_owned": cached_total,
                "synced_at": synced_at,
                "games": [],
                "error": "实时读取失败，仅返回缓存统计。",
            }
        self._count_call()
        needle = query_text.casefold()
        matched = [game for game in games if needle in str(game.get("name") or "").casefold()]
        try:
            start = max(0, int(offset))
            page_size = min(50, max(1, int(limit)))
        except (TypeError, ValueError):
            start, page_size = 0, 20
        page = matched[start:start + page_size]
        return {
            "steam_id": sid,
            "total_owned": len(games),
            "matched": len(matched),
            "offset": start,
            "next_offset": start + len(page) if start + len(page) < len(matched) else None,
            "games": [
                {"appid": game.get("appid"), "name": game.get("name") or f"AppID {game.get('appid')}"}
                for game in page
            ],
        }

    # ================= 退避 / 预算 =================

    def _mark_fail(self, sid):
        sid = str(sid)
        state = self.store.players.setdefault(sid, OwnedGamesStore.new_state())
        state["fail_count"] = int(state.get("fail_count") or 0) + 1
        cooldown = min(21600, 300 * (2 ** min(state["fail_count"] - 1, 6)))
        self._fail_until[sid] = time.time() + cooldown
        self._dirty = True

    def _sid_ready(self, sid) -> bool:
        return time.time() >= self._fail_until.get(str(sid), 0.0)

    def _on_rate_limited(self):
        self._rate_limit_streak += 1
        delay = min(3600, 60 * (2 ** min(self._rate_limit_streak, 6)))
        self._backoff_until = time.time() + delay
        logger.warning(f"[游戏库] Steam 接口限流（429），全局退避 {delay}s")

    def _daily_budget(self) -> int:
        try:
            value = int(self.plugin.config.get("owned_games_daily_budget", DEFAULT_DAILY_BUDGET))
        except (TypeError, ValueError):
            value = DEFAULT_DAILY_BUDGET
        return max(50, value or DEFAULT_DAILY_BUDGET)

    def _current_day_key(self):
        ranking = getattr(self.plugin, "ranking_service", None)
        if ranking is not None:
            try:
                return ranking.day_key(0)
            except Exception:
                pass
        now = datetime.now()
        if now.hour < DAY_ROLLOVER_HOUR:
            now = now - timedelta(days=1)
        return now.strftime("%Y-%m-%d")

    def _budget_available(self) -> bool:
        key = self._current_day_key()
        if key != self._day_key:
            self._day_key = key
            self._day_calls = 0
        return self._day_calls < self._daily_budget()

    def _count_call(self):
        self._day_calls += 1

    def _seconds_to_next_day(self) -> int:
        now = datetime.now()
        target = now.replace(hour=DAY_ROLLOVER_HOUR, minute=0, second=0, microsecond=0)
        if now >= target:
            target = target + timedelta(days=1)
        return max(60, int((target - now).total_seconds()) + 60)
