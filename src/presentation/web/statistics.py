import math
from datetime import datetime, timedelta


def round_minutes(value):
    """把分钟值规范为 2 位小数，供 Web API 输出使用。"""
    try:
        minutes = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(minutes):
        return 0.0
    return round(minutes, 2)


BUCKET_MINUTES = 10
MAX_PROFILE_DAYS = 90
MAX_SESSION_SPAN_SECONDS = 86400


def build_report_window(now, days, offset):
    """Return the report time window using the dashboard's 04:00 day boundary."""
    boundary_today = now.replace(hour=4, minute=0, second=0, microsecond=0)
    if now < boundary_today:
        boundary_today -= timedelta(days=1)
    range_end = boundary_today + timedelta(days=offset + 1)
    return range_end - timedelta(days=days), range_end


def _build_display_names(plugin):
    names = {}
    for info in (getattr(plugin, "_bind_data", {}) or {}).values():
        sid = str(info.get("sid", ""))
        nickname = info.get("nickname", "")
        if sid and nickname and nickname != "*":
            names.setdefault(sid, nickname)

    for states in (getattr(plugin, "group_last_states", {}) or {}).values():
        for sid, state in list(states.items()):
            sid = str(sid)
            if sid not in names and state.get("name"):
                names[sid] = state["name"]
    return names


def build_dashboard_stats(plugin, today, last_update):
    groups = getattr(plugin, "group_steam_ids", {}) or {}
    push_groups = getattr(plugin, "push_groups", {}) or {}
    all_sids = {str(sid) for sids in groups.values() for sid in list(sids)}
    all_sids.update(str(sid) for sid in push_groups)
    bind_data = getattr(plugin, "_bind_data", {}) or {}
    today_records = (getattr(plugin, "play_records", {}) or {}).get(today, {})
    display_names = _build_display_names(plugin)

    game_totals = {}
    player_totals = {}
    for sid, sid_data in list(today_records.items()):
        total = 0
        for gid, game_info in list(sid_data.items()):
            minutes = game_info.get("minutes", 0) if isinstance(game_info, dict) else 0
            name = game_info.get("name", gid) if isinstance(game_info, dict) else str(gid)
            aggregate = game_totals.setdefault(
                gid, {"name": name, "minutes": 0, "player_count": 0}
            )
            aggregate["minutes"] += minutes
            aggregate["player_count"] += 1
            total += minutes
        if total > 0:
            player_totals[str(sid)] = total

    top_games = sorted(
        list(game_totals.values()), key=lambda item: -item["minutes"]
    )[:10]
    top_players = sorted(
        [
            {"sid": sid, "name": display_names.get(sid, sid), "minutes": minutes}
            for sid, minutes in player_totals.items()
        ],
        key=lambda item: -item["minutes"],
    )[:10]

    online_count = 0
    players = []
    seen_players = set()
    for states in (getattr(plugin, "group_last_states", {}) or {}).values():
        for sid, state in list(states.items()):
            sid = str(sid)
            if sid in seen_players:
                continue
            seen_players.add(sid)
            if state.get("gameid") or state.get("personastate", 0) > 0:
                online_count += 1
            players.append({
                "sid": sid,
                "name": display_names.get(sid, sid),
                "gameid": state.get("gameid", ""),
                "game": state.get("gameextrainfo", ""),
                "personastate": state.get("personastate", 0),
                "avatar_url": state.get("avatarfull") or state.get("avatar", ""),
            })

    return {
        "total_groups": len(groups),
        "total_players": len(all_sids),
        "total_bindings": len(bind_data),
        "today_active_players": len(today_records),
        "online_players": online_count,
        "top_games_today": [
            {**item, "minutes": round_minutes(item["minutes"])} for item in top_games
        ],
        "top_players_today": [
            {**item, "minutes": round_minutes(item["minutes"])} for item in top_players
        ],
        "players": players,
        "last_update": last_update,
    }


