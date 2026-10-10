# -*- coding: utf-8 -*-
"""Запуск Курымдык: найти Python 3.10+, venv, зависимости, uvicorn."""

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

# Сначала "python" — на этой машине это 3.13; "py -3" часто залипает на 3.9.
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
    """Вернуть ((major, minor), executable) или None."""
    try:
        r = subprocess.run(
            cmd
            + [
                "-c",
                "import sys; print(sys.version_info.major); print(sys.version_info.minor); print(sys.executable)",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            cwd=str(ROOT),
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
    # текущий интерпретатор тоже кандидат
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
        die(
            "Need Python 3.10+. "
            f"This process is {sys.version.split()[0]}. "
            "In PowerShell run:  python launch.py"
        )
    ok.sort(key=lambda x: x[0], reverse=True)
    return ok[0]


def venv_version(py: Path) -> tuple[int, int] | None:
    hit = _probe([str(py)])
    return hit[0] if hit else None


def _server_log(msg: str) -> None:
    try:
        log_dir = ROOT / "cache"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "server.log", "a", encoding="utf-8") as f:
            f.write(msg.rstrip() + "\n")
    except Exception:
        pass
    print(msg)


def _desktop_log(msg: str) -> None:
    try:
        d = ROOT / ".tools"
        d.mkdir(parents=True, exist_ok=True)
        import datetime

        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(d / "desktop_launch.log", "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg.rstrip()}\n")
    except Exception:
        pass


def _ollama_ready(timeout: float = 3) -> bool:
    # non-blocking check, timeout 3s, never raises — offline is ok
    try:
        import requests  # type: ignore

        r = requests.get("http://127.0.0.1:11434/api/tags", timeout=3)
        ok = r.status_code == 200
        _desktop_log(f"ollama check: {'OK' if ok else 'offline'} (requests, {r.status_code})")
        return ok
    except Exception:
        pass
    try:
        import urllib.request

        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=timeout) as r:
            ok = r.status == 200
            _desktop_log(f"ollama check: {'OK' if ok else 'offline'} (urllib, {r.status})")
            return ok
    except Exception as exc:
        _desktop_log(f"ollama check: offline ({exc})")
        return False


def _find_ollama() -> str | None:
    exe = shutil.which("ollama")
    if exe:
        return exe
    local = os.environ.get("LOCALAPPDATA") or ""
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    cands = [
        Path(local) / "Programs" / "Ollama" / "ollama.exe",
        Path(pf) / "Ollama" / "ollama.exe",
        Path("/usr/local/bin/ollama"),
        Path("/usr/bin/ollama"),
        Path.home() / "bin" / "ollama",
    ]
    for p in cands:
        if p.is_file():
            return str(p)
    return None


