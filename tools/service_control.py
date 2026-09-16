"""Управление сервисом одной кнопкой (Metacritic_Parser.bat на Windows, metacritic_parser.sh на Linux):
1 запускает, 2 перезапускает, 3 выключает целиком.

Запуск: скрытый фоновый процесс без окна, по умолчанию слушает только этот компьютер (127.0.0.1) и продолжает
работать после закрытия окна или выхода из SSH. На сервере адрес задаётся SERVICE_HOST=0.0.0.0.
Службы, задания планировщика и автозапуск не создаются.

Перезапуск нужен после правок кода: сервис останавливается целиком и поднимается заново, то есть
новый процесс читает изменившиеся файлы. Это полная остановка и полный запуск, а не перезагрузка на лету.

Остановка штатная: по защищённой ручке сервис доделывает текущий шаг, помечает прогон прерванным,
выгружает модель, которую сам загрузил в LM Studio, и закрывает базу. Если за STOP_TIMEOUT секунд
процесс не вышел, он и все его дочерние процессы (yt-dlp, deno, ffmpeg) снимаются принудительно.
После остановки не остаётся ни процессов сервиса, ни его инструментов, ни файла состояния, порт свободен.

Меню в окне устроено как лаунчер AI Studio: перед каждым показом экран очищается, вверху заголовок между
двойными линиями, ниже состояние сервиса (зелёным работает, серым остановлен), затем пункты. Сообщения
действия идут под строкой выбора; его итог остаётся серой строкой при следующем показе, ошибка выводится
красным и ждёт Enter. Консольные команды (start, restart, stop, status) печатают то же без экрана меню.
"""

import ctypes
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "service.state.json"
LOG = ROOT / "logs" / "uvicorn.log"
# адрес, который слушает сервис; сам лаунчер всегда обращается к нему локально
BIND = os.environ.get("SERVICE_HOST", "127.0.0.1").strip() or "127.0.0.1"
HOST = "127.0.0.1"
PORT = int(os.environ.get("SERVICE_PORT", "8000"))
URL = f"http://{HOST}:{PORT}/"
START_TIMEOUT = 90
STOP_TIMEOUT = 30
WINDOWS = sys.platform == "win32"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if WINDOWS else 0
# PowerShell пишет в кодировке консоли (cp866): командные строки процессов с кириллицей в пути
# ломали чтение UTF-8, и список процессов молча оказывался пустым
PS_UTF8 = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
PORT_HINT = f"set SERVICE_PORT={PORT + 10}" if WINDOWS else f"SERVICE_PORT={PORT + 10} ./metacritic_parser.sh"
# инструменты, которые сервис запускает сам: после остановки их быть не должно
TOOL_NAMES = ("yt-dlp.exe", "ffmpeg.exe", "ffprobe.exe", "deno.exe") if WINDOWS else ("yt-dlp", "ffmpeg", "ffprobe", "deno")


# ---- консоль: цвета, очистка экрана, отступы

CYAN, GREEN, GRAY, RED, RESET = "\x1b[96m", "\x1b[92m", "\x1b[90m", "\x1b[91m", "\x1b[0m"
ANSI = False  # цвета и очистка экрана; включается в main(), когда вывод идёт в консоль, а не в файл
INDENT = ""  # отступ сообщений действия: в меню они стоят под строкой выбора, в консольной команде у края


def paint(text: str, color: str | None) -> str:
    return f"{color}{text}{RESET}" if color and ANSI else text


def show(text: str = "", color: str | None = None) -> None:
    """Строка экрана как есть."""
    print(paint(text, color), flush=True)


def say(text: str = "", color: str | None = None) -> None:
    """Сообщение действия: каждая строка с отступом текущего режима."""
    for line in text.splitlines() or [""]:
        show(INDENT + line if line else "", color)


def clear() -> None:
    if not ANSI:
        return
    if WINDOWS:
        os.system("cls")
    else:
        print("\x1b[2J\x1b[H", end="", flush=True)


# ---- процессы (без сторонних пакетов: ctypes на Windows)

