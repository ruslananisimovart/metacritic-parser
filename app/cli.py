"""Ручные команды для проверки работы сервиса.

python -m app.cli new-releases
python -m app.cli see-all --offset 20
python -m app.cli game elden-ring
python -m app.cli reviews elden-ring --kind user --limit 30
python -m app.cli summary elden-ring --kind critic
python -m app.cli run-once [--day 2026-09-12]
python -m app.cli media [--all]
"""

import argparse
import asyncio
import dataclasses
import json
import logging
import sys
from datetime import date
from typing import Any

from app.config import Settings, load_settings
from app.db import Database
from app import export
from app.gemini import GeminiClient, create_gemini_http_client
from app.metacritic import MetacriticClient, create_http_client
from app.gplay import GPlayClient
from app.pipeline import current_day, find_media, game_from_db_row, media_needs, run_once
from app.runconfig import SETTING_KEY as RUN_KEY
from app.runconfig import load as load_run
from app.steam import SteamClient, create_steam_http_client
from app.summaries import build_prompt, fetch_sample, review_total, summarize

log = logging.getLogger("app.cli")


def _dump(value: Any) -> None:
    def default(obj: Any) -> Any:
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return dataclasses.asdict(obj)
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        if isinstance(obj, BaseException):
            return {"error": f"{type(obj).__name__}: {obj}"}
        return str(obj)

    print(json.dumps(value, ensure_ascii=False, indent=2, default=default))


