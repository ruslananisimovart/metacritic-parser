"""Свежие cookies YouTube из собственного профиля Chrome.

YouTube перевыпускает cookies входа при каждом визите из браузера, поэтому файл, выгруженный вручную,
живёт до следующего захода аккаунта в браузер. Здесь у сервиса свой профиль Chrome (YOUTUBE_PROFILE),
в который один раз входят в аккаунт (open_login_window, пункт лаунчера). Дальше сервис сам запускает
headless Chrome с этим профилем, открывает youtube.com и через протокол DevTools забирает текущие
cookies в файл Netscape для yt-dlp. Chrome принимает ротированные cookies в профиль, так что каждый
экспорт актуален, а аккаунт больше нигде открывать нельзя: любой вход снаружи снова сломает сессию.

Вход считается живым, если среди cookies youtube.com есть LOGIN_INFO или SAPISID.
"""

import json
import logging
import os
import shutil
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

LOGIN_URL = "https://accounts.google.com/ServiceLogin?continue=https://www.youtube.com/"
YOUTUBE_URL = "https://www.youtube.com/"
# домены, cookies которых нужны yt-dlp
DOMAINS = ("youtube.com", "google.com")
LOGIN_COOKIES = ("LOGIN_INFO", "SAPISID", "__Secure-1PSID")
CHROME_CANDIDATES = (
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
)
START_TIMEOUT = 25.0
LOAD_TIMEOUT = 25.0
# после загрузки страницы даём YouTube время перевыпустить cookies
SETTLE_SECONDS = 2.0


class CookieExportError(Exception):
    pass


class NotLoggedIn(CookieExportError):
    """В профиле нет входа в аккаунт: нужен ручной вход через окно Chrome (лаунчер)."""


@dataclass(frozen=True)
class CookieReport:
    count: int
    logged_in: bool
    path: Path


def find_chrome() -> Path | None:
    for candidate in CHROME_CANDIDATES:
        if candidate.is_file():
            return candidate
    found = shutil.which("chrome") or shutil.which("google-chrome") or shutil.which("chromium")
    return Path(found) if found else None


def _root_flags() -> list[str]:
    """Chrome не запускается от root без отключения песочницы, а на VPS сервис часто работает от root."""
    return ["--no-sandbox"] if hasattr(os, "geteuid") and os.geteuid() == 0 else []


def open_login_window(profile: Path) -> None:
    """Открывает обычное окно Chrome с профилем сервиса на странице входа Google. Окно живёт само."""
    chrome = find_chrome()
    if chrome is None:
        raise CookieExportError("Chrome не найден")
    profile = profile.resolve()
    profile.mkdir(parents=True, exist_ok=True)
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [str(chrome), f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
         "--window-size=1100,800", *_root_flags(), LOGIN_URL],
        creationflags=flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
    )


def _netscape_line(cookie: dict[str, Any]) -> str:
    domain = str(cookie.get("domain", ""))
    expires = cookie.get("expires", -1)
    stamp = int(expires) if isinstance(expires, (int, float)) and expires > 0 else 0
    return "\t".join((
        domain,
        "TRUE" if domain.startswith(".") else "FALSE",
        str(cookie.get("path", "/")),
        "TRUE" if cookie.get("secure") else "FALSE",
        str(stamp),
        str(cookie.get("name", "")),
        str(cookie.get("value", "")),
    ))


def write_netscape(cookies: list[dict[str, Any]], path: Path) -> None:
    # заголовок латиницей: файл читают yt-dlp и стандартный MozillaCookieJar, кодировка у них не задаётся
    lines = ["# Netscape HTTP Cookie File", "# exported by the service from its Chrome profile (app/ytcookies.py)", ""]
    lines.extend(_netscape_line(c) for c in cookies)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _wanted(cookie: dict[str, Any]) -> bool:
    domain = str(cookie.get("domain", "")).lstrip(".")
    return any(domain == d or domain.endswith("." + d) for d in DOMAINS)


def logged_in(cookies: list[dict[str, Any]]) -> bool:
    return any(c.get("name") in LOGIN_COOKIES and "youtube.com" in str(c.get("domain", "")) for c in cookies)


class _Session:
    """Одно соединение DevTools со страницей headless Chrome."""

    def __init__(self, ws_url: str) -> None:
        import websocket  # websocket-client, только здесь

        self._ws = websocket.create_connection(ws_url, timeout=LOAD_TIMEOUT, suppress_origin=True)
        self._id = 0

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        self._id += 1
        self._ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            message = json.loads(self._ws.recv())
            if message.get("id") == self._id:
                if "error" in message:
                    raise CookieExportError(f"{method}: {message['error']}")
                return message.get("result", {})

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass


def _wait_port(profile: Path, deadline: float) -> int:
    marker = profile / "DevToolsActivePort"
    while time.monotonic() < deadline:
        try:
            first = marker.read_text(encoding="utf-8").splitlines()[0].strip()
            if first.isdigit():
                return int(first)
        except (OSError, IndexError):
            pass
        time.sleep(0.2)
    raise CookieExportError("Chrome не поднял порт DevTools (профиль занят другим окном Chrome?)")


def _page_target(port: int, deadline: float) -> str:
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as response:
                targets = json.loads(response.read())
            for target in targets:
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return str(target["webSocketDebuggerUrl"])
        except (OSError, ValueError):
            pass
        time.sleep(0.2)
    raise CookieExportError("страница Chrome не появилась")


def export_cookies(profile: Path, out: Path) -> CookieReport:
    """Запускает headless Chrome с профилем, открывает YouTube и пишет cookies в out.

    Блокирует поток на 5-20 секунд: звать через asyncio.to_thread. NotLoggedIn, если входа в профиле нет
    (файл при этом всё равно записан, без cookies аккаунта).
    """
    chrome = find_chrome()
    if chrome is None:
        raise CookieExportError("Chrome не найден: cookies YouTube обновить нечем")
    # Chrome считает относительный путь профиля от своей папки, поэтому только абсолютный
    profile = profile.resolve()
    profile.mkdir(parents=True, exist_ok=True)
    marker = profile / "DevToolsActivePort"
    try:
        marker.unlink()
    except OSError:
        pass
    proc = subprocess.Popen(
        [str(chrome), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--window-size=1280,900",
         f"--user-data-dir={profile}", "--remote-debugging-port=0", "--remote-allow-origins=*",
         "--no-first-run", "--no-default-browser-check", "--disable-background-networking", *_root_flags(), YOUTUBE_URL],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    session: _Session | None = None
    try:
        deadline = time.monotonic() + START_TIMEOUT
        port = _wait_port(profile, deadline)
        session = _Session(_page_target(port, deadline))
        session.call("Page.enable")
        deadline = time.monotonic() + LOAD_TIMEOUT
        while time.monotonic() < deadline:
            state = session.call("Runtime.evaluate", expression="document.readyState", returnByValue=True)
            if state.get("result", {}).get("value") == "complete":
                break
            time.sleep(0.3)
        time.sleep(SETTLE_SECONDS)
        raw = session.call("Network.getAllCookies").get("cookies", [])
    finally:
        if session is not None:
            session.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    cookies = [c for c in raw if isinstance(c, dict) and _wanted(c)]
    out.parent.mkdir(parents=True, exist_ok=True)
    write_netscape(cookies, out)
    report = CookieReport(count=len(cookies), logged_in=logged_in(cookies), path=out)
    if not report.logged_in:
        raise NotLoggedIn(f"в профиле {profile} нет входа в YouTube: {len(cookies)} cookies без аккаунта")
    log.info("youtube cookies: %d written to %s", report.count, out)
    return report
