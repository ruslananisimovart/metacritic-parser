"""Хранилище каталога: сохранённые итерации сбора данных.

"Опустошить" сохраняет итерацию: снимок базы целиком ложится в data/storage/<дата_время>/games.db рядом
с manifest.json, после чего игры, резюме, летсплеи, трейлеры и история прогонов из рабочей базы удаляются,
и сервис выглядит как чистый. Итерация покрывает дни прогонов, собранных с прошлой очистки.
"Подгрузить" берёт любую итерацию из списка: добавляет её к текущему каталогу (то, что есть сейчас,
не затирается) или заменяет каталог ею (текущий сперва сам уходит в хранилище новой итерацией).
Файлы итераций не удаляются. Настройки сервиса (выбранная модель, расписание) остаются на месте.
"""

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app import export
from app.db import Database

log = logging.getLogger(__name__)

SETTING_KEY = "storage"
# порядок важен: сперва прогоны и игры, потом таблицы, которые на них ссылаются
CATALOG_TABLES = (
    "runs", "games", "game_platforms", "review_summaries", "letsplays", "media", "processed", "failures",
)
MODES = ("merge", "replace")
# имя папки итерации: время очистки по поясу сервиса; суффикс, если две очистки пришлись на одну секунду
_ID = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?:_\d+)?$")
# состояние хранилища входит в каждый снимок мониторинга, поэтому manifest.json держим разобранным в памяти
_manifests: dict[Path, tuple[float, dict[str, Any]]] = {}


class StorageError(Exception):
    pass


class SnapshotNotFound(StorageError):
    pass


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix() + "/"
    except ValueError:
        return str(path)