async def _run(args: argparse.Namespace, settings: Settings) -> None:
    async with (
        create_http_client() as http,
        create_gemini_http_client() as gemini_http,
        create_steam_http_client() as steam_http,
    ):
        steam = SteamClient(steam_http)
        gplay = GPlayClient(steam_http)
        mc = MetacriticClient(
            http,
            concurrency=settings.metacritic_concurrency,
            min_interval=settings.metacritic_min_interval,
            api_key=settings.metacritic_api_key,
        )
        # консольные команды берут модель из .env (LLM_PROVIDER), выбор из интерфейса их не касается
        llm = GeminiClient(
            gemini_http,
            settings.gemini_keys,
            settings.gemini_model,
            provider=settings.gemini_provider,
            other_keys=settings.llm_keys,
            local_url=settings.local_llm_url,
            local_model=settings.local_llm_model,
            local_ttl=settings.gpu_idle_seconds,
        )
        llm_reason = await llm.ready(fresh=True, start=True) if args.command in ("summary", "run-once") else None
        gemini = None if llm_reason else llm

        if args.command in ("new-releases", "see-all"):
            if args.command == "new-releases":
                listing = await mc.new_releases(args.limit)
            else:
                listing = await mc.see_all(args.offset, args.limit)
            games = await asyncio.gather(*(mc.game(g.slug) for g in listing.games), return_exceptions=True)
            _dump({"total": listing.total, "games": games})
        elif args.command == "game":
            _dump(await mc.game(args.slug))
        elif args.command == "reviews":
            _dump(await mc.reviews(args.slug, args.kind, limit=args.limit, sentiment=args.sentiment))
        elif args.command == "summary":
            if gemini is None:
                raise SystemExit(f"LLM {llm.title} {llm.model} is not ready: {llm_reason}")
            game = await mc.game(args.slug)
            total = review_total(game, args.kind)
            sample = await fetch_sample(mc, game.slug, args.kind)
            reviews = sample.reviews
            if args.show_prompt:
                print(build_prompt(game, args.kind, reviews, total, sample.counts), file=sys.stderr)
            summary = await summarize(gemini, game, args.kind, reviews, total, sample.counts)
            if args.save:
                # пересборка резюме уже сохранённой игры: так у старых записей появляются источники
                db = Database(settings.db_path)
                try:
                    if db.conn.execute("SELECT 1 FROM games WHERE id = ?", (game.id,)).fetchone() is None:
                        raise SystemExit(f"игры {game.slug} нет в базе, сохранять резюме некуда")
                    db.save_summary(
                        run_id=None,
                        game_id=game.id,
                        kind=args.kind,
                        likes=[point.model_dump() for point in summary.likes],
                        dislikes=[point.model_dump() for point in summary.dislikes],
                        summary=summary.summary,
                        reviews_used=len(reviews),
                        reviews_total=total,
                        model=gemini.model,
                    )
                finally:
                    db.close()
            _dump(
                {
                    "game": game.title,
                    "kind": args.kind,
                    "reviews_total": total,
                    "reviews_used": len(reviews),
                    "summary": summary,
                    "keys": [
                        {
                            "name": k.name,
                            "state": k.state(),
                            "calls": k.calls,
                            "credits": round(k.credits, 4),
                            "tokens": {"prompt": k.prompt_tokens, "output": k.output_tokens, "thinking": k.thinking_tokens},
                        }
                        for k in gemini.keys
                    ],
                }
            )
        elif args.command == "run-once":
            if gemini is None:
                log.warning("LLM %s %s is not ready (%s), review summaries are skipped", llm.title, llm.model, llm_reason)
            db = Database(settings.db_path)
            try:
                day = args.day or current_day(settings.timezone)
                result = await run_once(mc, db, day=day, trigger="cli", gemini=gemini,
                                        run=load_run(db.get_setting(RUN_KEY)), steam=steam, gplay=gplay,
                                        tz=settings.timezone)
                _dump(result)
                if settings.export_enabled:
                    counts = export.save_run(
                        settings.db_path, settings.db_path.parent / "exports",
                        run_id=result.run_id, tz=settings.timezone, keep=settings.export_keep,
                    )
                    log.info("export: %s", ", ".join(f"{name} {n}" for name, n in counts.items()))
            finally:
                db.close()
        elif args.command == "media":
            # разовый добор обложек и трейлеров из магазинов (Steam, затем Google Play) для игр базы,
            # у которых их нет с Metacritic; --all повторяет и те, где магазины уже спрашивали
            db = Database(settings.db_path)
            try:
                rows = db.conn.execute(
                    "SELECT * FROM games WHERE cover_url IS NULL OR cover_url = '' OR video_url IS NULL OR video_url = ''"
                    " OR description IS NULL OR trim(description) = '' ORDER BY id"
                ).fetchall()
                report = []
                today = current_day(settings.timezone)
                for row in rows:
                    game = game_from_db_row(row)
                    if not args.all and not media_needs(db, game, today):  # type: ignore[arg-type]
                        continue
                    fields, notes = await find_media(steam, gplay, game)  # type: ignore[arg-type]
                    db.save_media(game.id, **fields)
                    report.append({
                        "game": game.title, "cover": fields.get("cover_source"), "cover_kind": fields.get("cover_kind"),
                        "trailer": fields.get("video_title"), "description": fields.get("description_source"), "notes": notes,
                    })
                    log.info("media for %s: %s", game.slug, "; ".join(notes))
                _dump({"checked": len(report), "found": sum(1 for r in report if r["cover"] or r["trailer"]), "games": report})
            finally:
                db.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    new = commands.add_parser("new-releases", help="20 игр из блока New Releases с полными данными")
    new.add_argument("--limit", type=int, default=20)
    see_all = commands.add_parser("see-all", help="страница SEE ALL (новые) с полными данными")
    see_all.add_argument("--offset", type=int, default=0)
    see_all.add_argument("--limit", type=int, default=20)
    game = commands.add_parser("game", help="полные данные одной игры")
    game.add_argument("slug")
    reviews = commands.add_parser("reviews", help="отзывы критиков или игроков")
    reviews.add_argument("slug")
    reviews.add_argument("--kind", choices=["critic", "user"], default="critic")
    reviews.add_argument("--sentiment", choices=["all", "positive", "neutral", "negative"], default="all")
    reviews.add_argument("--limit", type=int, default=20)
    summary = commands.add_parser("summary", help="резюме отзывов одной игры через выбранную модель")
    summary.add_argument("slug")
    summary.add_argument("--kind", choices=["critic", "user"], default="critic")
    summary.add_argument("--show-prompt", action="store_true", help="вывести промпт в stderr")
    summary.add_argument("--save", action="store_true", help="записать резюме в базу (игра должна там быть)")
    run = commands.add_parser("run-once", help="один прогон: 20 игр по правилам дня в базу")
    run.add_argument("--day", type=date.fromisoformat, help="день прогона вместо сегодняшнего (для проверки смены дня)")
    media = commands.add_parser("media", help="добрать обложки и трейлеры из Steam и Google Play для игр базы, у которых их нет с Metacritic")
    media.add_argument("--all", action="store_true", help="искать и там, где магазины уже спрашивали недавно")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    # httpx пишет каждый запрос на INFO, а в URL Gemini нет ключа, но шум не нужен
    logging.getLogger("httpx").setLevel(logging.WARNING)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    asyncio.run(_run(args, load_settings()))


if __name__ == "__main__":
    main()