def build_groups(plugin):
    display_names = _build_display_names(plugin)
    groups = getattr(plugin, "group_steam_ids", {}) or {}
    push_groups = getattr(plugin, "push_groups", {}) or {}
    last_states = getattr(plugin, "group_last_states", {}) or {}
    result = {}
    for group_id, direct_ids in list(groups.items()):
        visible_ids = list(direct_ids)
        visible_ids.extend(
            sid
            for sid, target_groups in push_groups.items()
            if str(group_id) in {str(target) for target in target_groups}
            and sid not in visible_ids
        )
        states = last_states.get(group_id, {})
        result[group_id] = [
            {
                "sid": str(sid),
                "name": display_names.get(str(sid), str(sid)),
                "gameid": states.get(str(sid), {}).get("gameid", ""),
                "game": states.get(str(sid), {}).get("gameextrainfo", ""),
                "personastate": states.get(str(sid), {}).get("personastate", 0),
                "is_push_group": sid not in direct_ids,
            }
            for sid in visible_ids
        ]
    return result


def build_player_search_index(plugin):
    display_names = _build_display_names(plugin)
    groups = getattr(plugin, "group_steam_ids", {}) or {}
    push_groups = getattr(plugin, "push_groups", {}) or {}
    result = [
        {
            "sid": str(sid),
            "name": display_names.get(str(sid), str(sid)),
            "group_id": group_id,
            "is_push_group": False,
        }
        for group_id, steam_ids in list(groups.items())
        for sid in list(steam_ids)
    ]
    result.extend(
        {
            "sid": str(sid),
            "name": display_names.get(str(sid), str(sid)),
            "group_id": group_id,
            "is_push_group": True,
        }
        for sid, target_groups in push_groups.items()
        for group_id in target_groups
        if str(sid) not in {
            str(item["sid"])
            for item in result
            if str(item["group_id"]) == str(group_id)
        }
    )
    return result


def _build_heatmap_contributions(plugin, start_key, end_key, allowed_sids):
    """按玩家和日期聚合时长，同日优先采用精确会话以避免重复计数。"""

    contributions = {}
    session_days = set()
    session_records = getattr(plugin, "session_records", {}) or {}
    for raw_sid, sessions in list(session_records.items()):
        sid = str(raw_sid)
        if sid not in allowed_sids:
            continue
        for session in list(sessions):
            date_key = session.get("date", "")
            if not start_key <= date_key <= end_key:
                continue
            minutes = max(0.0, round_minutes(session.get("duration_min", 0)))
            if minutes <= 0:
                continue
            player_day = contributions.setdefault(date_key, {}).setdefault(
                sid, {"minutes": 0, "games": {}}
            )
            player_day["minutes"] += minutes
            game_id = str(session.get("gameid", ""))
            game_name = session.get("game_name", game_id or "未知游戏")
            game = player_day["games"].setdefault(
                game_id, {"gameid": game_id, "name": game_name, "minutes": 0}
            )
            game["minutes"] += minutes
            session_days.add((date_key, sid))

    play_records = getattr(plugin, "play_records", {}) or {}
    for date_key, day_records in list(play_records.items()):
        if not start_key <= date_key <= end_key:
            continue
        for raw_sid, games in list(day_records.items()):
            sid = str(raw_sid)
            if sid not in allowed_sids or (date_key, sid) in session_days:
                continue
            player_day = contributions.setdefault(date_key, {}).setdefault(
                sid, {"minutes": 0, "games": {}}
            )
            for raw_game_id, game_info in list(games.items()):
                if not isinstance(game_info, dict):
                    continue
                minutes = max(0, int(game_info.get("minutes", 0) or 0))
                if minutes <= 0:
                    continue
                game_id = str(raw_game_id)
                game_name = game_info.get("name", game_id)
                player_day["minutes"] += minutes
                game = player_day["games"].setdefault(
                    game_id, {"gameid": game_id, "name": game_name, "minutes": 0}
                )
                game["minutes"] += minutes
    return contributions


