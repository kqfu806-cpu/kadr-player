"""Иконка Курымдык в системном трее. Отдельный процесс: python -m backend.tray."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
ICON_PATH = ROOT / "frontend" / "icons" / "kadr.ico"
HOST = "127.0.0.1"
PORT = 8000
URL = f"http://{HOST}:{PORT}"


def _app_window_running() -> bool:
    # TASK5: simplified stub — no Edge --app detection needed
    return False


def _open_window() -> None:
    # TASK5: simplified — default browser only, no Edge --app / pywebview
    try:
        webbrowser.open("http://127.0.0.1:8000")
    except Exception:
        pass


def _notify(title: str, message: str) -> None:
    try:
        from plyer import notification

        notification.notify(title=title, message=message, timeout=5, app_name="Курымдык")
        return
    except Exception:
        pass
    if os.name == "nt":
        try:
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Add-Type -AssemblyName System.Windows.Forms; "
                    "[System.Windows.Forms.MessageBox]::Show('"
                    + message.replace("'", "''")
                    + "','"
                    + title.replace("'", "''")
                    + "')",
                ],
                timeout=8,
                capture_output=True,
            )
        except Exception:
            pass


def start_tray(stop_server: Callable[[], None]) -> threading.Thread:
    """ПКМ-меню в трее. Не блокирует вызывающий поток."""

    def worker() -> None:
        try:
            import pystray
            from PIL import Image
        except Exception as exc:
            print("tray skip:", exc)
            return

        img = Image.open(ICON_PATH) if ICON_PATH.is_file() else Image.new("RGBA", (64, 64), (20, 20, 24, 255))

        def on_open(_icon, _item) -> None:
            _open_window()

        def on_refresh(_icon, _item) -> None:
            try:
                import urllib.request

                req = urllib.request.Request(
                    f"{URL}/api/resolve-all?force=true", method="POST"
                )
                urllib.request.urlopen(req, timeout=4)
            except Exception:
                pass

        def on_music(_icon, _item) -> None:
            music = Path.home() / "Music"
            path = music if music.is_dir() else Path.home()
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(path)])

        def on_cache(_icon, _item) -> None:
            cache = ROOT / "cache"
            cache.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(cache)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(cache)])

        def on_logs(_icon, _item) -> None:
            log = ROOT / "cache" / "server.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            if not log.is_file():
                log.write_text("", encoding="utf-8")
            if os.name == "nt":
                os.startfile(log)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(log)])

        def on_exit(icon, _item) -> None:
            try:
                stop_server()
            except Exception:
                pass
            try:
                import urllib.request

                req = urllib.request.Request(f"{URL}/api/shutdown", method="POST")
                urllib.request.urlopen(req, timeout=3)
            except Exception:
                pass
            try:
                icon.stop()
            except Exception:
                pass

        menu = pystray.Menu(
            pystray.MenuItem("Открыть Курымдык", on_open, default=True),
            pystray.MenuItem("Обновить клипы", on_refresh),
            pystray.MenuItem("Открыть папку с музыкой", on_music),
            pystray.MenuItem("Открыть папку кэша", on_cache),
            pystray.MenuItem("Показать логи", on_logs),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", on_exit),
        )
        icon = pystray.Icon("kadr", img, "Курымдык", menu)

        def setup(ic) -> None:
            ic.visible = True
            try:
                ic.notify("Курымдык свёрнут в трей. ПКМ по иконке → Выход", "Курымдык")
            except Exception:
                _notify("Курымдык", "Курымдык свёрнут в трей. ПКМ по иконке → Выход")

        icon.run(setup=setup)

    th = threading.Thread(target=worker, name="kadr-tray", daemon=True)
    th.start()
    return th


def health_ok(timeout: float = 1.5) -> bool:
    try:
        import urllib.request

        with urllib.request.urlopen(f"{URL}/api/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


if __name__ == "__main__":
    def _noop() -> None:
        return None

    start_tray(_noop)
    import time

    while True:
        time.sleep(60)