def ensure_ollama() -> bool:
    """Поднять ollama serve, если демон ещё не слушает :11434. Не блокирует запуск если offline."""
    if _ollama_ready(3):
        _server_log("ollama auto-start: already running")
        _desktop_log("ollama auto-start: already running")
        return True
    exe = _find_ollama()
    if not exe:
        _server_log("ollama auto-start: ollama.exe not found — continuing without Ollama")
        _desktop_log("ollama auto-start: ollama.exe not found — continuing without Ollama (non-blocking)")
        return False
    log_dir = ROOT / "cache"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_f = open(log_dir / "ollama.log", "a", encoding="utf-8", buffering=1)
    kwargs: dict = {
        "cwd": str(ROOT),
        "stdout": log_f,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen([exe, "serve"], **kwargs)
    except Exception as exc:
        _server_log(f"ollama auto-start: failed {exc}")
        _desktop_log(f"ollama auto-start: failed {exc} — continuing without Ollama")
        return False
    time.sleep(2)
    code = proc.poll()
    if code is not None:
        _server_log(f"ollama auto-start: process died code={code} — see cache/ollama.log")
        _desktop_log(f"ollama auto-start: process died code={code} — continuing without Ollama")
        return False
    t0 = time.time()
    for _ in range(40):
        if _ollama_ready(0.45):
            dt = time.time() - t0
            _server_log(f"ollama auto-start: launched pid={proc.pid}, ready in {dt:.1f}s")
            _desktop_log(f"ollama auto-start: launched pid={proc.pid}, ready in {dt:.1f}s")
            return True
        if proc.poll() is not None:
            _server_log(f"ollama auto-start: process died while waiting — see cache/ollama.log")
            _desktop_log("ollama auto-start: process died while waiting — continuing without Ollama")
            return False
        time.sleep(0.5)
    _server_log(f"ollama auto-start: launched pid={proc.pid}, not ready in 20s — continuing without Ollama")
    _desktop_log(f"ollama auto-start: launched pid={proc.pid}, not ready in 20s — continuing without Ollama (non-blocking, progress 100%)")
    return False


def _report_ollama_models() -> None:
    try:
        import json
        import urllib.request

        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as r:
            data = json.loads(r.read())
            names = [m.get("name") for m in data.get("models", [])]
            print(f"ollama models: {names}")
            need = ["deepseek-r1:8b", "llava:7b", "nomic-embed-text"]
            missing = [
                n
                for n in need
                if not any(m == n or (m or "").startswith(n.split(":")[0] + ":") for m in names)
            ]
            if missing:
                print(f"ollama: отсутствуют модели: {missing}. Установи: ollama pull <имя>")
    except Exception as e:
        print(f"ollama check failed: {e}")


def start_tray_process() -> subprocess.Popen | None:
    """Трей — отдельный процесс на venv-python, где стоят pystray/plyer."""
    try:
        probe = subprocess.run(
            [str(VENV_PY), "-c", "import pystray, plyer; print('ok')"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
    except Exception as exc:
        print("tray probe failed:", exc)
        print("Установи: venv\\Scripts\\python.exe -m pip install pystray plyer")
        return None
    if "ok" not in (probe.stdout or ""):
        print("pystray/plyer не установлены в venv — трей пропущен")
        print("Установи: venv\\Scripts\\python.exe -m pip install pystray plyer")
        return None
    kwargs: dict = {"cwd": str(ROOT)}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000
    try:
        return subprocess.Popen([str(VENV_PY), "-m", "backend.tray"], **kwargs)
    except Exception as exc:
        print("tray launch failed:", exc)
        return None


def acquire_instance_lock():
    cache_dir = ROOT / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    lock_path = cache_dir / "kadr.lock"
    lock_f = open(lock_path, "a+")
    try:
        lock_f.seek(0)
        if lock_f.read(1) == "":
            lock_f.write("0")
            lock_f.flush()
        lock_f.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            lock_f.write("\n" + str(os.getpid()))
            lock_f.flush()
        except Exception:
            pass
        return lock_f
    except OSError:
        try:
            lock_f.close()
        except Exception:
            pass
        return None


def release_instance_lock(lock_f) -> None:
    if not lock_f:
        return
    try:
        lock_f.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        lock_f.close()
    except Exception:
        pass


def main() -> None:
    os.chdir(ROOT)
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ.setdefault("OLLAMA_NUM_GPU", "1")

    if not (ROOT / "backend" / "main.py").is_file():
        die("backend/main.py not found — run from the Курымдык folder")

    print(f"Курымдык root : {ROOT}")
    print(f"This exe  : {sys.executable}  ({sys.version.split()[0]})")
    print("Looking for Python 3.10+ ...")

    best_ver, best_exe = find_best_python()
    print(f"Selected  : Python {best_ver[0]}.{best_ver[1]}  {best_exe}")

    # Если нас запустили через py -3 (=3.9), перезапускаемся на 3.13
    same = Path(best_exe).resolve() == Path(sys.executable).resolve()
    if not same or sys.version_info < MIN_VER:
        print(f"Re-launching with {best_exe}")
        raise SystemExit(
            subprocess.call([best_exe, str(Path(__file__).resolve())] + sys.argv[1:])
        )

    if sys.version_info < MIN_VER:
        die(f"Need Python 3.10+, got {sys.version}")

    lock_f = acquire_instance_lock()
    if lock_f is None:
        from backend.tray import health_ok, _open_window
        print("Курымдык already running — opening window")
        if health_ok(1.5):
            _open_window()
            return
        for _ in range(20):
            time.sleep(0.5)
            if health_ok(0.8):
                _open_window()
                return
        print("Another Курымдык is starting. Try again in a few seconds.")
        return

    try:
        _main_locked()
    finally:
        release_instance_lock(lock_f)


def _main_locked() -> None:
    # Старый venv от 3.9 — выкидываем
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
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        print(probe.stdout)
        print(probe.stderr)
        die("packages missing inside venv after pip install")
    print(probe.stdout.strip())

    url = f"http://{HOST}:{PORT}"
    debug = "--debug" in sys.argv
    app_mode = "--app" in sys.argv or not debug
    print(f"\n[3/3] Starting {url}" + ("  (debug)" if debug else ""))
    print("     Ollama expected at http://127.0.0.1:11434 (optional)\n")

    from backend.tray import health_ok, _open_window, _notify

    # быстрый путь: сервер уже работает — сразу окно, без splash
    if health_ok(timeout=2):
        _desktop_log(f"=== LAUNCH === server already running at {url} — opening window immediately (progress 100%)")
        print("Server already running — opening window")
        _open_window()
        _desktop_log("main window opened (already running)")
        if not debug:
            start_tray_process()
            try:
                while health_ok(2.0):
                    time.sleep(2)
            except KeyboardInterrupt:
                pass
        return

    # === БАГ1 ФИКС: простая сплэш-логика БЕЗ Ollama в середине ===
    splash_proc = None
    _desktop_log("=== LAUNCH START ===")
    _desktop_log(f"STEP 1: opening splash app_mode={app_mode} debug={debug} progress 0%")
    if app_mode and not debug:
        splash_proc = _open_splash()
        pid = splash_proc.pid if splash_proc else None
        _desktop_log(f"STEP 1: splash opened pid={pid} progress 30%")
    else:
        _desktop_log("STEP 1: skip splash (debug mode)")

    _desktop_log("STEP 2: sleep 3 sec (splash visible) progress 50% — NO Ollama check here (БАГ1 spec)")
    time.sleep(3)
    _desktop_log("STEP 2: sleep done — progress 50% — proceeding to health check БЕЗ Ollama")

    log_dir = ROOT / "cache"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "server.log"
    log_f = open(log_path, "a", encoding="utf-8", buffering=1)
    _desktop_log(f"STEP 3: starting uvicorn at {url} log={log_path}")

    cmd = [py, "-m", "uvicorn", "backend.main:app", "--host", HOST, "--port", str(PORT)]
    kwargs: dict = {"cwd": str(ROOT)}
    if os.name == "nt" and not debug:
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        kwargs["stdout"] = log_f
        kwargs["stderr"] = subprocess.STDOUT
    else:
        kwargs["stdout"] = None if debug else log_f
        kwargs["stderr"] = None if debug else subprocess.STDOUT

    try:
        proc = subprocess.Popen(cmd, **kwargs)
        _desktop_log(f"STEP 3: server Popen pid={proc.pid} progress 60%")
    except Exception as e:
        _desktop_log(f"STEP 3: Popen failed {e}")
        if splash_proc:
            try:
                splash_proc.terminate()
            except Exception:
                pass
        die(f"failed to start server: {e}")

    _desktop_log("STEP 4: polling http://127.0.0.1:8000/api/health timeout=2 (max 30s)")
    ready = False
    for attempt in range(60):
        if proc.poll() is not None:
            _desktop_log(f"STEP 4: server died code={proc.poll()} attempt={attempt+1}")
            break
        try:
            if health_ok(timeout=2):
                ready = True
                _desktop_log(f"STEP 4: health OK attempt={attempt+1} — progress 100%")
                break
            else:
                _desktop_log(f"STEP 4: health not ready attempt={attempt+1}")
        except Exception as e:
            _desktop_log(f"STEP 4: health exception attempt={attempt+1} err={e}")
        time.sleep(0.5)

    _desktop_log(f"STEP 5: closing splash ready={ready} progress 100%")
    if splash_proc:
        try:
            splash_proc.terminate()
            _desktop_log("STEP 5: splash terminated")
        except Exception as e:
            _desktop_log(f"STEP 5: splash terminate err {e}")
        # дать Edge закрыться
        time.sleep(0.4)

    if not ready:
        _desktop_log("STEP 5: server not ready in 30s — error splash")
        if app_mode:
            _open_splash(error=True)
        die("server did not become ready in 30s — see cache/server.log")

    _desktop_log("STEP 6: opening main window (always, even if Ollama offline) progress 100%")
    if app_mode:
        try:
            _open_window()
            _desktop_log("STEP 6: _open_window() called — main window should be visible")
        except Exception as e:
            _desktop_log(f"STEP 6: _open_window exception {e}")
            try:
                webbrowser.open(url)
                _desktop_log("STEP 6: fallback webbrowser.open")
            except Exception as e2:
                _desktop_log(f"STEP 6: fallback failed {e2}")
        if not debug:
            _notify("Курымдык", "Курымдык свёрнут в трей. ПКМ по иконке → Выход")
            _desktop_log("STEP 6: tray notify sent")
    else:
        try:
            webbrowser.open(url)
            _desktop_log("STEP 6: browser opened (debug)")
        except Exception as e:
            _desktop_log(f"STEP 6: browser open err {e}")

    # БАГ1: Ollama проверка ТОЛЬКО после открытия окна, в фоне — не в середине splash→health→window
    def _bg_ollama_after():
        try:
            _desktop_log("BG-AFTER: ollama check start (non-blocking, after window)")
            ok = ensure_ollama()
            _desktop_log(f"BG-AFTER: ollama check done ok={ok}")
            if ok:
                _report_ollama_models()
        except Exception as e:
            _desktop_log(f"BG-AFTER: ollama exception {e}")
    import threading
    threading.Thread(target=_bg_ollama_after, daemon=True).start()
    _desktop_log("BG-AFTER: Ollama thread started after window (non-blocking)")

    def _stop() -> None:
        try:
            proc.terminate()
            _desktop_log("server terminated via _stop")
        except Exception:
            pass

    if not debug:
        start_tray_process()
        _desktop_log("tray process started")

    _desktop_log("STEP 7: waiting for server proc.wait()")
    try:
        raise SystemExit(proc.wait())
    except KeyboardInterrupt:
        _desktop_log("KeyboardInterrupt — stopping server")
        _stop()
        raise SystemExit(0)


def _browser_bins() -> list[Path]:
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    return [
        Path(pf86) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(pf) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(pf) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(pf86) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]


def _open_splash(error: bool = False, warn: bool = False) -> subprocess.Popen | None:
    splash = ROOT / "frontend" / "splash.html"
    if not splash.is_file():
        return None
    uri = splash.resolve().as_uri()
    if error:
        uri += "?err=1"
    elif warn:
        uri += "?warn=ollama"
    profile = ROOT / "cache" / "edge-splash"
    profile.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        for bin_ in _browser_bins():
            if bin_.is_file():
                return subprocess.Popen(
                    [str(bin_), f"--app={uri}", f"--user-data-dir={profile}"],
                    cwd=str(ROOT),
                )
    try:
        webbrowser.open(uri)
    except Exception:
        pass
    return None


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
        raise SystemExit(0)
