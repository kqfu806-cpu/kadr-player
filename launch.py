# -*- coding: utf-8 -*-
"""Запуск Курымдык — только сайт: venv → pip → uvicorn → прозрачное окно-оверлей (pywebview)."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
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

# Оверлей: адрес страницы и размеры нативного окна (свёрнуто / раскрыто)
OVERLAY_PATH = "/overlay"
OVERLAY_URL = f"http://{HOST}:{PORT}{OVERLAY_PATH}"
OVERLAY_W = 380
OVERLAY_H_COLLAPSED = 92
OVERLAY_H_EXPANDED = 470

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
                        _log(f"cleanup: removed {f.name} age {age:.1f}d")
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

    # TASK7: доп. мусор из корня: *.tmp, *.temp, *.cache, *.old, *.orig (кроме .env.orig), *.orig.bak
    try:
        protected_names = {
            ".env", "requirements.txt", "run.bat", "start_app.bat", "launch.py",
            "install_autostart.bat", "remove_autostart.bat"
        }
        protected_dirs = ["backend", "frontend", "tools", "venv", "cache", "docs"]
        for pat in ["*.tmp", "*.temp", "*.cache", "*.old", "*.orig", "*.orig.bak"]:
            for f in ROOT.glob(pat):
                try:
                    if not f.is_file():
                        continue
                    if f.name == ".env.orig":
                        _log(f"cleanup: keep {f.name} (protected .env.orig)")
                        continue
                    if f.name in protected_names:
                        _log(f"cleanup: keep {f.name} (protected)")
                        continue
                    # не трогать если внутри protected_dirs (но glob только корень, так что ок)
                    f.unlink()
                    _log(f"cleanup: removed {f.name} (TASK7 root pattern {pat})")
                except Exception as e:
                    _log(f"cleanup: {f.name} error {e}")
        # также *.tmp может быть с точкой: уже покрыто, но проверить отдельно
        for f in ROOT.glob("*.tmp"):
            try:
                if f.is_file() and f.name not in protected_names:
                    f.unlink()
                    _log(f"cleanup: removed {f.name} (tmp)")
            except Exception as e:
                _log(f"cleanup: {f.name} error {e}")
    except Exception as e:
        _log(f"cleanup: TASK7 root extra error {e}")

    # TASK7: из cache/ удалять: test_*, tmp_*, *.tmp (кроме logs/ и *.sqlite3)
    try:
        cache_dir = ROOT / "cache"
        if cache_dir.is_dir():
            for pat in ["test_*", "tmp_*", "*.tmp"]:
                for f in cache_dir.glob(pat):
                    try:
                        if not f.is_file():
                            continue
                        # не трогать cache/logs/ и *.sqlite3
                        if "logs" in f.parts or f.suffix == ".sqlite3":
                            _log(f"cleanup: keep {f.relative_to(ROOT)} (protected)")
                            continue
                        f.unlink()
                        _log(f"cleanup: removed {f.relative_to(ROOT)} ({pat})")
                    except Exception as e:
                        _log(f"cleanup: {f} error {e}")
            # также cache/logs не трогать, cache/*.sqlite3 не трогать — уже выше
    except Exception as e:
        _log(f"cleanup: TASK7 cache error {e}")

    # TASK7: удалять пустые папки .pytest_run_*, .pytest-run-*
    try:
        for pat in [".pytest_run_*", ".pytest-run-*"]:
            for d in ROOT.glob(pat):
                try:
                    if d.is_dir():
                        if not any(d.iterdir()):
                            d.rmdir()
                            _log(f"cleanup: removed empty dir {d.name} (TASK7)")
                        else:
                            _log(f"cleanup: keep {d.name} not empty")
                except Exception as e:
                    _log(f"cleanup: {d.name} error {e}")
        # также .pytest_run_* внутри cache? проверить отдельно
        for pat in [".pytest_run_*", ".pytest-run-*"]:
            for d in (ROOT / "cache").glob(pat) if (ROOT / "cache").exists() else []:
                try:
                    if d.is_dir() and not any(d.iterdir()):
                        d.rmdir()
                        _log(f"cleanup: removed empty cache/{d.name}")
                except Exception:
                    pass
    except Exception as e:
        _log(f"cleanup: TASK7 empty dirs error {e}")

    _log("cleanup: protected venv/, backend/, frontend/, tools/, cache/*.sqlite3, cache/logs/, .env, requirements.txt, run.bat, start_app.bat, launch.py, install_autostart.bat, remove_autostart.bat, docs/ — not touched")
    _log("cleanup: done")

# ---------- прозрачное окно-оверлей ----------

def wait_for_server(url: str, timeout: float = 40.0) -> bool:
    """Ждём, пока uvicorn поднимет /api/health.

    Прокси системно НЕ используем (иначе VPN/прокси перехватывает запрос к
    127.0.0.1 и health-check ложно падает), плюс запасной вариант — по TCP.
    """
    deadline = time.time() + timeout
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.time() < deadline:
        try:
            with opener.open(url, timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        try:
            host, port = url.split("//", 1)[1].split("/", 1)[0].split(":")
            with socket.create_connection((host, int(port)), timeout=2):
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def _running_in_venv() -> bool:
    """Мы внутри venv проекта? Нужно, чтобы был доступен импорт pywebview."""
    try:
        return Path(sys.executable).resolve() == VENV_PY.resolve()
    except Exception:
        return False


class OverlayApi:
    """JS-API оверлея: раскрытие/сворачивание меняет размер нативного окна."""

    def __init__(self) -> None:
        self.window = None
        self.expanded = False

    def bind(self, window) -> None:
        self.window = window

    def set_expanded(self, expanded: bool) -> bool:
        expanded = bool(expanded)
        if self.expanded == expanded:
            return self.expanded
        self.expanded = expanded
        h = OVERLAY_H_EXPANDED if expanded else OVERLAY_H_COLLAPSED
        try:
            win = self.window
            if win is None:
                return self.expanded
            # держим верх-центр: считаем центр до ресайза и возвращаем окно туда же
            try:
                center_x = int(win.x) + int(win.width) // 2
                top_y = int(win.y)
            except Exception:
                center_x = None
                top_y = None
            win.resize(OVERLAY_W, h)
            if center_x is not None and top_y is not None:
                win.move(center_x - OVERLAY_W // 2, top_y)
            print(f"overlay: {'expanded' if expanded else 'collapsed'} -> {OVERLAY_W}x{h}")
        except Exception as e:
            print(f"overlay resize error: {e}")
        return self.expanded

    def ping(self) -> str:
        return "pong"


def move_to_top_center(window) -> None:
    """Ставим окно по верхнему краю, по центру основного экрана."""
    try:
        import webview
        screens = getattr(webview, "screens", None) or []
        if not screens:
            return
        screen = screens[0]
        sw = int(getattr(screen, "width", 0) or 0)
        sh = int(getattr(screen, "height", 0) or 0)
        if sw <= 0 or sh <= 0:
            return
        x = (sw - OVERLAY_W) // 2
        y = 24
        window.move(x, y)
        print(f"overlay: window at {x},{y} (screen {sw}x{sh})")
    except Exception as e:
        print(f"overlay: не удалось спозиционировать окно: {e}")


def open_overlay(open_main_ui: bool = True, show_main_ui: bool = False, debug: bool = False) -> int:
    """Открывает прозрачное frameless-окно с оверлеем. Возвращает код выхода."""
    try:
        import webview
    except ImportError:
        print("ERROR: не установлен pywebview. Установи: pip install pywebview")
        return 2

    api = OverlayApi()
    window = webview.create_window(
        "Курымдык — оверлей",
        url=OVERLAY_URL,
        js_api=api,
        width=OVERLAY_W,
        height=OVERLAY_H_COLLAPSED,
        frameless=True,          # без рамки
        transparent=True,        # прозрачное окно: видно игру
        on_top=True,             # = always_on_top, всегда поверх игр
        # pywebview ждёт 6-значный hex; прозрачность на Windows ставится
        # отдельно через transparent=True (DefaultBackgroundColor = Transparent)
        background_color="#000000",
        easy_drag=False,
        resizable=False,
        confirm_close=False,
        text_select=False,
    )
    api.bind(window)

    def _on_loaded():
        move_to_top_center(window)

    try:
        window.events.loaded += _on_loaded
    except Exception:
        pass

    if open_main_ui:
        # окно основного интерфейса: крутит логику плеера и публикует
        # текущий трек в /ws, поэтому оверлей работает и без браузера
        # (--with-ui — показать его на экране, по умолчанию скрытое)
        try:
            webview.create_window(
                "Курымдык",
                url=f"http://{HOST}:{PORT}/",
                hidden=not show_main_ui,
                focus=show_main_ui,
                resizable=show_main_ui,
            )
            print(
                "Main UI: hidden window (логика плеера + публикация трека в /ws)"
                if not show_main_ui
                else "Main UI: visible window"
            )
        except Exception as e:
            print(f"Скрытое окно основного UI не создано: {e}")

    try:
        webview.start(debug=debug)
    except Exception as e:
        print(f"pywebview не запустился: {e}")
        return 3
    return 0


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
    if (not same or sys.version_info < MIN_VER) and not _running_in_venv():
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
    if os.environ.get("KADR_IN_VENV") == "1" and _running_in_venv():
        print("\n[2/3] venv already ready (pywebview доступен) — установку пропускаем")
    else:
        print("\n[2/3] Installing dependencies ...")
        run([py, "-m", "ensurepip", "--upgrade"], check=False)
        run([py, "-m", "pip", "install", "--upgrade", "pip"])
        run([py, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")])

    probe = subprocess.run(
        [py, "-c", "import fastapi, uvicorn, mutagen, httpx, PIL, webview; print('imports-ok')"],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    if probe.returncode != 0:
        print(probe.stdout)
        print(probe.stderr)
        die("packages missing inside venv after pip install")
    print(probe.stdout.strip())

    # pywebview нужен именно в процессе лаунчера (там живёт GUI-поток),
    # поэтому перезапускаем скрипт интерпретатором venv
    if not _running_in_venv():
        print("Re-launching inside venv (нужен импорт pywebview) ...")
        env = os.environ.copy()
        env["KADR_IN_VENV"] = "1"
        raise SystemExit(
            subprocess.call(
                [str(VENV_PY), str(Path(__file__).resolve())] + sys.argv[1:],
                cwd=str(ROOT),
                env=env,
            )
        )

    url = f"http://{HOST}:{PORT}"
    print(f"\n[3/3] Starting {url}")
    print(f"Uvicorn running on {url}")

    # запустить uvicorn отдельным процессом, чтобы главный поток жил в GUI pywebview
    cmd = [py, "-m", "uvicorn", "backend.main:app", "--host", HOST, "--port", str(PORT)]
    print(">", " ".join(cmd))
    proc = subprocess.Popen(cmd, cwd=str(ROOT))

    # дождаться готовности сервера, иначе оверлей откроется пустым
    if not wait_for_server(f"{url}/api/health", timeout=45.0):
        print("WARNING: сервер не ответил на /api/health за 45 c — оверлей может не подключиться")
    else:
        print(f"Server ready: {url}")

    print(f"Overlay: {OVERLAY_URL}")
    print(f"Main UI: http://{HOST}:{PORT}/")
    code = 0
    try:
        code = open_overlay(
            open_main_ui="--no-ui" not in sys.argv,
            show_main_ui="--with-ui" in sys.argv,
            debug="--debug" in sys.argv,
        )
    finally:
        # оверлей закрыт — гасим сервер
        try:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception:
            pass
    if code in (2, 3):
        # pywebview недоступен/не запустился — подстраховка: та же страница в браузере
        print("Fallback: открываю оверлей в браузере")
        try:
            webbrowser.open(OVERLAY_URL)
        except Exception as e:
            print(f"webbrowser.open failed: {e}")
        code = subprocess.call([py, "-m", "uvicorn", "backend.main:app", "--host", HOST, "--port", str(PORT)], cwd=str(ROOT))
    raise SystemExit(code)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
        raise SystemExit(0)