def _stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Период и объём итерации: дни прогонов, число прогонов, игр, резюме и найденных летсплеев."""

    def total(sql: str) -> int:
        try:
            return int(conn.execute(sql).fetchone()[0])
        except sqlite3.OperationalError:
            # таблицы нет в снимке старой схемы
            return 0

    first, last, runs, days = conn.execute("SELECT MIN(day), MAX(day), COUNT(*), COUNT(DISTINCT day) FROM runs").fetchone()
    if first is None:
        # прогонов нет (данные собирали без истории): период по отметкам обработки
        first, last, days = conn.execute("SELECT MIN(day), MAX(day), COUNT(DISTINCT day) FROM processed").fetchone()
    return {
        "first_day": first,
        "last_day": last,
        "days": days or 0,
        "runs": runs,
        "games": total("SELECT COUNT(*) FROM games"),
        "summaries": total("SELECT COUNT(*) FROM review_summaries"),
        "letsplays": total("SELECT COUNT(*) FROM letsplays WHERE status = 'done'"),
    }


def _manifest(folder: Path) -> dict[str, Any] | None:
    """Описание итерации. У снимков старого формата период считается по самому снимку и дописывается в manifest."""
    db_file = folder / "games.db"
    if not db_file.is_file():
        return None
    path = folder / "manifest.json"
    stamp = path.stat().st_mtime if path.is_file() else 0.0
    cached = _manifests.get(folder)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        manifest = json.loads(path.read_text(encoding="utf-8")) if stamp else {}
    except (OSError, ValueError):
        manifest = {}
    if "first_day" not in manifest:
        try:
            conn = sqlite3.connect(db_file)
            try:
                manifest.update(_stats(conn))
            finally:
                conn.close()
        except sqlite3.Error:
            return None
        manifest.setdefault(
            "archived_at", datetime.fromtimestamp(db_file.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
        )
        try:
            path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            stamp = path.stat().st_mtime
        except OSError:
            pass
    _manifests[folder] = (stamp, manifest)
    return manifest


def _loaded(db: Database) -> list[str]:
    """Итерации, которые сейчас подгружены в каталог. Запись прежнего формата (путь к снимку) их не содержит."""
    raw = db.get_setting(SETTING_KEY)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    loaded = data.get("loaded") if isinstance(data, dict) else None
    return [str(item) for item in loaded] if isinstance(loaded, list) else []


def _set_loaded(db: Database, loaded: list[str]) -> None:
    db.set_setting(SETTING_KEY, json.dumps({"loaded": sorted(set(loaded))}, ensure_ascii=False))


def snapshots(storage_root: Path, loaded: list[str] | None = None) -> list[dict[str, Any]]:
    """Сохранённые итерации, новые первыми."""
    if not storage_root.is_dir():
        return []
    marks = set(loaded or [])
    folders = sorted((p for p in storage_root.iterdir() if p.is_dir() and _ID.match(p.name)), key=lambda p: p.name)
    items: list[dict[str, Any]] = []
    for folder in reversed(folders):
        manifest = _manifest(folder)
        if manifest is None:
            continue
        items.append({
            "id": folder.name,
            "archived_at": manifest.get("archived_at"),
            "first_day": manifest.get("first_day"),
            "last_day": manifest.get("last_day"),
            "days": manifest.get("days", 0),
            "runs": manifest.get("runs", 0),
            "games": manifest.get("games", 0),
            "summaries": manifest.get("summaries", 0),
            "letsplays": manifest.get("letsplays", 0),
            "loaded": folder.name in marks,
        })
    return items


def state(db: Database, storage_root: Path, project_root: Path) -> dict[str, Any]:
    """Что сейчас в хранилище: для блока "Данные каталога", окна подгрузки и экрана очищенного каталога."""
    items = snapshots(storage_root, _loaded(db))
    latest = items[0] if items else None
    return {
        "archived": bool(items),
        "path": _relative(storage_root, project_root),
        "count": len(items),
        "snapshots": items,
        # последняя итерация: для короткой строки состояния
        "games": latest["games"] if latest else 0,
        "archived_at": latest["archived_at"] if latest else None,
    }


def archive(db: Database, db_path: Path, storage_root: Path, tz: ZoneInfo) -> dict[str, Any]:
    """Сохраняет текущий каталог итерацией и очищает его. StorageError, если каталог пуст."""
    stats = _stats(db.conn)
    if stats["games"] == 0:
        raise StorageError("catalog is empty")
    now = datetime.now(timezone.utc)
    name = now.astimezone(tz).strftime("%Y-%m-%d_%H-%M-%S")
    folder, suffix = storage_root / name, 1
    # замена каталога сразу после очистки может прийтись на ту же секунду: папки не должны совпасть
    while folder.exists():
        suffix += 1
        folder = storage_root / f"{name}_{suffix}"
    folder.mkdir(parents=True)
    target = sqlite3.connect(folder / "games.db")
    try:
        # штатный способ SQLite: копия согласована даже при открытых соединениях сервиса
        db.conn.backup(target)
    finally:
        target.close()
    # CSV рядом со снимком: итерация уходит из интерфейса целиком, и её данные остаются читаемыми без сервиса
    try:
        export.write(folder / "games.db", folder / "csv", tz)
    except (OSError, sqlite3.Error) as e:
        log.warning("csv for %s is not written: %s", folder.name, e)
    manifest = {"archived_at": now.isoformat(timespec="seconds"), "source": str(db_path), **stats}
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    db.clear_catalog()
    _set_loaded(db, [])
    return {"id": folder.name, **manifest}


def restore(
    db: Database,
    db_path: Path,
    storage_root: Path,
    tz: ZoneInfo,
    snapshot: str | None = None,
    mode: str = "merge",
) -> dict[str, Any]:
    """Подгружает итерацию, без snapshot последнюю.

    merge: добавить к текущему каталогу, существующие игры и прогоны не затираются.
    replace: текущий каталог сперва сохраняется новой итерацией и очищается, затем подгружается выбранная.
    SnapshotNotFound, если такой итерации нет.
    """
    if mode not in MODES:
        raise StorageError(f"mode must be one of {MODES}")
    items = snapshots(storage_root)
    if not items:
        raise SnapshotNotFound("nothing is archived")
    chosen = snapshot or items[0]["id"]
    # id сверяется со списком, а не склеивается в путь как есть: так в него не подставить чужую папку
    if chosen not in {item["id"] for item in items}:
        raise SnapshotNotFound(f"no snapshot {chosen!r}")
    saved = None
    loaded = _loaded(db)
    if mode == "replace":
        if db.conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]:
            saved = archive(db, db_path, storage_root, tz)
        loaded = []
    restored = db.restore_catalog(storage_root / chosen / "games.db", CATALOG_TABLES)
    _set_loaded(db, [*loaded, chosen])
    return {"snapshot": chosen, "mode": mode, "restored": restored, "saved": saved["id"] if saved else None}
