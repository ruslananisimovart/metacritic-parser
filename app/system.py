"""Состояние машины для мониторинга: видеокарта, процессор, память, диск, сеть и статистика Whisper.

Без сторонних пакетов: nvidia-smi ставится вместе с драйвером NVIDIA, процессор, память и сеть на Windows
берутся из системного API через ctypes, на Linux из /proc (на других системах эти поля пустые), диск через shutil.
Снимок кэшируется на несколько секунд: статус пересчитывается на каждое событие мониторинга.
"""

import ctypes
import logging
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CACHE_SECONDS = 5.0
MB = 1024 * 1024


def _number(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None  # "[N/A]" у карт, которые не отдают часть показателей


def _gpu() -> dict[str, Any] | None:
    try:
        done = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = done.stdout.strip().splitlines()
    if done.returncode != 0 or not lines:
        return None
    parts = [p.strip() for p in lines[0].split(",")]
    if len(parts) < 6:
        return None
    return {
        "name": parts[0],
        "memory_used_mb": _number(parts[1]),
        "memory_total_mb": _number(parts[2]),
        "utilization": _number(parts[3]),
        "temperature": _number(parts[4]),
        "power_w": _number(parts[5]),
    }


if sys.platform == "win32":
    from ctypes import wintypes

    class _FileTime(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

        def value(self) -> int:
            return (self.high << 32) | self.low

    class _MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.DWORD), ("load", wintypes.DWORD), ("total_phys", ctypes.c_ulonglong),
            ("avail_phys", ctypes.c_ulonglong), ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong), ("avail_ext", ctypes.c_ulonglong),
        ]

    class _ProcessMemory(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("page_faults", wintypes.DWORD), ("peak_working_set", ctypes.c_size_t),
            ("working_set", ctypes.c_size_t), ("peak_paged", ctypes.c_size_t), ("paged", ctypes.c_size_t),
            ("peak_non_paged", ctypes.c_size_t), ("non_paged", ctypes.c_size_t), ("pagefile", ctypes.c_size_t),
            ("peak_pagefile", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # без явных типов ctypes передаёт псевдодескриптор процесса (-1) как int и падает на переполнении
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessMemory), wintypes.DWORD]
    _kernel32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
    _kernel32.GetSystemTimes.argtypes = [ctypes.POINTER(_FileTime)] * 3
    _kernel32.GetSystemTimes.restype = wintypes.BOOL
    _kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MemoryStatus)]
    _kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL

    def _cpu_times() -> tuple[int, int] | None:
        idle, kernel, user = _FileTime(), _FileTime(), _FileTime()
        if not _kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        # время ядра включает простой, поэтому занятость = 1 - простой / (ядро + пользователь)
        return idle.value(), kernel.value() + user.value()

    def _memory() -> tuple[float, float] | None:
        status = _MemoryStatus()
        status.length = ctypes.sizeof(_MemoryStatus)
        if not _kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return status.total_phys / MB, (status.total_phys - status.avail_phys) / MB

    def _process_mb() -> float | None:
        counters = _ProcessMemory()
        counters.cb = ctypes.sizeof(_ProcessMemory)
        if not _kernel32.K32GetProcessMemoryInfo(_kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return None
        return counters.working_set / MB

    # MIB_IFROW из iphlpapi: только DWORD и массивы байт, поэтому раскладка без выравнивания
    class _IfRow(ctypes.Structure):
        _fields_ = [
            ("name", wintypes.WCHAR * 256), ("index", wintypes.DWORD), ("type", wintypes.DWORD),
            ("mtu", wintypes.DWORD), ("speed", wintypes.DWORD), ("phys_len", wintypes.DWORD),
            ("phys", ctypes.c_ubyte * 8), ("admin", wintypes.DWORD), ("oper", wintypes.DWORD),
            ("last_change", wintypes.DWORD), ("in_octets", wintypes.DWORD), ("in_ucast", wintypes.DWORD),
            ("in_nucast", wintypes.DWORD), ("in_discards", wintypes.DWORD), ("in_errors", wintypes.DWORD),
            ("in_unknown", wintypes.DWORD), ("out_octets", wintypes.DWORD), ("out_ucast", wintypes.DWORD),
            ("out_nucast", wintypes.DWORD), ("out_discards", wintypes.DWORD), ("out_errors", wintypes.DWORD),
            ("out_qlen", wintypes.DWORD), ("descr_len", wintypes.DWORD), ("descr", ctypes.c_ubyte * 256),
        ]

    _iphlpapi = ctypes.WinDLL("iphlpapi")
    _iphlpapi.GetIfTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.ULONG), wintypes.BOOL]
    _iphlpapi.GetIfTable.restype = wintypes.DWORD
    _IF_OPERATIONAL = 5
    _IF_SKIP_TYPES = {24, 131}  # петля и туннели

    def _interfaces() -> dict[bytes, tuple[int, int, int]] | None:
        """Счётчики сетевых адаптеров: {MAC: (принято байт, отправлено байт, скорость бит/с)}.

        Драйверы-фильтры показывают один адаптер несколько раз с теми же счётчиками, поэтому по MAC
        остаётся одна запись.
        """
        size = wintypes.ULONG(0)
        _iphlpapi.GetIfTable(None, ctypes.byref(size), False)
        if not size.value:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if _iphlpapi.GetIfTable(buffer, ctypes.byref(size), False) != 0:
            return None
        count = ctypes.c_ulong.from_buffer(buffer).value
        rows = (_IfRow * count).from_buffer(buffer, ctypes.sizeof(wintypes.DWORD))
        out: dict[bytes, tuple[int, int, int]] = {}
        for row in rows:
            if row.oper != _IF_OPERATIONAL or row.type in _IF_SKIP_TYPES or not row.speed or not row.phys_len:
                continue
            mac = bytes(row.phys[: row.phys_len])
            if mac not in out or row.in_octets + row.out_octets > sum(out[mac][:2]):
                out[mac] = (row.in_octets, row.out_octets, row.speed)
        return out