def build_heatmap_data(plugin, period, now, group_id=None):
    end_date = now.replace(hour=4, minute=0, second=0, microsecond=0)
    if end_date <= now:
        end_date += timedelta(days=1)
    start_date = end_date - timedelta(days=period)
    start_key = start_date.strftime("%Y-%m-%d")
    end_key = end_date.strftime("%Y-%m-%d")

    groups = getattr(plugin, "group_steam_ids", {}) or {}
    allowed_sids = _resolve_allowed_sids(plugin, group_id)
    contributions = _build_heatmap_contributions(
        plugin, start_key, end_key, allowed_sids
    )
    display_names = _build_display_names(plugin)
    player_minutes = {}
    heatmap_daily = {}
    daily_contributors = {}

    day = start_date
    while day <= end_date:
        date_key = day.strftime("%Y-%m-%d")
        day_players = []
        for sid, contribution in contributions.get(date_key, {}).items():
            minutes = contribution["minutes"]
            if minutes <= 0:
                continue
            player_minutes[sid] = player_minutes.get(sid, 0) + minutes
            day_players.append({
                "sid": sid,
                "name": display_names.get(sid, sid),
                "total_minutes": minutes,
                "games": sorted(
                    contribution["games"].values(),
                    key=lambda item: -item["minutes"],
                ),
            })
        day_players.sort(key=lambda item: -item["total_minutes"])
        total_minutes = sum(player["total_minutes"] for player in day_players)
        heatmap_daily[date_key] = total_minutes
        daily_contributors[date_key] = day_players
        day += timedelta(days=1)

    players = sorted(
        [
            {
                "sid": sid,
                "name": display_names.get(sid, sid),
                "total_minutes": minutes,
            }
            for sid, minutes in player_minutes.items()
        ],
        key=lambda item: -item["total_minutes"],
    )
    for day_players in daily_contributors.values():
        for player in day_players:
            player["total_minutes"] = round_minutes(player["total_minutes"])
            for game in player["games"]:
                game["minutes"] = round_minutes(game["minutes"])
    for date_key, total in heatmap_daily.items():
        heatmap_daily[date_key] = round_minutes(total)
    for player in players:
        player["total_minutes"] = round_minutes(player["total_minutes"])
    group_options = [
        {"id": str(group), "player_count": len({str(sid) for sid in steam_ids})}
        for group, steam_ids in groups.items()
    ]
    return {
        "heatmap_data": heatmap_daily,
        "daily_contributors": daily_contributors,
        "players": players,
        "groups": group_options,
        "selected_group": group_id or "",
    }


def _resolve_allowed_sids(plugin, group_id=None):
    """解析参与统计的 SteamID：群成员 + 该群的联动推送玩家。"""
    groups = getattr(plugin, "group_steam_ids", {}) or {}
    push_groups = getattr(plugin, "push_groups", {}) or {}
    if group_id:
        direct_ids = groups.get(group_id, [])
        push_ids = [
            sid
            for sid, target_groups in push_groups.items()
            if str(group_id) in {str(target) for target in target_groups}
        ]
        return {str(sid) for sid in [*direct_ids, *push_ids]}
    return {
        str(sid) for steam_ids in groups.values() for sid in steam_ids
    } | {str(sid) for sid in push_groups}


def _session_interval(session):
    """还原会话的绝对起止时间；缺少 start_time 或时长非法时返回 None。"""
    if not isinstance(session, dict):
        return None
    try:
        start = int(session.get("start_time") or 0)
    except (TypeError, ValueError):
        start = 0
    try:
        end = int(session.get("end_time") or 0)
    except (TypeError, ValueError):
        end = 0
    try:
        duration_min = float(session.get("duration_min") or 0)
    except (TypeError, ValueError):
        duration_min = 0.0
    if not math.isfinite(duration_min) or duration_min < 0:
        duration_min = 0.0
    if start <= 0:
        return None
    if end <= 0:
        end = start + int(duration_min * 60)
    if end <= start:
        return None
    if end - start > MAX_SESSION_SPAN_SECONDS:
        end = start + MAX_SESSION_SPAN_SECONDS
    return start, end


def _bucket_label(index, bucket_minutes):
    start = index * bucket_minutes
    return f"{start // 60:02d}:{start % 60:02d}"


