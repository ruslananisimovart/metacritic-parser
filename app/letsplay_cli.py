"""Ручные команды для летсплеев (дополнительная часть 1).

python -m app.letsplay_cli search "Elden Ring" --release 2022-02-25
python -m app.letsplay_cli choose "Elden Ring" --release 2022-02-25
python -m app.letsplay_cli text dnpjxeDDThw --source whisper
python -m app.letsplay_cli game elden-ring hades-ii
python -m app.letsplay_cli conclusion elden-ring     заключение заново по сохранённой расшифровке
python -m app.letsplay_cli pending --limit 3

Модель берётся как в сервисе: LLM_PROVIDER и LOCAL_LLM_MODEL из .env или окружения.
"""

import argparse
import asyncio
import dataclasses
import json
import logging
import sys
from datetime import date
from typing import Any

from app.config import ROOT, load_settings
from app.db import Database
from app.gemini import GeminiClient, create_gemini_http_client
from app.letsplay import (
    Candidate,
    GameRef,
    LetsplayStore,
    LetsplayWorker,
    VideoText,
    _utc_now,
    game_from_row,
    process_game,
    settings_from_env,
)
from app.transcribe import Transcriber
from app.youtube import YouTubeClient, create_youtube_http_client

log = logging.getLogger("app.letsplay_cli")


def dump(value: Any) -> None:
    def default(obj: Any) -> Any:
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return dataclasses.asdict(obj)
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        return str(obj)

    print(json.dumps(value, ensure_ascii=False, indent=2, default=default))


def show(candidates: list[Candidate]) -> None:
    for candidate in candidates:
        minutes = f"{candidate.duration // 60}:{candidate.duration % 60:02d}" if candidate.duration else "?"
        print(f"  {candidate.video_id}  {(candidate.views or 0):>12,}  {minutes:>8}  {candidate.title[:70]}".replace(",", " "))


async def run(args: argparse.Namespace) -> None:
    settings = load_settings()
    letsplay_settings = settings_from_env(ROOT)
    async with create_youtube_http_client() as yt_http, create_gemini_http_client() as gemini_http:
        youtube = YouTubeClient(yt_http, cookies=settings.youtube_cookies)
        transcriber = Transcriber.from_env()
        llm = GeminiClient(
            gemini_http,
            settings.gemini_keys,
            settings.gemini_model,
            provider=settings.gemini_provider,
            other_keys=settings.llm_keys,
            local_url=settings.local_llm_url,
            local_model=settings.local_llm_model,
            local_ttl=settings.gpu_idle_seconds,
            # локальная модель и Whisper делят видеокарту, как в сервисе
            gpu_lock=transcriber.gpu,
        )
        # поиск и расшифровка обходятся без модели, она нужна только выбору и заключению
        if args.command in ("choose", "game", "pending", "conclusion"):
            reason = await llm.ready(fresh=True, start=True)
            if reason:
                raise SystemExit(f"LLM {llm.title} {llm.model} is not ready: {reason}")
            log.info("LLM: %s, model %s", llm.title, llm.model)
        worker = LetsplayWorker(youtube, llm, transcriber, letsplay_settings)
        try:
            await command(args, worker, settings.db_path, letsplay_settings)
        finally:
            await llm.release_local()


