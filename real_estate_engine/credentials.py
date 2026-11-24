"""Local Windows credential storage; no plaintext service keys in source files."""
import ctypes
import os
from pathlib import Path
from .cities import runtime_root


DEFAULT_KEY_PATH = runtime_root() / ".local-secrets" / "nvidia.dpapi"
DEFAULT_DADATA_KEY_PATH = runtime_root() / ".local-secrets" / "dadata.dpapi"
DEFAULT_YANDEX_MAPS_KEY_PATH = runtime_root() / ".local-secrets" / "yandex_maps.dpapi"
DEFAULT_DGIS_MAPS_KEY_PATH = runtime_root() / ".local-secrets" / "dgis_maps.dpapi"


def _protect(data: bytes, decrypt=False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Local encrypted credentials require Windows")
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        operation = library.CryptUnprotectData
        operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                              ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    else:
        operation = library.CryptProtectData
        operation.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
                              ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise RuntimeError("Windows credential encryption/decryption failed")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))


def _save_key(value: str, default_path: Path, path=None) -> None:
    target = Path(path) if path is not None else default_path
    protected = _protect(value.encode("utf-8"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(protected)


def _load_key(env_var: str, file_env_var: str, default_path: Path, error_message: str) -> str:
    environment_key = os.environ.get(env_var, "").strip()
    if environment_key:
        return environment_key
    path = Path(os.environ.get(file_env_var) or default_path)
    if not path.exists():
        return ""
    try:
        return _protect(path.read_bytes(), decrypt=True).decode("utf-8").strip()
    except (OSError, UnicodeError, RuntimeError) as exc:
        raise RuntimeError(error_message) from exc


def save_nvidia_key(value: str, path=None):
    value = value.strip()
    if not value.startswith("nvapi-") or len(value) < 20:
        raise ValueError("Invalid NVIDIA key format")
    _save_key(value, DEFAULT_KEY_PATH, path=path)


def load_nvidia_key():
    return _load_key(
        "NVIDIA_API_KEY",
        "NVIDIA_API_KEY_FILE",
        DEFAULT_KEY_PATH,
        "Cannot read local NVIDIA credential; configure NVIDIA_API_KEY",
    )


def _validate_ascii_token(value: str, label: str, min_len: int = 16) -> str:
    cleaned = value.strip()
    if len(cleaned) < min_len or not cleaned.isascii() or any(ch.isspace() for ch in cleaned):
        raise ValueError(f"Invalid {label} key format")
    return cleaned


def save_dadata_key(value: str, path=None):
    """Encrypt and save a DaData API key using Windows DPAPI."""
    _save_key(_validate_ascii_token(value, "DaData", min_len=20), DEFAULT_DADATA_KEY_PATH, path=path)


def load_dadata_key():
    """Load DaData API key from environment override or encrypted local storage."""
    return _load_key(
        "DADATA_API_KEY",
        "DADATA_API_KEY_FILE",
        DEFAULT_DADATA_KEY_PATH,
        "Cannot read local DaData credential; configure DADATA_API_KEY",
    )


def save_yandex_maps_key(value: str, path=None):
    """Encrypt and save a Yandex Maps JS API key using Windows DPAPI."""
    _save_key(
        _validate_ascii_token(value, "Yandex Maps", min_len=16),
        DEFAULT_YANDEX_MAPS_KEY_PATH,
        path=path,
    )


def load_yandex_maps_key():
    """Load Yandex Maps API key from env override or encrypted local storage."""
    return _load_key(
        "YANDEX_MAPS_API_KEY",
        "YANDEX_MAPS_API_KEY_FILE",
        DEFAULT_YANDEX_MAPS_KEY_PATH,
        "Cannot read local Yandex Maps credential; configure YANDEX_MAPS_API_KEY",
    )


def save_dgis_maps_key(value: str, path=None):
    """Encrypt and save a 2GIS Maps API key using Windows DPAPI."""
    _save_key(
        _validate_ascii_token(value, "2GIS Maps", min_len=16),
        DEFAULT_DGIS_MAPS_KEY_PATH,
        path=path,
    )


def load_dgis_maps_key():
    """Load 2GIS Maps API key from env override or encrypted local storage."""
    return _load_key(
        "DGIS_MAPS_API_KEY",
        "DGIS_MAPS_API_KEY_FILE",
        DEFAULT_DGIS_MAPS_KEY_PATH,
        "Cannot read local 2GIS Maps credential; configure DGIS_MAPS_API_KEY",
    )
