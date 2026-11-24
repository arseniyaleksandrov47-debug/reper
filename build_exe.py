"""Build Reper Windows desktop app (--onedir) and Reper_Setup.exe installer."""
import argparse
import ctypes
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import winreg

ROOT = Path(__file__).resolve().parent
APP_UNINSTALL_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    r"\{A7E4C910-4D2B-4F88-8A2C-9E31B60D1102}_is1"
)


def _copy_sqlite(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(str(dst))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()


def _copy_tree_contents(
    src_dir: Path,
    dst_dir: Path,
    *,
    skip_names: set[str] | None = None,
) -> None:
    if not src_dir.is_dir():
        return
    skip = skip_names or set()
    dst_dir.mkdir(parents=True, exist_ok=True)
    for item in src_dir.iterdir():
        if item.name in skip or item.name.endswith(("-wal", "-shm")):
            continue
        target = dst_dir / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        elif item.suffix in (".db", ".sqlite3"):
            _copy_sqlite(item, target)
        else:
            shutil.copy2(item, target)


def _find_iscc() -> Path | None:
    found = shutil.which("ISCC") or shutil.which("iscc")
    if found:
        return Path(found)
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Inno Setup 6" / "ISCC.exe",
    ]
    return next((c for c in candidates if c.is_file()), None)


def _find_installed_dir() -> Path | None:
    if os.name != "nt":
        return None
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, APP_UNINSTALL_KEY) as key:
                loc, _ = winreg.QueryValueEx(key, "InstallLocation")
                if loc:
                    p = Path(loc)
                    if (p / "Reper.exe").is_file():
                        return p
        except OSError:
            continue
    return None


def _is_installed_running(install_dir: Path) -> bool:
    try:
        res = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"Get-Process -Name Reper -ErrorAction SilentlyContinue | Where-Object {{ $_.Path -like '{install_dir}*' }} | Measure-Object | Select-Object -ExpandProperty Count",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return int((res.stdout or "0").strip() or "0") > 0
    except Exception:
        return False


def _sync_installed_copy(setup_exe: Path) -> None:
    """Silently refresh an already-installed local copy so desktop shortcut stays up to date."""
    install_dir = _find_installed_dir()
    if not install_dir:
        return
    if _is_installed_running(install_dir):
        print(f"Skipping auto-update of {install_dir}: Reper.exe is currently running.")
        return
    res = subprocess.run(
        [
            str(setup_exe),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/CURRENTUSER",
            f"/DIR={install_dir}",
        ],
        check=False,
    )
    if res.returncode == 0:
        print(f"Updated already-installed Reper in place at: {install_dir}")
    else:
        print(f"Warning: in-place installer update exited with code {res.returncode}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Reper desktop app & Windows Setup.exe")
    parser.add_argument("--with-secrets", action="store_true", help="Copy local DPAPI secrets alongside portable dist")
    parser.add_argument("--with-cache", action="store_true", help="Copy .local-cache datasets alongside dist")
    parser.add_argument("--skip-frontend", action="store_true", help="Reuse existing frontend/dist bundle")
    parser.add_argument("--no-seed-db", action="store_true", help="Do not embed starter SQLite DBs in installer")
    parser.add_argument("--skip-installer", action="store_true", help="Skip Inno Setup Reper_Setup.exe compilation")
    parser.add_argument("--no-update-installed", action="store_true", help="Do not refresh existing local installation")
    args = parser.parse_args()

    # 1. Generate Windows .ico (BMP frames) + wizard graphics + web favicon
    subprocess.run([sys.executable, str(ROOT / "assets" / "generate_icon.py")], cwd=ROOT, check=True)
    ico_path = ROOT / "assets" / "reper.ico"
    favicon_dst = ROOT / "frontend" / "public" / "favicon.ico"
    favicon_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ico_path, favicon_dst)

    # 2. Frontend production build
    dist_front = ROOT / "frontend" / "dist" / "index.html"
    if not args.skip_frontend or not dist_front.is_file():
        subprocess.run(["npm", "run", "build"], cwd=ROOT / "frontend", check=True, shell=True)

    old_onefile = ROOT / "dist" / "Reper.exe"
    if old_onefile.is_file():
        old_onefile.unlink()

    # 3. PyInstaller --onedir build (zero bootloader fork process, fast native launch)
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(ROOT / "Reper.spec")],
        cwd=ROOT,
        check=True,
    )

    out_dir = ROOT / "dist" / "Reper"
    skip_seed = {"moscow_commercial.db", "saint_petersburg_commercial.db"} if args.no_seed_db else None
    _copy_tree_contents(ROOT / "data", out_dir / "seed_data", skip_names=skip_seed)
    _copy_tree_contents(ROOT / "data", out_dir / "data", skip_names=skip_seed)

    if args.with_cache:
        _copy_tree_contents(
            ROOT / ".local-cache",
            out_dir / ".local-cache",
            skip_names={"python-bytecode"},
        )
    if args.with_secrets:
        _copy_tree_contents(ROOT / ".local-secrets", out_dir / ".local-secrets")

    exe_path = out_dir / "Reper.exe"
    if not exe_path.is_file():
        raise FileNotFoundError(f"Expected desktop executable not built: {exe_path}")
    print(f"Built Reper desktop folder cleanly at: {out_dir}")

    # 4. Build Windows Setup installer (Reper_Setup.exe) and refresh local install
    if not args.skip_installer:
        iscc = _find_iscc()
        if iscc is None:
            raise FileNotFoundError("Inno Setup 6 compiler (ISCC.exe) not found")
        iss_path = ROOT / "installer" / "Reper.iss"
        subprocess.run([str(iscc), str(iss_path)], cwd=ROOT / "installer", check=True)
        setup_exe = ROOT / "dist" / "Reper_Setup.exe"
        if not setup_exe.is_file():
            raise FileNotFoundError(f"Expected installer not built: {setup_exe}")
        size_mb = setup_exe.stat().st_size / (1024 * 1024)
        print(f"Built Windows Installer ({size_mb:.1f} MB) cleanly at: {setup_exe}")
        if not args.no_update_installed:
            _sync_installed_copy(setup_exe)

    if os.name == "nt":
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
        subprocess.run(["ie4uinit.exe", "-show"], check=False)


if __name__ == "__main__":
    main()
