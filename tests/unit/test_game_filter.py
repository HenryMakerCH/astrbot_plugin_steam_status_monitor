from src.domain.monitoring.game_filter import (
    add_appids,
    normalize_appid,
    parse_appids,
    remove_appids,
    should_skip_game,
)


def test_should_skip_game_all_mode_never_skips():
    assert should_skip_game({"game_filter_mode": "全部游戏", "game_filter_ids": "10"}, "10") is False
    assert should_skip_game({}, None) is False


def test_should_skip_game_blacklist_and_whitelist():
    blacklist = {"game_filter_mode": "黑名单", "game_filter_ids": "10,20"}
    whitelist = {"game_filter_mode": "白名单", "game_filter_ids": "10,20"}

    assert should_skip_game(blacklist, "10") is True
    assert should_skip_game(blacklist, "30") is False
    assert should_skip_game(whitelist, "10") is False
    assert should_skip_game(whitelist, "30") is True
    assert should_skip_game({"game_filter_mode": "黑名单", "game_filter_ids": ""}, "10") is False


def test_normalize_appid_strips_padding_and_leading_zeros():
    assert normalize_appid(" 0730 ") == "730"
    assert normalize_appid("570") == "570"


def test_normalize_appid_rejects_invalid_values():
    for token in ("", "abc", "0", "000", "73o", "７３０", "76561198099315021", "4294967296"):
        assert normalize_appid(token) is None


def test_parse_appids_supports_mixed_separators_and_dedupe():
    valid, invalid = parse_appids("730，431960 570,0730")
    assert valid == ["730", "431960", "570"]
    assert invalid == []


def test_parse_appids_collects_invalid_tokens():
    valid, invalid = parse_appids("730,abc,76561198099315021")
    assert valid == ["730"]
    assert invalid == ["abc", "76561198099315021"]


def test_add_and_remove_appids():
    merged, added = add_appids("730,431960", ["570", "0730"])
    assert (merged, added) == ("730,431960,570", ["570"])

    left, removed, missing = remove_appids("730,431960", ["730", "999"])
    assert (left, removed, missing) == ("431960", ["730"], ["999"])


def test_should_skip_game_normalizes_legacy_values():
    config = {"game_filter_mode": "黑名单", "game_filter_ids": "0730，431960 570"}
    assert should_skip_game(config, "730") is True
    assert should_skip_game(config, 431960) is True
    assert should_skip_game(config, "999") is False
