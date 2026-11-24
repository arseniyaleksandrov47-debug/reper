"""PyWebView + Uvicorn desktop launcher for Reper (.exe entrypoint)."""
import argparse
import ctypes
import os
from pathlib import Path
import shutil
import socket
import sys
import threading
import time
import traceback
import urllib.request
import uvicorn
import webview

from real_estate_engine.cities import runtime_root


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_ready(host: str, port: int, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.08)


def _bypass_local_proxy() -> None:
    existing = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    items = [x.strip() for x in existing.split(",") if x.strip()]
    for host in ("127.0.0.1", "localhost"):
        if host not in items:
            items.append(host)
    joined = ",".join(items)
    os.environ["NO_PROXY"] = joined
    os.environ["no_proxy"] = joined


def _local_get(url: str, timeout_s: float = 5.0) -> tuple[int, str]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout_s) as resp:
        return resp.status, resp.read().decode("utf-8")


def _configure_windowed_streams(root: Path) -> None:
    """Ensure stdio/exceptions and root logs have a valid sink when running frozen without console."""
    import logging

    log_path = root / "reper_desktop.log"
    if getattr(sys, "stdout", None) is not None and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if getattr(sys, "frozen", False) or getattr(sys, "stdout", None) is None or getattr(sys, "stderr", None) is None:
        log_file = open(log_path, "a", encoding="utf-8", buffering=1)
        if getattr(sys, "stdout", None) is None or not hasattr(sys.stdout, "fileno"):
            sys.stdout = log_file
        if getattr(sys, "stderr", None) is None or not hasattr(sys.stderr, "fileno"):
            sys.stderr = log_file
    root_logger = logging.getLogger()
    if root_logger.level == logging.NOTSET or root_logger.level > logging.INFO:
        root_logger.setLevel(logging.INFO)
    if not any(getattr(h, "_reper_file_sink", False) for h in root_logger.handlers):
        try:
            fh = logging.FileHandler(log_path, encoding="utf-8")
            fh._reper_file_sink = True  # type: ignore[attr-defined]
            fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
            root_logger.addHandler(fh)
        except Exception:
            pass


def _set_windows_app_id() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Reper.Desktop.1")
    except Exception:
        pass


def _find_icon_path() -> Path | None:
    candidates = []
    for name in ("reper.ico", "geoestate.ico"):
        if getattr(sys, "frozen", False):
            exe_dir = Path(sys.executable).resolve().parent
            candidates.append(exe_dir / "_internal" / "assets" / name)
            if hasattr(sys, "_MEIPASS"):
                candidates.append(Path(sys._MEIPASS) / "assets" / name)
        candidates.append(Path(__file__).resolve().parent / "assets" / name)
    for p in candidates:
        if p.is_file():
            return p
    return None


def _bootstrap_seed_data(root: Path, seed_root: Path | None = None) -> None:
    """Copy starter reference files and SQLite DBs to writable root on first launch."""
    if seed_root is None:
        if not getattr(sys, "frozen", False):
            return
        candidates = [Path(sys.executable).resolve().parent / "seed_data"]
        if hasattr(sys, "_MEIPASS"):
            candidates.append(Path(sys._MEIPASS) / "seed_data")
        seed_root = next((c for c in candidates if c.is_dir()), None)
        if seed_root is None:
            return
    if not seed_root.is_dir():
        return
    target_data = root / "data"
    target_data.mkdir(parents=True, exist_ok=True)
    for src in seed_root.rglob("*"):
        if not src.is_file() or src.name.endswith(("-wal", "-shm")):
            continue
        rel = src.relative_to(seed_root)
        dst = target_data / rel
        if dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f".{dst.name}.tmp")
        shutil.copy2(src, tmp)
        tmp.replace(dst)


def _apply_frameless_chrome(native_form) -> None:
    """Disable Win10/11 DWM border stroke and match dark host background color."""
    if os.name != "nt" or native_form is None:
        return
    try:
        from System.Drawing import ColorTranslator  # type: ignore[import-not-found]

        hwnd = int(native_form.Handle.ToInt32())
        # DWMWA_BORDER_COLOR (34) = DWMWA_COLOR_NONE (0xFFFFFFFE) removes Win11 1px frame stroke
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 34, ctypes.byref(ctypes.c_uint(0xFFFFFFFE)), 4
        )
        # Match WinForms BackColor to dark graphite so DPI rounding never exposes white fringes
        native_form.BackColor = ColorTranslator.FromHtml("#181b1f")
    except Exception:
        pass