def build_active_profile(plugin, period, now, group_id=None, bucket_minutes=BUCKET_MINUTES):
    """按自然日统计各时段平均同时游戏人数与单日峰值。

    仅使用 session_records（含精确起止时间），进行中的会话不计入；
    同一天同一时段按 sid 去重；平均值除以窗口内全部自然日数；
    周期自动截到 MAX_PROFILE_DAYS（会话记录保留期）。
    """
    try:
        bucket_minutes = int(bucket_minutes or BUCKET_MINUTES)
    except (TypeError, ValueError):
        bucket_minutes = BUCKET_MINUTES
    bucket_minutes = max(1, min(bucket_minutes, 60))
    buckets_per_day = (24 * 60) // bucket_minutes

    try:
        requested = int(period or 30)
    except (TypeError, ValueError):
        requested = 30
    requested = max(1, requested)
    days = min(requested, MAX_PROFILE_DAYS)

    today = now.date()
    start_day = today - timedelta(days=days - 1)
    start_ts = datetime(start_day.year, start_day.month, start_day.day).timestamp()
    end_ts = datetime(today.year, today.month, today.day).timestamp() + 86400

    allowed_sids = _resolve_allowed_sids(plugin, group_id)
    sessions = getattr(plugin, "session_records", {}) or {}
    buckets = {}
    days_with_data = set()
    total_sessions = 0
    skipped = 0

    for raw_sid, items in list(sessions.items()):
        sid = str(raw_sid)
        if sid not in allowed_sids:
            continue
        for session in list(items or []):
            interval = _session_interval(session)
            if not interval:
                skipped += 1
                continue
            session_start, session_end = interval
            if session_end <= start_ts or session_start >= end_ts:
                continue
            cursor = max(session_start, start_ts)
            session_end = min(session_end, end_ts)
            if session_end <= cursor:
                continue
            total_sessions += 1
            while cursor < session_end:
                day_start = datetime.fromtimestamp(cursor).replace(
                    hour=0, minute=0, second=0, microsecond=0
                ).timestamp()
                chunk_end = min(session_end, day_start + 86400)
                day_index = int(round((day_start - start_ts) / 86400))
                if 0 <= day_index < days:
                    first = int((cursor - day_start) // (bucket_minutes * 60))
                    last = int((chunk_end - 1 - day_start) // (bucket_minutes * 60))
                    for index in range(
                        max(0, first), min(buckets_per_day - 1, last) + 1
                    ):
                        buckets.setdefault((day_index, index), set()).add(sid)
                    days_with_data.add(day_index)
                cursor = chunk_end

    bucket_list = []
    total_avg = 0.0
    peak_value = 0
    peak_label = ""
    for index in range(buckets_per_day):
        daily_counts = [
            len(buckets.get((day_index, index), ()))
            for day_index in range(days)
        ]
        series_avg = sum(daily_counts) / days if days else 0.0
        bucket_peak = max(daily_counts) if daily_counts else 0
        total_avg += series_avg
        label = _bucket_label(index, bucket_minutes)
        bucket_list.append({
            "label": label,
            "start_minute": index * bucket_minutes,
            "avg": round(series_avg, 2),
            "peak": bucket_peak,
        })
        if bucket_peak > peak_value:
            peak_value = bucket_peak
            peak_label = label

    note = ""
    if requested > MAX_PROFILE_DAYS:
        note = f"会话记录仅保留最近 {MAX_PROFILE_DAYS} 天，已按最近 {days} 天统计"

    return {
        "bucket_minutes": bucket_minutes,
        "buckets": bucket_list,
        "days": days,
        "days_with_data": len(days_with_data),
        "period": days,
        "requested_period": requested,
        "selected_group": group_id or "",
        "summary": {
            "avg_online": round(total_avg / buckets_per_day, 2) if buckets_per_day else 0.0,
            "peak": peak_value,
            "peak_label": peak_label,
            "sessions": total_sessions,
            "skipped": skipped,
        },
        "note": note,
    }