elif sys.platform.startswith("linux"):
    # Linux (в том числе VPS): те же показатели из /proc и /sys, без сторонних пакетов
    _PROC = Path("/proc")

    def _cpu_times() -> tuple[int, int] | None:
        # первая строка /proc/stat: user nice system idle iowait irq softirq steal ...
        fields = [int(x) for x in (_PROC / "stat").read_text().splitlines()[0].split()[1:9]]
        return fields[3] + fields[4], sum(fields)

    def _meminfo() -> dict[str, int]:
        out = {}
        for line in (_PROC / "meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            out[key] = int(rest.split()[0]) * 1024  # значения в килобайтах
        return out

    def _memory() -> tuple[float, float] | None:
        info = _meminfo()
        total, available = info["MemTotal"], info.get("MemAvailable", info.get("MemFree", 0))
        return total / MB, (total - available) / MB

    def _process_mb() -> float | None:
        for line in (_PROC / "self" / "status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024 / MB
        return None

    def _interfaces() -> dict[bytes, tuple[int, int, int]] | None:
        out: dict[bytes, tuple[int, int, int]] = {}
        for line in (_PROC / "net" / "dev").read_text().splitlines()[2:]:
            name, _, rest = line.partition(":")
            name = name.strip()
            if name == "lo":
                continue
            parts = rest.split()
            try:
                # у виртуальных адаптеров VPS скорость не указана (-1): считаем гигабитом
                mbps = int((Path("/sys/class/net") / name / "speed").read_text().strip())
            except (OSError, ValueError):
                mbps = -1
            out[name.encode()] = (int(parts[0]) % 2**32, int(parts[8]) % 2**32, (mbps if mbps > 0 else 1000) * 1_000_000)
        return out
else:
    def _cpu_times() -> tuple[int, int] | None:
        return None

    def _memory() -> tuple[float, float] | None:
        return None

    def _process_mb() -> float | None:
        return None

    def _interfaces() -> dict[bytes, tuple[int, int, int]] | None:
        return None


class SystemMonitor:
    """Снимок видеокарты, процессора, памяти и диска с базой. Потокобезопасен, кэш CACHE_SECONDS."""

    def __init__(self, data_path: Path) -> None:
        self._disk_path = data_path if data_path.is_dir() else data_path.parent
        self._lock = threading.Lock()
        self._cached_at = float("-inf")
        self._cached: dict[str, Any] | None = None
        self._cpu_prev = _cpu_times()
        self._net_prev = (time.monotonic(), _safe(_interfaces))

    def _network(self) -> dict[str, Any] | None:
        """Загрузка сети по самому занятому адаптеру: скорость обмена к скорости подключения."""
        now, current = time.monotonic(), _interfaces()
        (then, previous), self._net_prev = self._net_prev, (now, current)
        elapsed = now - then
        if not current or not previous or elapsed <= 0:
            return None
        best: dict[str, Any] | None = None
        for mac, (rx, tx, speed) in current.items():
            if mac not in previous:
                continue
            # 32-битные счётчики переполняются примерно каждые 4 ГБ
            rx_rate = ((rx - previous[mac][0]) % 2**32) / elapsed
            tx_rate = ((tx - previous[mac][1]) % 2**32) / elapsed
            percent = 100 * max(rx_rate, tx_rate) * 8 / speed
            if best is None or percent > best["percent"]:
                best = {"percent": round(min(percent, 100.0), 1), "rx_bps": round(rx_rate), "tx_bps": round(tx_rate),
                        "speed_mbps": round(speed / 1_000_000)}
        return best

    def _cpu_percent(self) -> float | None:
        now = _cpu_times()
        prev, self._cpu_prev = self._cpu_prev, now
        if now is None or prev is None:
            return None
        idle, total = now[0] - prev[0], now[1] - prev[1]
        return round(max(0.0, min(100.0, 100.0 * (1 - idle / total))), 1) if total > 0 else None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._cached is not None and time.monotonic() - self._cached_at < CACHE_SECONDS:
                return self._cached
            memory = _safe(_memory)
            process = _safe(_process_mb)
            self._cached = {
                "gpu": _safe(_gpu),
                "cpu_percent": _safe(self._cpu_percent),
                "memory": {"total_mb": round(memory[0]), "used_mb": round(memory[1]),
                           "percent": round(100 * memory[1] / memory[0], 1)} if memory else None,
                "process_mb": round(process) if process else None,
                "disk": _safe(self._disk),
                "network": _safe(self._network),
            }
            self._cached_at = time.monotonic()
            return self._cached

    def _disk(self) -> dict[str, float]:
        disk = shutil.disk_usage(self._disk_path)
        return {"total_gb": round(disk.total / 1024**3, 1), "free_gb": round(disk.free / 1024**3, 1),
                "percent": round(100 * disk.used / disk.total, 1)}


def _safe(probe: Any) -> Any:
    """Сбой одного замера даёт пустое поле, а не ошибку всего статуса мониторинга."""
    try:
        return probe()
    except Exception:
        log.warning("system probe %s failed", getattr(probe, "__name__", probe), exc_info=True)
        return None


class WhisperMonitor:
    """Обёртка над Transcriber с тем же интерфейсом: считает расшифровки для мониторинга.

    Модуль расшифровки не меняется: всё, кроме transcribe, уходит в исходный объект.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._busy_since: float | None = None
        self.runs = 0
        self.failures = 0
        self.last: dict[str, Any] | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def transcribe(self, path: Path, **kwargs: Any) -> Any:
        self._busy_since = time.time()
        try:
            result = await self._inner.transcribe(path, **kwargs)
        except BaseException:
            self.failures += 1
            raise
        finally:
            self._busy_since = None
        self.runs += 1
        self.last = {
            "audio_seconds": round(result.audio_seconds, 1),
            "took_seconds": round(result.took_seconds, 1),
            "language": result.language,
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        return result

    def snapshot(self) -> dict[str, Any]:
        busy = self._busy_since
        return {
            "busy": busy is not None,
            "busy_seconds": round(time.time() - busy) if busy is not None else None,
            # модель грузится при расшифровке и выгружается после простоя; без неё видеопамять не занята
            "loaded": bool(getattr(self._inner, "loaded", False)),
            "runs": self.runs,
            "failures": self.failures,
            "last": self.last,
        }
