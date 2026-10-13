# -*- coding: utf-8 -*-
"""Запуск Курымдык — только сайт: venv → pip → uvicorn → браузер."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / "venv"
if os.name == "nt":
    VENV_PY = VENV_DIR / "Scripts" / "python.exe"
else:
    VENV_PY = VENV_DIR / "bin" / "python"

HOST = "127.0.0.1"
PORT = 8000
MIN_VER = (3, 10)

CANDIDATES: list[list[str]] = [
    ["python"],
    ["python3"],
    ["py", "-3.13"],
    ["py", "-3.12"],
    ["py", "-3.11"],
    ["py", "-3.10"],
    ["py", "-3"],
]

def die(msg: str, code: int = 1) -> None:
    print("\nERROR:", msg)
    raise SystemExit(code)

def run(cmd: list[str], check: bool = True) -> int:
    print(">", " ".join(cmd))
    r = subprocess.run(cmd, cwd=str(ROOT))
    if check and r.returncode != 0:
        die(f"command failed ({r.returncode}): {' '.join(cmd)}", r.returncode)
    return r.returncode

def _probe(cmd: list[str]) -> tuple[tuple[int, int], str] | None:
    try:
        r = subprocess.run(
            cmd + ["-c", "import sys; print(sys.version_info.major); print(sys.version_info.minor); print(sys.executable)"],
            capture_output=True, text=True, timeout=20, cwd=str(ROOT),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    lines = [x.strip() for x in r.stdout.splitlines() if x.strip()]
    if len(lines) < 3:
        return None
    try:
        ver = (int(lines[0]), int(lines[1]))
    except ValueError:
        return None
    return ver, lines[2]

def find_best_python() -> tuple[tuple[int, int], str]:
    found: list[tuple[tuple[int, int], str]] = []
    seen: set[str] = set()
    cur = ((sys.version_info.major, sys.version_info.minor), sys.executable)
    found.append(cur)
    seen.add(Path(cur[1]).resolve().as_posix().lower())
    for cmd in CANDIDATES:
        hit = _probe(cmd)
        if not hit:
            continue
        ver, exe = hit
        key = Path(exe).resolve().as_posix().lower()
        if key in seen:
            continue
        seen.add(key)
        found.append((ver, exe))
        print(f"  found Python {ver[0]}.{ver[1]}  {exe}")
    ok = [(v, e) for v, e in found if v >= MIN_VER]
    if not ok:
        die(f"Need Python 3.10+. This process is {sys.version.split()[0]}. In PowerShell run: python launch.py")
    ok.sort(key=lambda x: x[0], reverse=True)
    return ok[0]

def venv_version(py: Path) -> tuple[int, int] | None:
    hit = _probe([str(py)])
    return hit[0] if hit else None

def cleanup_old_files() -> None:
    """Удаляет мусор при каждом запуске. Не трогает venv/backend/frontend/tools."""
    import datetime, time as _time
    log_path = ROOT / "cache" / "cleanup.log"
    def _log(msg: str):
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {msg}\n")
        except Exception:
            pass
        print(msg)
    now = _time.time()
    # .pytest_cache, .mypy_cache, .ruff_cache старше 3 дней
    for name in [".pytest_cache", ".mypy_cache", ".ruff_cache"]:
        try:
            p = ROOT / name
            if p.exists():
                age = (now - p.stat().st_mtime) / 86400
                if age > 3:
                    shutil.rmtree(p, ignore_errors=True)
                    _log(f"cleanup: removed {name} age {age:.1f}d")
                else:
                    _log(f"cleanup: keep {name} age {age:.1f}d")
        except Exception as e:
            _log(f"cleanup: {name} error {e}")
    # cache/server.log и cache/errors.log >5МБ — keep 500 строк
    for rel in ["cache/server.log", "cache/errors.log"]:
        try:
            fp = ROOT / rel
            if fp.is_file():
                sz = fp.stat().st_size
                if sz > 5 * 1024 * 1024:
                    with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                        lines = f.readlines()
                    last = lines[-500:] if len(lines) > 500 else lines
                    with open(fp, "w", encoding="utf-8") as f:
                        f.writelines(last)
                    _log(f"cleanup: truncated {rel} {sz} -> {fp.stat().st_size} keep 500 lines")
                else:
                    _log(f"cleanup: keep {rel} size {sz}")
        except Exception as e:
            _log(f"cleanup: {rel} error {e}")
    # *.bak старше 3 дней в корне
    try:
        for bak in ROOT.glob("*.bak"):
            if bak.is_file():
                age = (now - bak.stat().st_mtime) / 86400
                if age > 3:
                    bak.unlink()
                    _log(f"cleanup: removed {bak.name} age {age:.1f}d")
                else:
                    _log(f"cleanup: keep {bak.name} age {age:.1f}d")
        for pat in ["log.txt", "log_start*.txt"]:
            for f in ROOT.glob(pat):
                if f.is_file():
                    try:
                        f.unlink()
                        _log(f"cleanup: removed {f.name}")
                    except Exception as e:
                        _log(f"cleanup: {f.name} error {e}")
    except Exception as e:
        _log(f"cleanup: bak/log error {e}")
    # splash дубликаты
    for sp in [ROOT / "splash.html", ROOT / "frontend" / "splash.html"]:
        try:
            if sp.is_file():
                sp.unlink()
                _log(f"cleanup: removed {sp.relative_to(ROOT)}")
        except Exception as e:
            _log(f"cleanup: splash error {e}")
    _log("cleanup: protected venv/, backend/, frontend/app.js|styles.css|index.html, requirements.txt, tools/audiodl.exe — not touched")
    _log("cleanup: done")

def main() -> None:
    os.chdir(ROOT)
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    try:
        cleanup_old_files()
    except Exception as e:
        print(f"cleanup error: {e}")

    if not (ROOT / "backend" / "main.py").is_file():
        die("backend/main.py not found — run from the Kurymdyk folder")

    print(f"Курымдык root : {ROOT}")
    print(f"This exe  : {sys.executable}  ({sys.version.split()[0]})")
    print("Looking for Python 3.10+ ...")
    best_ver, best_exe = find_best_python()
    print(f"Selected  : Python {best_ver[0]}.{best_ver[1]}  {best_exe}")

    same = Path(best_exe).resolve() == Path(sys.executable).resolve()
    if not same or sys.version_info < MIN_VER:
        print(f"Re-launching with {best_exe}")
        raise SystemExit(subprocess.call([best_exe, str(Path(__file__).resolve())] + sys.argv[1:]))

    if sys.version_info < MIN_VER:
        die(f"Need Python 3.10+, got {sys.version}")

    # venv
    if VENV_PY.is_file():
        vv = venv_version(VENV_PY)
        if vv is None or vv < MIN_VER:
            print(f"Removing stale venv (Python {vv}) ...")
            shutil.rmtree(VENV_DIR, ignore_errors=True)

    if not VENV_PY.is_file():
        print("\n[1/3] Creating venv ...")
        run([sys.executable, "-m", "venv", str(VENV_DIR)])
        if not VENV_PY.is_file():
            die(f"venv python not created: {VENV_PY}")
    else:
        print(f"\n[1/3] venv OK  ({VENV_PY})")

    py = str(VENV_PY)
    print("\n[2/3] Installing dependencies ...")
    run([py, "-m", "ensurepip", "--upgrade"], check=False)
    run([py, "-m", "pip", "install", "--upgrade", "pip"])
    run([py, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")])

    probe = subprocess.run(
        [py, "-c", "import fastapi, uvicorn, mutagen, httpx, PIL; print('imports-ok')"],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    if probe.returncode != 0:
        print(probe.stdout)
        print(probe.stderr)
        die("packages missing inside venv after pip install")
    print(probe.stdout.strip())

    url = f"http://{HOST}:{PORT}"
    print(f"\n[3/3] Starting {url}")
    print(f"Uvicorn running on {url}")

    # открыть браузер по умолчанию через 1.5с в фоне
    def _open_browser():
        time.sleep(1.5)
        try:
            webbrowser.open(url)
            print(f"Browser opened {url}")
        except Exception as e:
            print(f"webbrowser.open failed: {e}")

    import threading
    threading.Thread(target=_open_browser, daemon=True).start()

    # запустить uvicorn — блокирующий вызов, лог в консоль
    cmd = [py, "-m", "uvicorn", "backend.main:app", "--host", HOST, "--port", str(PORT)]
    print(">", " ".join(cmd))
    # Uvicorn сам напишет "Uvicorn running on http://127.0.0.1:8000"
    raise SystemExit(subprocess.call(cmd, cwd=str(ROOT)))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
        raise SystemExit(0)