if WINDOWS:
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    _kernel32.GetStdHandle.restype = wintypes.HANDLE
    _kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetConsoleMode.restype = wintypes.BOOL
    _kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.SetConsoleMode.restype = wintypes.BOOL
    _QUERY = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
    _STILL_ACTIVE = 259
    _STD_OUTPUT = 0xFFFFFFF5  # STD_OUTPUT_HANDLE, то есть -11 как DWORD
    _VT_OUTPUT = 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING


def _image(pid: int) -> str | None:
    """Путь к exe живого процесса или None, если процесса нет."""
    if not WINDOWS:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            pass
        try:
            # зомби (процесс вышел, родитель не забрал код) живым не считаем
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            if fields and fields[0] == "Z":
                return None
            return os.readlink(f"/proc/{pid}/exe")
        except OSError:
            # без /proc (macOS) имени не узнать: процесс жив, считаем его python из файла состояния
            return sys.executable
    handle = _kernel32.OpenProcess(_QUERY, False, pid)
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != _STILL_ACTIVE:
            return None
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return buffer.value
    finally:
        _kernel32.CloseHandle(handle)


def alive(pid: int | None) -> bool:
    # номер процесса могли отдать другой программе: живым считаем только python
    image = _image(pid) if pid else None
    return bool(image) and Path(image).name.lower().startswith("python")


def _process_table() -> dict[int, int]:
    """pid -> pid родителя по всем процессам (для поиска потомков сервиса)."""
    if not WINDOWS:
        try:
            out = subprocess.run(["ps", "-eo", "pid=,ppid="], capture_output=True, text=True, timeout=15).stdout
            return {int(pid): int(ppid) for pid, ppid in (line.split() for line in out.splitlines() if line.strip())}
        except (OSError, subprocess.SubprocessError, ValueError):
            return {}
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             PS_UTF8 + "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId | ConvertTo-Json -Compress"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, creationflags=NO_WINDOW,
        ).stdout
        return {row["ProcessId"]: row["ParentProcessId"] for row in json.loads(out or "[]")}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


def descendants(pid: int) -> list[int]:
    table = _process_table()
    found, frontier = [], [pid]
    while frontier:
        parent = frontier.pop()
        children = [child for child, owner in table.items() if owner == parent and child not in found]
        found += children
        frontier += children
    return found


def leftover_tools() -> list[tuple[int, str]]:
    """Инструменты сервиса (yt-dlp, ffmpeg, deno), запущенные из папки проекта и пережившие его.

    Отбор по пути проекта в командной строке: чужие ffmpeg и deno пользователя не трогаем.
    """
    root = str(ROOT).lower()
    if not WINDOWS:
        try:
            out = subprocess.run(["ps", "-eo", "pid=,comm=,args="], capture_output=True, text=True, timeout=15).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        found = []
        for line in out.splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[1] in TOOL_NAMES and root in parts[2].lower():
                found.append((int(parts[0]), parts[1]))
        return found
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             PS_UTF8 + "Get-CimInstance Win32_Process | Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, creationflags=NO_WINDOW,
        ).stdout
        rows = json.loads(out or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    found = []
    for row in rows if isinstance(rows, list) else [rows]:
        name = (row.get("Name") or "").lower()
        line = (row.get("CommandLine") or "").lower()
        if name in TOOL_NAMES and root in line:
            found.append((row["ProcessId"], row.get("Name") or name))
    return found


def kill_tools() -> int:
    tools = leftover_tools()
    for pid, _ in tools:
        kill_tree(pid)
    return len(tools)


def kill_tree(pid: int) -> None:
    if not WINDOWS:
        # сначала потомки, потом сам процесс: иначе осиротевшие дети уйдут к init и останутся
        for victim in [*reversed(descendants(pid)), pid]:
            try:
                os.kill(victim, signal.SIGKILL)
            except OSError:
                pass
        return
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, creationflags=NO_WINDOW)


