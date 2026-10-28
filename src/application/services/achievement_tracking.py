import asyncio
import tempfile
import time

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image, Plain

from ...shared.fonts import resolve_font_path
from ...shared.logging import logger
from ...shared.utils.notify_session import is_sendable_group_session
from ...shared.utils.quiet_hours import in_quiet_hours
from ...infrastructure.persistence.achievement_blacklist_store import (
    REASON_FETCH_FAILED,
    SOURCE_AUTO,
)


class AchievementTrackingMixin:
    """成就轮询、结束补偿和成就通知的应用层编排。"""

    async def achievement_periodic_check(self, group_id, sid, gameid, player_name, game_name):
        key = (group_id, sid, gameid)
        try:
            while True:
                await asyncio.sleep(1200)
                if self._achievement_is_blacklisted(gameid):
                    logger.info(f"[成就定时对比] 游戏 {gameid} 已在黑名单，跳过轮询")
                    break
                achievements_a = self.achievement_snapshots.get(key)
                achievements_b = await self.achievement_monitor.get_player_achievements(
                    self.API_KEY, group_id, sid, gameid, game_name=game_name
                )
                today = time.strftime('%Y-%m-%d')
                fail_key = (gameid, today)
                if achievements_b is None:
                    cnt = self.achievement_fail_count.get(fail_key, 0) + 1
                    self.achievement_fail_count[fail_key] = cnt
                    if cnt >= 10:
                        self._blacklist_game(gameid, game_name)
                        break
                    continue
                if achievements_a is not None:
                    new_achievements = set(achievements_b) - set(achievements_a)
                    if new_achievements:
                        logger.info(f"[成就定时对比] {player_name} 在 {game_name} 解锁新成就：{', '.join(new_achievements)}")
                        delivered = await self.notify_new_achievements(group_id, sid, player_name, gameid, game_name, new_achievements)
                        if delivered:
                            self.achievement_snapshots[key] = list(achievements_b)
                    else:
                        logger.info(f"[成就定时对比] {player_name} 在 {game_name} 未发现新成就")
        except asyncio.CancelledError:
            logger.info(f"[成就定时对比] 任务已取消 group_id={group_id} sid={sid} gameid={gameid}")
        except Exception as e:
            logger.error(f"[成就定时对比] group_id={group_id} sid={sid} gameid={gameid} 异常: {e}")
        finally:
            task = getattr(self, "achievement_poll_tasks", {}).get(key)
            if task is asyncio.current_task():
                self.achievement_poll_tasks.pop(key, None)

    async def achievement_delayed_final_check(self, group_id, sid, gameid, player_name, game_name, achievements_a=None):
        key = (group_id, sid, gameid)
        await asyncio.sleep(300)
        if self._achievement_is_blacklisted(gameid):
            logger.info(f"[成就结束冗余对比] 游戏 {gameid} 已在黑名单，跳过轮询")
            return
        # 优先使用结束瞬间捕获的基准；未传（兼容旧调用）则读全局快照
        if achievements_a is None:
            achievements_a = self.achievement_snapshots.get(key)
        achievements_b = await self.achievement_monitor.get_player_achievements(
            self.API_KEY, group_id, sid, gameid, game_name=game_name
        )
        fail_key = (gameid, time.strftime('%Y-%m-%d'))
        if achievements_b is None:
            cnt = self.achievement_fail_count.get(fail_key, 0) + 1
            self.achievement_fail_count[fail_key] = cnt
            if cnt >= 10:
                self._blacklist_game(gameid, game_name)
            return
        delivered = True
        if achievements_a is not None:
            new_achievements = set(achievements_b) - set(achievements_a)
            if new_achievements:
                logger.info(f"[成就结束冗余对比] {player_name} 在 {game_name} 解锁新成就：{', '.join(new_achievements)}")
                delivered = await self.notify_new_achievements(
                    group_id, sid, player_name, gameid, game_name, new_achievements
                )
            else:
                logger.info(f"[成就结束冗余对比] {player_name} 在 {game_name} 未发现新成就")
        # 若该 key 已被新的成就轮询/会话占用（如重开同游戏或 A→B→A 切回），则跳过清理，避免误清新局数据。
        if key in getattr(self, "achievement_poll_tasks", {}):
            return
        if delivered:
            self.achievement_snapshots.pop(key, None)
            self.achievement_monitor.clear_game_achievements(group_id, sid, gameid)

    async def notify_new_achievements(self, group_id, steamid, player_name, gameid, game_name, new_achievements):
        if not new_achievements:
            return True
        if in_quiet_hours(getattr(self, "config", {}) or {}):
            # 静默时段：返回 True 表示「已消费」，调用方照常推进 achievement_snapshots，
            # 静默期间解锁的成就不会在早上补发。
            logger.info(
                "静默时段跳过成就播报 (group_id=%s, gameid=%s, %s 项)",
                group_id, gameid, len(new_achievements),
            )
            return True
        if not self.group_achievement_enabled.get(group_id, True):
            return True
        achievements_to_notify = list(new_achievements)[:self.max_achievement_notifications]
        details = self.achievement_monitor.details_cache.get((group_id, gameid))
        if not details:
            try:
                details = await self.achievement_monitor.get_achievement_details(
                    group_id, gameid, lang="schinese", api_key=self.API_KEY, steamid=steamid
                )
            except Exception as e:
                details = None
                logger.warning(f"获取成就详情失败: {e}")
        if details and game_name:
            for detail in details.values():
                detail["game_name"] = game_name

        notify_sessions = []
        notify_session = getattr(self, "notify_sessions", {}).get(group_id)
        if notify_session:
            notify_sessions.append(notify_session)
        for push_gid in (getattr(self, "push_groups", {}) or {}).get(steamid, []):
            push_session = getattr(self, "notify_sessions", {}).get(push_gid)
            if push_session and push_session not in notify_sessions:
                notify_sessions.append(push_session)
        notify_sessions = [session for session in notify_sessions if is_sendable_group_session(session)]
        if not notify_sessions:
            logger.warning("成就通知无有效会话，已跳过 (group_id=%s, steamid=%s)", group_id, steamid)
            return False

        msg_parts = []
        if self.config.get("notify_send_text", True):
            msg_parts.append(Plain(f"🏆【{player_name}】在 {game_name} 解锁成就："))

        tmp_path = None
        if self.config.get("notify_send_image", True) and details:
            unlocked_set = await self.achievement_monitor.get_player_achievements(
                self.API_KEY, group_id, steamid, gameid, game_name=game_name
            )
            if not unlocked_set:
                unlocked_set = set(self.achievement_snapshots.get((group_id, steamid, gameid), []))
            try:
                img_bytes = await self.achievement_monitor.render_achievement_image(
                    details, set(achievements_to_notify), player_name=player_name,
                    steamid=steamid, appid=gameid, unlocked_set=unlocked_set or set(),
                    font_path=resolve_font_path("NotoSansHans-Regular.otf"),
                    group_id=group_id,
                )
                with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                    tmp.write(img_bytes)
                    tmp_path = tmp.name
                msg_parts.append(Image.fromFileSystem(tmp_path))
            except Exception as e:
                logger.error(f"成就图片渲染失败: {e}")

        if not msg_parts:
            logger.warning("成就通知没有可发送内容 (group_id=%s, steamid=%s)", group_id, steamid)
            return False

        async def send_one(session):
            try:
                await self.context.send_message(session, MessageChain(msg_parts))
                return True
            except Exception as e:
                logger.error("发送成就通知失败 (session=%s): %s", session, e)
                return False

        results = await asyncio.gather(*(send_one(session) for session in notify_sessions))
        return all(results)

    def _achievement_is_blacklisted(self, gameid) -> bool:
        """黑名单判断统一走 AchievementMonitor，避免插件级与监视器两套集合不同步。"""
        monitor = getattr(self, "achievement_monitor", None)
        if monitor is None:
            return False
        checker = getattr(monitor, "is_blacklisted", None)
        if callable(checker):
            return bool(checker(gameid))
        return str(gameid) in getattr(monitor, "achievement_blacklist", set())

    def _blacklist_game(self, gameid, game_name: str = "") -> bool:
        """当天失败 10 次后拉黑并落盘（含原因与游戏名，重启后保留）。"""
        monitor = getattr(self, "achievement_monitor", None)
        adder = getattr(monitor, "add_to_blacklist", None) if monitor is not None else None
        if not callable(adder):
            logger.warning(f"[成就黑名单] 成就监控未初始化，无法持久化 gameid={gameid}")
            return False
        added = adder(
            gameid,
            reason=REASON_FETCH_FAILED,
            game_name=game_name,
            source=SOURCE_AUTO,
        )
        logger.info(
            f"[成就黑名单] 游戏 {gameid} 当天累计获取失败10次"
            f"{'，已加入黑名单' if added else '，已在黑名单中'}"
        )
        return bool(added)

    def purge_achievement_runtime_state(self, appids) -> None:
        """删除黑名单条目后同步清理失败计数与成就详情缓存，避免立即被重新拉黑。"""
        wanted = {str(item) for item in (appids or [])}
        if not wanted:
            return

        fail_count = getattr(self, "achievement_fail_count", None)
        if isinstance(fail_count, dict):
            for key in list(fail_count):
                gameid = key[0] if isinstance(key, (tuple, list)) and key else key
                if str(gameid) in wanted:
                    fail_count.pop(key, None)

        monitor = getattr(self, "achievement_monitor", None)
        cache = getattr(monitor, "details_cache", None)
        if isinstance(cache, dict):
            for key in list(cache):
                appid = key[1] if isinstance(key, (tuple, list)) and len(key) > 1 else key
                if str(appid) in wanted:
                    cache.pop(key, None)
