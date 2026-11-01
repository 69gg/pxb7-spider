"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from .api import Pxb7API
from .browser import BrowserSession, WafBlocked
from .crawler import Crawler
from .picker import filter_games, interactive_select, print_games
from .store import JsonlWriter, StateStore

log = logging.getLogger("pxb7")

DEFAULT_OUT = "data/products.jsonl"
DEFAULT_STATE = "data/state.db"


def _split_ids(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        for part in value.replace("，", ",").split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pxb7-spider",
        description="递归抓取 pxb7.com 商品信息（JSONL 追加写入，支持断点续接）",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")

    sub = parser.add_subparsers(dest="command", required=True)

    # ---------------------------------------------------------------- games
    games = sub.add_parser("games", help="列出可爬取的分类（游戏）")
    games.add_argument("--json", action="store_true", help="以 JSON 输出")
    games.add_argument("--keyword", default="", help="按名称/ID 过滤")
    games.add_argument("--class-id", default="", help="只显示某个大类")
    games.add_argument("--limit", type=int, default=0, help="最多显示多少条")
    games.add_argument("--headless", dest="headless", action="store_true", default=True)
    games.add_argument("--headed", dest="headless", action="store_false")

    # ---------------------------------------------------------------- crawl
    crawl = sub.add_parser("crawl", help="开始抓取")
    sel = crawl.add_argument_group("分类选择")
    sel.add_argument("--all", action="store_true", help="抓取全部游戏")
    sel.add_argument("--game", action="append", metavar="ID", help="游戏ID，可重复或逗号分隔")
    sel.add_argument("--class-id", action="append", metavar="ID", help="大类ID，可重复")
    sel.add_argument("--keyword", default="", help="按名称/ID 过滤游戏")
    sel.add_argument("--interactive", action="store_true", help="交互式选择（默认无参数时启用）")
    sel.add_argument(
        "--select", choices=("single", "multi"), default="multi", help="交互选择模式，默认多选"
    )

    out = crawl.add_argument_group("输出与断点")
    out.add_argument("--out", default=DEFAULT_OUT, help=f"JSONL 输出路径（默认 {DEFAULT_OUT}）")
    out.add_argument("--state", default=DEFAULT_STATE, help=f"断点数据库（默认 {DEFAULT_STATE}）")
    out.add_argument("--restart", action="store_true", help="忽略断点，从头开始")
    out.add_argument("--retry-failed", action="store_true", help="重试之前失败的商品")
    out.add_argument("--keep-raw-detail", action="store_true", help="在记录里保留详情接口原始 JSON")

    perf = crawl.add_argument_group("抓取参数")
    perf.add_argument("--concurrency", type=int, default=4, help="并发标签页数（默认 4）")
    perf.add_argument("--page-size", type=int, default=100, help="列表每页条数（默认 100）")
    perf.add_argument("--biz-prod", type=int, default=1, help="业务类型，1=账号（默认）")
    perf.add_argument("--delay-min", type=float, default=0.1, help="每次请求最小间隔秒")
    perf.add_argument("--delay-max", type=float, default=0.3, help="每次请求最大间隔秒")
    perf.add_argument("--max-pages", type=int, default=0, help="每个游戏最多翻多少页（0=不限）")
    perf.add_argument("--limit", type=int, default=0, help="本次最多入队多少商品（0=不限）")
    perf.add_argument("--retries", type=int, default=4, help="单次请求重试次数")
    perf.add_argument("--timeout", type=int, default=25, help="单次请求超时秒数")
    perf.add_argument("--browser-channel", default="chrome", help="浏览器通道，默认 chrome")

    crawl.add_argument("--headless", dest="headless", action="store_true", default=True)
    crawl.add_argument("--headed", dest="headless", action="store_false")

    # --------------------------------------------------------------- status
    status = sub.add_parser("status", help="查看断点进度")
    status.add_argument("--state", default=DEFAULT_STATE, help=f"断点数据库（默认 {DEFAULT_STATE}）")

    return parser


async def _fetch_catalog(args: argparse.Namespace) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    session = BrowserSession(
        headless=args.headless,
        concurrency=max(1, getattr(args, "concurrency", 1)),
        channel=getattr(args, "browser_channel", "chrome"),
        timeout_ms=getattr(args, "timeout", 25) * 1000,
        retries=getattr(args, "retries", 4),
    )
    await session.start()
    try:
        api = Pxb7API(session)
        classes = await api.game_classes()
        games = await api.games()
    finally:
        await session.close()
    return classes, games


def _select_games(
    args: argparse.Namespace, classes: list[dict[str, Any]], games: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_id = {str(g.get("gameId")): g for g in games}

    if args.all:
        return list(games)

    game_ids = _split_ids(args.game)
    class_ids = _split_ids(args.class_id)
    if game_ids or class_ids:
        chosen: list[dict[str, Any]] = []
        for gid in game_ids:
            game = by_id.get(gid)
            if game is None:
                raise SystemExit(f"未知游戏ID: {gid}（用 `pxb7-spider games --keyword {gid}` 查）")
            chosen.append(game)
        for cid in class_ids:
            matched = [g for g in games if str(g.get("gameClassId")) == cid]
            if not matched:
                raise SystemExit(f"大类 {cid} 下没有游戏")
            chosen.extend(matched)
        if args.keyword:
            chosen = filter_games(chosen, args.keyword)
        # de-duplicate while keeping order
        seen: set[str] = set()
        unique = []
        for game in chosen:
            gid = str(game.get("gameId"))
            if gid not in seen:
                seen.add(gid)
                unique.append(game)
        return unique

    if args.keyword:
        matched = filter_games(games, args.keyword)
        if not matched:
            raise SystemExit(f"没有匹配 {args.keyword!r} 的分类")
        return matched

    if args.interactive or sys.stdin.isatty():
        return interactive_select(games, classes, mode=args.select)

    raise SystemExit(
        "未指定分类。使用 --all、--game ID、--class-id ID，或在终端中直接运行以交互选择。"
    )


async def _cmd_games(args: argparse.Namespace) -> int:
    classes, games = await _fetch_catalog(args)
    visible = filter_games(games, args.keyword, args.class_id or None)
    if args.limit:
        visible = visible[: args.limit]
    if args.json:
        print(json.dumps(visible, ensure_ascii=False, indent=2))
    else:
        print("大类: " + "  ".join(f"{c.get('className')}({c.get('classId')})" for c in classes))
        print_games(visible, classes)
        print(f"\n共 {len(visible)} 个")
    return 0


async def _cmd_crawl(args: argparse.Namespace) -> int:
    if args.delay_min < 0 or args.delay_max < args.delay_min:
        raise SystemExit("--delay-min/--delay-max 不合法")
    if (
        not (args.all or args.game or args.class_id or args.keyword or args.interactive)
        and not sys.stdin.isatty()
    ):
        raise SystemExit(
            "未指定分类。使用 --all、--game ID、--class-id ID，或在终端中直接运行以交互选择。"
        )

    store = StateStore(args.state)
    writer = JsonlWriter(args.out)
    session = BrowserSession(
        headless=args.headless,
        concurrency=args.concurrency,
        channel=args.browser_channel,
        timeout_ms=args.timeout * 1000,
        retries=args.retries,
    )
    crawler: Crawler | None = None
    try:
        await session.start()
        api = Pxb7API(session)
        classes = await api.game_classes()
        games = await api.games()
        selected = _select_games(args, classes, games)
        log.info("selected %d game(s)", len(selected))

        crawler = Crawler(
            api,
            store,
            writer,
            concurrency=args.concurrency,
            delay=(args.delay_min, args.delay_max),
            page_size=args.page_size,
            biz_prod=args.biz_prod,
            max_pages=args.max_pages,
            limit=args.limit,
            retry_failed=args.retry_failed,
            restart=args.restart,
            keep_raw_detail=args.keep_raw_detail,
        )
        store.set_meta("out", str(Path(args.out).resolve()))
        store.set_meta("last_run", json.dumps(
            {"games": [str(g.get("gameId")) for g in selected], "args": vars(args)},
            ensure_ascii=False,
        ))
        stats = await crawler.run(selected)
        print(f"\n完成: {stats.line()}")
        print(f"输出: {Path(args.out).resolve()}")
        print(f"断点: {Path(args.state).resolve()}")
    except KeyboardInterrupt:
        log.warning("已中断，进度已保存；下次运行同一命令即可续接")
        return 130
    except WafBlocked as exc:
        log.error("WAF 拦截: %s", exc)
        return 2
    finally:
        if crawler is not None:
            crawler.stop()
        writer.close()
        store.close()
        await session.close()
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    path = Path(args.state)
    if not path.exists():
        print(f"断点数据库不存在: {path}")
        return 1
    with StateStore(path) as store:
        stats = store.stats()
        print(f"数据库: {path.resolve()}")
        for key, value in stats.items():
            print(f"  {key:>16}: {value}")
        out = store.get_meta("out")
        if out:
            print(f"  {'out':>16}: {out}")
        rows = store.conn.execute(
            "SELECT game_id, game_name, state, next_page, pages_done FROM games "
            "ORDER BY updated_at DESC LIMIT 10"
        ).fetchall()
        if rows:
            print("\n最近更新的游戏:")
            for row in rows:
                print(
                    f"  {row['game_id']:<10} {row['game_name'][:16]:<18} "
                    f"{row['state']:<12} next_page={row['next_page']} pages={row['pages_done']}"
                )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        if args.command == "games":
            return asyncio.run(_cmd_games(args))
        if args.command == "crawl":
            return asyncio.run(_cmd_crawl(args))
        if args.command == "status":
            return _cmd_status(args)
    except KeyboardInterrupt:
        print("\n已中断，进度已保存；重跑同一命令即可续接", file=sys.stderr)
        return 130
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
