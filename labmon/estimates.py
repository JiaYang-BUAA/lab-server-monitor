"""Bounded local log reading; conservative estimates from observed progress only."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import re
import stat
import threading
import time

MAX_TAIL = 256 * 1024
EXTENSIONS = {".sta", ".log", ".out", ".txt", ".msg"}
NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][+-]?\d+)?"


def iso(timestamp=None):
    return datetime.fromtimestamp(time.time() if timestamp is None else timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def finite_number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _has_reparse(path: Path):
    attrs = path.lstat()
    return stat.S_ISLNK(attrs.st_mode) or bool(getattr(attrs, "st_file_attributes", 0) & 0x400)


def _inside(path: Path, root: Path):
    try:
        return os.path.commonpath((str(path), str(root))) == str(root)
    except ValueError:
        return False


def safe_tail(log_path: str, allowed_roots: list[str]):
    """Never return raw content over HTTP; caller receives only parsing metadata."""
    if not isinstance(log_path, str) or not log_path or "\x00" in log_path:
        raise ValueError("日志路径无效")
    candidate = Path(log_path)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("日志必须使用允许目录下的绝对路径，不能包含 ..")
    if str(candidate).startswith(("\\\\", "//")):
        raise ValueError("不允许网络共享或设备路径")
    if candidate.suffix.lower() not in EXTENSIONS:
        raise ValueError("只允许 .sta/.log/.out/.txt/.msg 日志")
    if os.name == "nt" and ":" in str(candidate)[2:]:
        raise ValueError("不允许 NTFS 备用数据流")
    resolved = candidate.resolve(strict=True)
    roots = [Path(root).resolve(strict=True) for root in allowed_roots]
    if not any(_inside(resolved, root) for root in roots):
        raise ValueError("日志不在允许读取的目录内")
    # Reject junctions/symlinks even if their current destination is allowed.
    for path in (candidate, *candidate.parents):
        if _has_reparse(path):
            raise ValueError("日志路径不能经过符号链接或重解析点")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(candidate, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("只能读取常规日志文件")
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            import msvcrt
            get_final = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
            get_final.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
            get_final.restype = wintypes.DWORD
            buffer = ctypes.create_unicode_buffer(32768)
            count = get_final(msvcrt.get_osfhandle(fd), buffer, len(buffer), 0)
            if not count or count >= len(buffer):
                raise ValueError("无法确认已打开日志的实际路径")
            final_name = buffer.value.removeprefix("\\\\?\\")
            if final_name.startswith("UNC\\") or not any(_inside(Path(final_name).resolve(), root) for root in roots):
                raise ValueError("日志实际路径超出允许目录")
        check = candidate.stat(follow_symlinks=False)
        if (info.st_dev, info.st_ino) != (check.st_dev, check.st_ino):
            raise ValueError("日志路径在打开时发生变化")
        for path in (candidate, *candidate.parents):
            if _has_reparse(path):
                raise ValueError("日志路径在打开时发生变化")
        offset = max(0, info.st_size - MAX_TAIL)
        os.lseek(fd, offset, os.SEEK_SET)
        raw = os.read(fd, MAX_TAIL)
        if offset and b"\n" in raw:
            raw = raw.split(b"\n", 1)[1]
        text = raw.decode("utf-8", errors="replace")
        return text, {"size": info.st_size, "mtime_ns": info.st_mtime_ns,
                      "file_id": (info.st_dev, info.st_ino), "path": str(resolved)}
    finally:
        os.close(fd)


def parse_progress(text: str, kind: str, header_seen: bool = False):
    """Return latest units, explicit completion, unit description, header state."""
    if kind == "abaqus":
        finished = False
        progress = None
        for line in text.splitlines():
            if re.search(r"THE ANALYSIS HAS COMPLETED SUCCESSFULLY", line, re.I):
                finished = True
            values = line.split()
            # Abaqus/Standard .sta: STEP INC ATT SEVERE EQUIL TOTAL TOTAL-TIME STEP-TIME INC-TIME.
            if len(values) >= 9 and all(re.fullmatch(r"\d+", x) for x in values[:2]) and re.fullmatch(r"\d+[Uu]?", values[2]):
                if all(re.fullmatch(NUMBER, x) for x in values[3:9]):
                    progress = finite_number(values[6].replace("D", "E").replace("d", "e"))
                    finished = False
        return progress, finished, "累计分析时间", True
    if kind == "fluent":
        header_seen = header_seen or bool(re.search(r"(?im)^\s*iter\s+.*(?:continuity|residual|x-velocity|energy)", text))
        progress = None
        if header_seen:
            for line in text.splitlines():
                # Fluent prints residual tables with an integer iteration and at least two residuals.
                match = re.match(rf"^\s*(\d+)\s+({NUMBER})\s+({NUMBER})(?:\s|$)", line)
                if match:
                    progress = float(match.group(1))
        return progress, False, "迭代步", header_seen
    if kind == "comsol":
        progress = None
        # Restrict to explicitly labelled solver time; unlabelled table rows and
        # GUI percentages can refer to meshing/substeps rather than whole-job progress.
        for match in re.finditer(rf"(?im)^\s*(?:Time|t)\s*=\s*({NUMBER})(?:\s|$)", text):
            progress = finite_number(match.group(1).replace("D", "E").replace("d", "e"))
        return progress, False, "求解时间", True
    raise ValueError("不支持的日志类型")


class Estimator:
    def __init__(self, allowed_roots: list[str], clock=time.time):
        self.allowed_roots, self.clock = allowed_roots, clock
        self._states = {}
        self._lock = threading.RLock()

    def estimate(self, log_path: str, log_kind: str, total_units=None):
        if log_kind not in {"abaqus", "fluent", "comsol"}:
            raise ValueError("日志类型必须是 abaqus、fluent 或 comsol")
        total = finite_number(total_units)
        if total_units is not None and (isinstance(total_units, bool) or total is None or total <= 0):
            raise ValueError("总进度必须是有限正数")
        text, metadata = safe_tail(log_path, self.allowed_roots)
        now = self.clock()
        result = {"status": "unknown", "remaining_seconds": None, "estimated_end": None,
                  "progress_pct": None, "source": f"{log_kind}-log", "detail": "",
                  "updated_at": iso(now), "log_modified_at": iso(metadata["mtime_ns"] / 1e9)}
        key = (metadata["path"], log_kind, total)
        with self._lock:
            previous = self._states.get(key)
            progress, completed, unit, header = parse_progress(text, log_kind, bool(previous and previous["header"]))
            if completed:
                return {**result, "status": "completed", "remaining_seconds": 0,
                        "progress_pct": 100, "detail": "日志包含明确的分析成功结束标记"}
            if progress is None or progress < 0:
                return {**result, "detail": "日志尾部没有可可靠识别的单调进度；不根据 CPU 占用猜测结束时间"}
            reset = previous is not None and (metadata["file_id"] != previous["file_id"] or
                    metadata["size"] < previous["size"] or progress < previous["progress"] or now < previous["at"])
            if previous is None or reset:
                previous = {**metadata, "progress": progress, "at": now, "changed_at": now,
                            "header": header, "samples": deque(maxlen=8)}
                if len(self._states) >= 256:
                    self._states.pop(next(iter(self._states)))
                self._states[key] = previous
            if now > previous["at"] or not previous["samples"]:
                if progress > previous["progress"]:
                    previous["changed_at"] = now
                previous["samples"].append((now, progress))
            previous.update({**metadata, "progress": progress, "at": now, "header": header})
            if total is None:
                return {**result, "detail": f"已识别{unit} {progress:g}；尚未登记可靠的总目标，预计结束时间未知"}
            result["progress_pct"] = min(100, max(0, 100 * progress / total))
            if progress >= total:
                return {**result, "status": "completed", "remaining_seconds": 0,
                        "detail": "日志进度已达到登记目标；这不代表已验证求解成功"}
            if reset:
                return {**result, "status": "warming", "detail": "检测到日志重建、截断或进度回退，重新积累估算样本"}
            if now - previous["changed_at"] >= 30:
                return {**result, "status": "no-progress", "detail": "日志进度至少 30 秒未变化，暂停预计结束时间"}
            samples = list(previous["samples"])
            if len(samples) < 3 or samples[-1][0] - samples[0][0] < 10:
                return {**result, "status": "warming", "detail": "正在积累至少 3 次、跨度 10 秒的墙钟进度样本"}
            # Least-squares slope over recent wall-clock observations. This adapts
            # to changing iteration costs without equating CPU usage with progress.
            mean_t = sum(t - samples[0][0] for t, _ in samples) / len(samples)
            mean_p = sum(p for _, p in samples) / len(samples)
            denominator = sum((t - samples[0][0] - mean_t) ** 2 for t, _ in samples)
            rate = sum((t - samples[0][0] - mean_t) * (p - mean_p) for t, p in samples) / denominator if denominator else 0
            if rate <= 0:
                return {**result, "status": "warming", "detail": "尚未观测到足够的正向进度变化"}
            remaining = (total - progress) / rate
            if not math.isfinite(remaining) or remaining > 10 * 365 * 86400:
                return {**result, "detail": "当前进度速率不足以给出可靠的有限估算"}
            return {**result, "status": "estimating", "remaining_seconds": round(remaining),
                    "estimated_end": iso(now + remaining),
                    "detail": f"依据最近 {len(samples)} 次墙钟样本的{unit}速率估算；负载和后续步骤改变会影响结果"}