class _DesktopWindowApi:
    """JS bridge exposed to the UI for frameless desktop window controls."""

    _EDGE_HT = {
        "left": 10,
        "right": 11,
        "top": 12,
        "top-left": 13,
        "top-right": 14,
        "bottom": 15,
        "bottom-left": 16,
        "bottom-right": 17,
    }

    def __init__(self) -> None:
        self._window = None

    def attach(self, window) -> None:
        self._window = window

    def _invoke_ui(self, native, fn) -> None:
        if os.name != "nt" or native is None:
            return
        try:
            from System import Action  # type: ignore[import-not-found]

            native.Invoke(Action(fn))
        except Exception:
            pass

    def minimize(self) -> None:
        if self._window is not None:
            self._window.minimize()

    def is_maximized(self) -> bool:
        if self._window is None or getattr(self._window, "native", None) is None:
            return False
        try:
            import System.Windows.Forms as WinForms  # type: ignore[import-not-found]

            return self._window.native.WindowState == WinForms.FormWindowState.Maximized
        except Exception:
            return False

    def toggle_maximize(self) -> bool:
        if self._window is None:
            return False
        native = getattr(self._window, "native", None)
        if self.is_maximized():
            self._window.restore()

            def _clear_max_size():
                from System.Drawing import Size  # type: ignore[import-not-found]

                native.MaximumSize = Size(0, 0)

            self._invoke_ui(native, _clear_max_size)
            return False

        def _clamp_work_area():
            import System.Windows.Forms as WinForms  # type: ignore[import-not-found]

            area = WinForms.Screen.FromControl(native).WorkingArea
            native.MaximumSize = area.Size

        self._invoke_ui(native, _clamp_work_area)
        self._window.maximize()
        return True

    def close(self) -> None:
        if self._window is not None:
            self._window.destroy()

    def begin_resize(self, edge: str) -> None:
        if os.name != "nt" or self._window is None:
            return
        ht_code = self._EDGE_HT.get(edge)
        native = getattr(self._window, "native", None)
        if ht_code is None or native is None:
            return

        def _start_resize():
            hwnd = int(native.Handle.ToInt32())
            ctypes.windll.user32.ReleaseCapture()
            ctypes.windll.user32.SendMessageW(hwnd, 0x00A1, ht_code, 0)

        self._invoke_ui(native, _start_resize)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--smoke-check", action="store_true")
    args, _ = parser.parse_known_args(argv)

    root = runtime_root()
    os.chdir(root)
    _set_windows_app_id()
    _bypass_local_proxy()
    _configure_windowed_streams(root)
    _bootstrap_seed_data(root)

    try:
        from desktop_api import app

        port = _free_port()
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            log_config=None,
        )
        server = uvicorn.Server(config)

        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        _wait_ready("127.0.0.1", port)

        if args.smoke_check:
            try:
                st_health, payload = _local_get(f"http://127.0.0.1:{port}/api/health")
                if st_health != 200:
                    raise RuntimeError(f"Health check returned {st_health}")
                st_index, index_html = _local_get(f"http://127.0.0.1:{port}/")
                if st_index != 200 or '<div id="root"' not in index_html:
                    raise RuntimeError(f"Index check failed: status={st_index}")
                print(payload)
            finally:
                server.should_exit = True
                thread.join(timeout=3)
            return

        window_api = _DesktopWindowApi()
        window = webview.create_window(
            title="Reper",
            url=f"http://127.0.0.1:{port}/",
            width=1440,
            height=900,
            min_size=(1160, 740),
            frameless=True,
            easy_drag=False,
            shadow=False,
            background_color="#181b1f",
            js_api=window_api,
        )
        window_api.attach(window)

        def _on_shown():
            _apply_frameless_chrome(getattr(window, "native", None))

        def _on_closed():
            server.should_exit = True

        window.events.shown += _on_shown
        window.events.closed += _on_closed
        icon_path = _find_icon_path()
        cache_dir = root / ".webview-cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        webview.start(
            icon=str(icon_path) if icon_path else None,
            private_mode=False,
            storage_path=str(cache_dir),
        )
        server.should_exit = True
        thread.join(timeout=2.0)
        os._exit(0)
    except Exception:
        with open(root / "reper_desktop.log", "a", encoding="utf-8") as fh:
            fh.write(traceback.format_exc() + "\n")
        if args.smoke_check:
            sys.exit(1)
        raise


if __name__ == "__main__":
    main()