def listener_pid() -> int | None:
    """Процесс, который слушает порт сервиса (на случай, если файла состояния нет)."""
    if not WINDOWS:
        # ss есть почти везде в Linux, lsof на macOS и части серверов
        for command in (["ss", "-ltnpH", f"sport = :{PORT}"], ["lsof", "-t", f"-iTCP:{PORT}", "-sTCP:LISTEN"]):
            try:
                out = subprocess.run(command, capture_output=True, text=True, timeout=15).stdout
            except (OSError, subprocess.SubprocessError):
                continue
            for token in out.replace(",", " ").split():
                value = token[4:] if token.startswith("pid=") else token
                if value.isdigit() and (command[0] == "lsof" or token.startswith("pid=")):
                    return int(value)
        return None
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=15,
                             creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3] == "LISTENING" and parts[1].endswith(f":{PORT}"):
            return int(parts[4])
    return None


def port_busy() -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((HOST, PORT)) == 0


# ---- состояние и HTTP

def read_state() -> dict | None:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def http(path: str, *, method: str = "GET", token: str | None = None, timeout: float = 3.0) -> dict | None:
    request = urllib.request.Request(URL.rstrip("/") + path, method=method)
    if token:
        request.add_header("X-Control-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def running() -> dict | None:
    state = read_state()
    return state if state and alive(state.get("pid")) else None


def service_pid() -> int | None:
    """Процесс работающего сервиса: из файла состояния, иначе тот, кто слушает порт (запуск из консоли)."""
    state = running()
    if state:
        return state["pid"]
    orphan = listener_pid()
    return orphan if orphan and alive(orphan) else None


def details() -> list[str]:
    """Строки о работающем сервисе: каталог и прогон, модель, расписание. Для меню и команды status."""
    health = http("/api/health") or {}
    status = http("/api/status") or {}
    lines = []
    if "games" in health:
        run = ", идёт прогон" if health.get("run_in_progress") else ""
        lines.append(f"Игр в каталоге: {health['games']}{run}")
    llm = status.get("llm") or {}
    if llm:
        ready = "готова" if not llm.get("reason") else f"не готова: {llm['reason']}"
        lines.append(f"Модель: {llm.get('title')}, {llm.get('model')} ({ready})")
    scheduler = status.get("scheduler") or {}
    if scheduler.get("enabled"):
        rule = scheduler.get("rule") or "по расписанию"
        lines.append(f"Расписание: {rule}" + ("" if scheduler.get("active") else " (на паузе, запуск только вручную)"))
    return lines


def tail(path: Path, lines: int = 15) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


# ---- команды: каждая возвращает успех и заметку об итоге (None, когда итог виден по состоянию сервиса)

def start() -> tuple[bool, str | None]:
    state = running()
    if state:
        return True, f"Сервис уже работает (процесс {state['pid']})."
    STATE.unlink(missing_ok=True)  # остался после аварийного выключения ПК
    if port_busy():
        return False, f"Порт {PORT} занят другой программой. Освободите его или задайте другой порт: {PORT_HINT}"

    token = secrets.token_hex(16)
    env = {**os.environ, "SERVICE_CONTROL_TOKEN": token, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", BIND, "--port", str(PORT),
               "--timeout-graceful-shutdown", "5", "--no-access-log"]
    # без окна и в своей группе процессов (на Linux в своей сессии): закрытие окна или выход из SSH
    # сервис не останавливает
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if WINDOWS else 0
    with open(LOG, "w", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=subprocess.STDOUT, creationflags=flags, close_fds=True,
                                   start_new_session=not WINDOWS)
    STATE.write_text(json.dumps({
        "pid": process.pid, "port": PORT, "token": token,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    say(f"Запускаю сервис на порту {PORT}…", GRAY)
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            STATE.unlink(missing_ok=True)
            say("Сервис не запустился. Последние строки лога logs\\uvicorn.log:", RED)
            say(tail(LOG), GRAY)
            return False, None
        if http("/api/health"):
            if not os.environ.get("SERVICE_NO_BROWSER"):
                webbrowser.open(URL)
            return True, None
        time.sleep(1)
    say(f"Сервис не ответил за {START_TIMEOUT} с, останавливаю. Лог: logs\\uvicorn.log", RED)
    ok, note = stop(quiet=True)
    if not ok and note:
        say(note, RED)
    return False, None


def stop(quiet: bool = False) -> tuple[bool, str | None]:
    """Остановка целиком. С quiet сообщения о ходе и удачном итоге не нужны (вызов из start)."""
    state = read_state()
    pid = state.get("pid") if state else None
    token = state.get("token") if state else None
    if not alive(pid):
        # файла нет или процесс уже завершился; сервис могли запустить и вручную из консоли
        orphan = listener_pid()
        if orphan and alive(orphan):
            say(f"Нашёл сервис без файла состояния (процесс {orphan}), останавливаю принудительно.", GRAY)
            pid, token = orphan, None
        else:
            STATE.unlink(missing_ok=True)
            tools = kill_tools()
            if quiet:
                return True, None
            return True, "Сервис не запущен." if not tools else f"Сервиса нет, сняты его инструменты: {tools}."

    children = descendants(pid)
    if token and not quiet:
        say("Останавливаю сервис: текущий шаг доделается, прогон будет помечен прерванным…", GRAY)
    graceful = bool(token) and http("/api/control/shutdown", method="POST", token=token, timeout=10) is not None
    if token and not graceful and not quiet:
        say("Сервис не принял команду остановки, снимаю процесс принудительно.", GRAY)
    deadline = time.monotonic() + (STOP_TIMEOUT if graceful else 0)
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.5)
    forced = alive(pid)
    if forced:
        kill_tree(pid)
        time.sleep(1)
    # дочерние процессы (yt-dlp, deno, ffmpeg) могли пережить родителя: снимаем тех, кто остался
    leftovers = [child for child in children if _image(child)]
    for child in leftovers:
        kill_tree(child)
    # и те же инструменты, потерявшие родителя ещё раньше: после остановки не должно остаться ничего
    tools = kill_tools()
    STATE.unlink(missing_ok=True)

    if alive(pid) or port_busy():
        return False, f"Не удалось остановить полностью: процесс {pid} или порт {PORT} ещё заняты. Лог: logs\\service.log"
    if quiet:
        return True, None
    how = "принудительно" if forced else "штатно"
    count = len(leftovers) + tools
    extra = f", сняты вспомогательные процессы: {count}" if count else ""
    return True, f"Сервис остановлен {how}{extra}. Процессов сервиса не осталось, порт {PORT} свободен."


def restart() -> tuple[bool, str | None]:
    """Полная перезагрузка: остановить всё и поднять заново, чтобы новый процесс прочитал изменённый код."""
    if running() or listener_pid():
        ok, note = stop()
        if not ok:
            if note:
                say(note, RED)
            return False, "Перезапуск отменён: сервис не остановился полностью."
        if note:
            say(note, GRAY)
    else:
        say("Сервис не был запущен, просто поднимаю его.", GRAY)
    # порт освобождается не мгновенно: даём Windows закрыть сокет
    deadline = time.monotonic() + 10
    while port_busy() and time.monotonic() < deadline:
        time.sleep(0.5)
    return start()


def youtube_profile() -> Path:
    """Папка профиля Chrome для YouTube из .env (YOUTUBE_PROFILE), по умолчанию data/youtube-profile."""
    value = "data/youtube-profile"
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            key, sep, raw = line.partition("=")
            if sep and key.strip() == "YOUTUBE_PROFILE" and raw.strip().strip('"'):
                value = raw.strip().strip('"')
    except OSError:
        pass
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def youtube_login() -> tuple[bool, str | None]:
    """Окно Chrome с профилем сервиса: пользователь входит в аккаунт YouTube, закрывает окно,
    затем сервис (если запущен) сразу перечитывает cookies из профиля."""
    sys.path.insert(0, str(ROOT))
    from app import ytcookies

    profile = youtube_profile()
    try:
        ytcookies.open_login_window(profile)
    except ytcookies.CookieExportError as e:
        return False, f"Не удалось открыть Chrome: {e}"
    say("Открыл окно Chrome с профилем сервиса. Войдите в аккаунт YouTube (лучше отдельный,", GRAY)
    say("который больше нигде не открывается), дождитесь главной страницы YouTube и закройте окно.", GRAY)
    say("Пока окно открыто, сервис не может читать профиль.", GRAY)
    show()
    try:
        input(f"{INDENT}Enter — когда вошли и закрыли окно: ")
    except (EOFError, KeyboardInterrupt):
        return True, None
    state = read_state()
    token = state.get("token") if state else None
    if not service_pid() or not token:
        return True, "Вход сохранён в профиле. Сервис подхватит его при следующем запуске."
    result = http("/api/control/youtube-cookies", method="POST", token=token, timeout=60)
    if not result:
        return False, "Сервис не ответил на обновление cookies: проверьте, что окно Chrome закрыто, и повторите пункт."
    if result.get("logged_in"):
        return True, "Вход в YouTube подтверждён, cookies обновлены. Летсплеи пойдут без отказов."
    error = (result.get("cookies") or {}).get("error") or "в профиле нет входа"
    return False, f"Входа в профиле не видно: {error}. Повторите пункт и дождитесь главной страницы YouTube."


def status() -> None:
    pid = service_pid()
    if not pid:
        say("Сервис остановлен.")
        return
    say(f"Сервис работает: {URL} (процесс {pid}).")
    for line in details():
        say(line)


# ---- меню

RULE = "   " + "═" * 38


def render(note: str | None) -> None:
    """Экран меню: заголовок, состояние сервиса, заметка о последнем действии, пункты."""
    clear()
    show()
    show(RULE)
    show("      METACRITIC PARSER", CYAN)
    show(RULE)
    show()
    pid = service_pid()
    if pid:
        show("      Сервис работает", GREEN)
        show(f"      {URL}", GREEN)
        for line in details():
            show(f"      {line}", GRAY)
    else:
        show("      Сервис остановлен", GRAY)
    if note:
        show()
        show(f"      {note}", GRAY)
    show()
    show("      1 — Запустить")
    show("      2 — Перезапустить")
    show("      3 — Остановить")
    show("      4 — Войти в YouTube (профиль Chrome для летсплеев)")
    show("      0 — Выход" + (" (сервис продолжит работать)" if pid else ""))
    show()


def menu() -> None:
    global INDENT
    INDENT = "   "
    actions = {"1": start, "2": restart, "3": stop, "4": youtube_login}
    note: str | None = None
    while True:
        render(note)
        note = None
        try:
            # только буквы и цифры: консоль или перенаправленный ввод может добавить BOM и \r
            choice = "".join(ch for ch in input("      Выбор: ") if ch.isalnum())
        except (EOFError, KeyboardInterrupt):
            show()
            return
        if choice == "0":
            return
        action = actions.get(choice)
        if action is None:
            continue
        show()
        ok, note = action()
        if ok:
            continue
        if note:
            say(note, RED)
        show()
        try:
            input(f"{INDENT}Enter — вернуться в меню: ")
        except (EOFError, KeyboardInterrupt):
            return
        note = None


# ---- запуск

def _utf8_console() -> None:
    """Консоль Windows по умолчанию в cp866: русский текст меню иначе выводится мусором.

    Кодовую страницу переключаем отсюда, а не из Metacritic_Parser.bat: батник остаётся чистым ASCII,
    и на него не влияет разбор cmd.exe по смещению байт.
    """
    if not WINDOWS:
        return
    try:
        _kernel32.SetConsoleOutputCP(65001)
        _kernel32.SetConsoleCP(65001)
    except OSError:
        pass


def _enable_ansi() -> bool:
    """Цвета: терминалу Windows они доступны сразу, классической консоли нужен режим VT.

    Если вывод перенаправлен в файл или канал, консоли нет: текст идёт без цветов и без очистки экрана.
    """
    if not WINDOWS:
        return sys.stdout.isatty()
    handle = _kernel32.GetStdHandle(_STD_OUTPUT)
    mode = wintypes.DWORD()
    if not _kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return False
    return bool(_kernel32.SetConsoleMode(handle, mode.value | _VT_OUTPUT))


def main() -> int:
    global ANSI
    _utf8_console()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ANSI = _enable_ansi()
    command = sys.argv[1] if len(sys.argv) > 1 else "menu"
    if command in ("start", "restart", "stop"):
        ok, note = {"start": start, "restart": restart, "stop": stop}[command]()
        if note:
            say(note, None if ok else RED)
        if ok and command != "stop":
            status()
        return 0 if ok else 1
    if command == "status":
        status()
        return 0
    menu()
    return 0


if __name__ == "__main__":
    sys.exit(main())