async def command(args: argparse.Namespace, worker: LetsplayWorker, db_path: Any, letsplay_settings: Any) -> None:
    if args.command in ("search", "choose"):
        game = GameRef(id=0, title=args.title, release_date=args.release)
        candidates = await worker.find_candidates(game)
        print(f"после отсева правилами: {len(candidates)}")
        show(candidates)
        if args.command == "choose" and candidates:
            chosen = await worker.choose(game, candidates)
            print(f"отмечены как летсплеи: {len(chosen)}")
            show(chosen)
    elif args.command == "text":
        candidate = Candidate(
            video_id=args.video_id, title="", channel=None, duration=None, views=None, published=None, was_live=False
        )
        worker.settings = dataclasses.replace(letsplay_settings, transcript_source=args.source)
        text = await worker.text_for(candidate, hint=args.hint)
        print(
            f"источник {text.source}, язык {text.language}, сегментов {len(text.segments)}, "
            f"символов {len(text.text)}, {text.chars_per_minute:.0f} символов в минуту"
        )
        print(f"начало: {text.text[:400]}")
        print(f"конец:  {text.text[-400:]}")
    elif args.command == "conclusion":
        # заключение заново по тому, что уже расшифровано: ролики не скачиваются, Whisper не запускается
        db = Database(db_path)
        try:
            LetsplayStore(db)
            for slug in args.slugs:
                row = db.conn.execute(
                    "SELECT g.title AS game_title, l.* FROM games g JOIN letsplays l ON l.game_id = g.id WHERE g.slug = ?",
                    (slug,),
                ).fetchone()
                if row is None or row["status"] != "done" or not row["transcript"]:
                    print(f"== {slug}: расшифровки нет, пропускаем")
                    continue
                segments = [(float(s), float(e), t) for s, e, t in json.loads(row["segments"] or "[]")]
                # записи до 14.09 расшифрованы по краям, у новых покрытие записано
                coverage = row["coverage"] if "coverage" in row.keys() else None
                covered = worker.transcriber.head_seconds + worker.transcriber.tail_seconds
                whole = coverage == "full" if coverage else not (
                    (row["transcript_source"] or "whisper") == "whisper" and bool(row["duration"]) and row["duration"] > covered
                )
                text = VideoText(
                    row["transcript_source"] or "whisper", row["language"] or "", segments,
                    row["transcript"], float(row["duration"] or 0), whole,
                )
                candidate = Candidate(
                    video_id=row["video_id"], title=row["title"], channel=row["channel"], duration=row["duration"],
                    views=row["views"], published=row["published"], was_live=False,
                )
                print(f"== {row['game_title']} ({slug})")
                conclusion = await worker.conclude(GameRef(id=0, title=row["game_title"]), candidate, text, not whole)
                db.conn.execute("BEGIN IMMEDIATE")
                db.conn.execute(
                    "UPDATE letsplays SET conclusion = ?, updated_at = ? WHERE game_id = ?",
                    (json.dumps(conclusion.model_dump(), ensure_ascii=False), _utc_now(), row["game_id"]),
                )
                db.conn.execute("COMMIT")
                dump(conclusion)
        finally:
            db.close()
    elif args.command in ("game", "pending"):
        db = Database(db_path)
        try:
            store = LetsplayStore(db)
            if args.command == "game":
                rows = []
                for slug in args.slugs:
                    row = db.conn.execute("SELECT * FROM games WHERE slug = ?", (slug,)).fetchone()
                    if row is None:
                        raise SystemExit(f"игры {slug} нет в базе")
                    rows.append(row)
            else:
                rows = db.conn.execute("SELECT * FROM games ORDER BY id LIMIT ?", (args.limit * 5,)).fetchall()
                rows = [
                    r
                    for r in rows
                    if store.needs_search(r["id"], letsplay_settings)
                ][: args.limit]
            for row in rows:
                game = game_from_row(row)
                print(f"== {game.title} ({row['slug']})")
                result = await process_game(worker, store, game)
                dump(
                    {
                        "status": result.status,
                        "video": result.candidate.url if result.candidate else None,
                        "title": result.candidate.title if result.candidate else None,
                        "views": result.candidate.views if result.candidate else None,
                        "source": result.source,
                        "language": result.language,
                        "transcript_chars": len(result.transcript),
                        "conclusion": result.conclusion,
                        "error": result.error,
                    }
                )
        finally:
            db.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.letsplay_cli")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("search", "поиск и отсев правилами"), ("choose", "плюс разметка через модель")):
        command_parser = commands.add_parser(name, help=help_text)
        command_parser.add_argument("title")
        command_parser.add_argument("--release", type=date.fromisoformat, help="дата выхода игры")
    text = commands.add_parser("text", help="расшифровка одного ролика")
    text.add_argument("video_id")
    text.add_argument("--source", choices=["whisper", "captions"], default="whisper")
    text.add_argument("--hint", help="название игры как подсказка для Whisper")
    game = commands.add_parser("game", help="полный проход по играм из базы")
    game.add_argument("slugs", nargs="+")
    conclusion = commands.add_parser("conclusion", help="заключение заново по сохранённой расшифровке")
    conclusion.add_argument("slugs", nargs="+")
    pending = commands.add_parser("pending", help="игры без летсплея из базы")
    pending.add_argument("--limit", type=int, default=3)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
