"""Interactive category picker (single or multi select)."""

from __future__ import annotations

import sys
from collections.abc import Iterable
from typing import Any

MAX_ROWS_PER_SCREEN = 200


def _class_name(classes: list[dict[str, Any]], class_id: str) -> str:
    for cls in classes:
        if str(cls.get("classId")) == str(class_id):
            return str(cls.get("className") or "")
    return ""


def filter_games(
    games: Iterable[dict[str, Any]], keyword: str = "", class_id: str | None = None
) -> list[dict[str, Any]]:
    keyword = keyword.strip().lower()
    out = []
    for game in games:
        if class_id and str(game.get("gameClassId")) != str(class_id):
            continue
        if keyword:
            haystack = f"{game.get('gameName', '')} {game.get('gameId', '')}".lower()
            if keyword not in haystack:
                continue
        out.append(game)
    return out


def print_games(games: list[dict[str, Any]], classes: list[dict[str, Any]]) -> None:
    width = max((len(str(g.get("gameName") or "")) for g in games), default=8)
    width = min(max(width, 8), 30)
    print(f"{'#':>5}  {'游戏':<{width}}  {'游戏ID':<10}  {'大类'}")
    print("-" * (width + 26))
    for index, game in enumerate(games, 1):
        name = str(game.get("gameName") or "")[:width]
        cls = _class_name(classes, str(game.get("gameClassId") or ""))
        print(f"{index:>5}  {name:<{width}}  {game.get('gameId')!s:<10}  {cls}")


def _parse_selection(raw: str, total: int) -> list[int]:
    """Parse '1,3,5-8' into zero-based indexes."""
    picked: list[int] = []
    for token in raw.replace("，", ",").split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            start, _, end = token.partition("-")
            if not (start.strip().isdigit() and end.strip().isdigit()):
                raise ValueError(f"无法解析区间: {token}")
            a, b = int(start), int(end)
            if a > b:
                a, b = b, a
            picked.extend(range(a, b + 1))
        else:
            if not token.isdigit():
                raise ValueError(f"无法解析编号: {token}")
            picked.append(int(token))
    indexes = []
    for number in picked:
        if not 1 <= number <= total:
            raise ValueError(f"编号 {number} 超出范围 1-{total}")
        indexes.append(number - 1)
    return sorted(set(indexes))


def interactive_select(
    games: list[dict[str, Any]],
    classes: list[dict[str, Any]],
    *,
    mode: str = "multi",
) -> list[dict[str, Any]]:
    """Let the user narrow down by keyword, then pick one or many games."""
    if not sys.stdin.isatty():
        raise RuntimeError("当前不是交互终端，请用 --game/--class/--all 指定分类")

    print(f"\n共 {len(games)} 个可爬取分类（游戏）。")
    if classes:
        parts = [f"{c.get('className')}({c.get('classId')})" for c in classes]
        print("大类: " + "  ".join(parts))

    keyword = input("按关键字过滤（直接回车=全部，输入后回车显示列表）: ").strip()
    visible = filter_games(games, keyword)
    if not visible:
        raise RuntimeError(f"没有匹配 {keyword!r} 的分类")

    if len(visible) > MAX_ROWS_PER_SCREEN:
        print(f"匹配 {len(visible)} 个，只显示前 {MAX_ROWS_PER_SCREEN} 个；可用关键字缩小范围。")
        visible = visible[:MAX_ROWS_PER_SCREEN]
    print()
    print_games(visible, classes)

    hint = "输入编号（如 3，支持 1,3,5 或 2-6；a=全部；q=退出）"
    if mode == "single":
        hint = "输入编号（单选；q=退出）"
    while True:
        raw = input(f"\n{hint}: ").strip()
        if raw.lower() in ("q", "quit", "exit"):
            raise KeyboardInterrupt
        if raw.lower() in ("a", "all", "*"):
            if mode == "single":
                print("当前为单选模式，请输入单个编号。")
                continue
            return list(visible)
        try:
            indexes = _parse_selection(raw, len(visible))
        except ValueError as exc:
            print(f"输入无效: {exc}")
            continue
        if not indexes:
            print("没有选择任何分类。")
            continue
        if mode == "single" and len(indexes) != 1:
            print("当前为单选模式，请输入单个编号（或使用 --select multi）。")
            continue
        return [visible[i] for i in indexes]
