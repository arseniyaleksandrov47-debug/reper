import ctypes
import copy
from datetime import datetime, timezone
import time
import logging
import re
import select
import socket
import struct
import sys
import threading
from urllib.parse import urlsplit
from abc import ABC, abstractmethod
from typing import List, Optional, Dict, Any
from curl_cffi import requests as curl_requests
from ..models import CanonicalProperty
from ..rate_limiter import AdaptiveRateLimiter

logger = logging.getLogger("BaseCollector")
_IP_UNICAST_IF = 31


def preserve_source_fields(item, attributes):
    """Keep the received listing fields, independent of current normalizers."""
    return dict(attributes,source_payload=copy.deepcopy(item),
                source_fetched_at=datetime.now(timezone.utc).isoformat())


class _DirectInterfaceBridge:
    _instance: Optional["_DirectInterfaceBridge"] = None
    _lock = threading.Lock()

    def __init__(self, if_index: int):
        self.if_index = if_index
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(128)
        self.port = self._srv.getsockname()[1]
        self.proxy_url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self._accept_loop, daemon=True).start()
        logger.info(f"[DirectBridge] Bound to 127.0.0.1:{self.port} (if_index={self.if_index})")

    @classmethod
    def get_proxy_url(cls) -> Optional[str]:
        if sys.platform != "win32":
            return None
        with cls._lock:
            if cls._instance is not None:
                return cls._instance.proxy_url
            if_index = cls._detect_physical_if_index()
            if if_index is None:
                return None
            cls._instance = cls(if_index)
            return cls._instance.proxy_url

    @staticmethod
    def _detect_physical_if_index() -> Optional[int]:
        try:
            class MIB_IPADDRROW(ctypes.Structure):
                _fields_ = [
                    ("dwAddr", ctypes.c_ulong),
                    ("dwIndex", ctypes.c_ulong),
                    ("dwMask", ctypes.c_ulong),
                    ("dwBCastAddr", ctypes.c_ulong),
                    ("dwReasmSize", ctypes.c_ulong),
                    ("unused1", ctypes.c_ushort),
                    ("wType", ctypes.c_ushort),
                ]

            iphlpapi = ctypes.windll.iphlpapi
            size = ctypes.c_ulong(0)
            iphlpapi.GetIpAddrTable(None, ctypes.byref(size), 0)
            buf = ctypes.create_string_buffer(size.value)
            if iphlpapi.GetIpAddrTable(buf, ctypes.byref(size), 0) != 0:
                return None
            num_entries = struct.unpack_from("I", buf.raw, 0)[0]
            offset = 4
            for _ in range(num_entries):
                row = MIB_IPADDRROW.from_buffer_copy(buf.raw, offset)
                offset += ctypes.sizeof(MIB_IPADDRROW)
                ip = socket.inet_ntoa(struct.pack("<I", row.dwAddr))
                if ip.startswith("127.") or ip.startswith("169.254."):
                    continue
                if (row.wType & 4) and not ip.endswith(".1"):
                    return int(row.dwIndex)
        except Exception:
            pass
        return None

    def _accept_loop(self) -> None:
        while True:
            try:
                c, _ = self._srv.accept()
                threading.Thread(target=self._handle_client, args=(c,), daemon=True).start()
            except Exception:
                break

    def _handle_client(self, client_sock: socket.socket) -> None:
        rem_sock: Optional[socket.socket] = None
        try:
            client_sock.settimeout(12)
            data = b''
            while b'\r\n\r\n' not in data:
                chunk = client_sock.recv(4096)
                if not chunk or len(data) + len(chunk) > 65536:
                    return
                data += chunk
            header, pending = data.split(b'\r\n\r\n', 1)
            lines = header.split(b'\r\n')
            parts = lines[0].decode('latin1').split(' ')
            if len(parts) != 3 or not parts[2].startswith('HTTP/'):
                return
            tunnel = parts[0].upper() == 'CONNECT'
            target = urlsplit(('//' if tunnel else '') + parts[1])
            if not target.hostname or target.username or target.password or (not tunnel and target.scheme != 'http'):
                return
            host = target.hostname
            port = target.port or (443 if tunnel else 80)
            rem_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            rem_sock.settimeout(12)
            rem_sock.setsockopt(socket.IPPROTO_IP, _IP_UNICAST_IF, struct.pack(">I", self.if_index))
            rem_sock.connect((socket.gethostbyname(host), port))
            if tunnel:
                client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if pending:
                    rem_sock.sendall(pending)
            else:
                path = (target.path or '/') + ('?' + target.query if target.query else '')
                first = f'{parts[0]} {path} {parts[2]}'.encode('latin1')
                headers = [line for line in lines[1:] if line.split(b':', 1)[0].lower()
                           not in (b'proxy-connection', b'proxy-authorization', b'connection', b'host')]
                headers += [f'Host: {target.netloc}'.encode('latin1'), b'Connection: close']
                rem_sock.sendall(b'\r\n'.join([first, *headers]) + b'\r\n\r\n' + pending)
            sockets = [client_sock, rem_sock]
            while True:
                # The HTTP client owns its request deadline. A slow public
                # query may legitimately send no bytes for more than 12 s.
                r, _, _ = select.select(sockets, [], [], 60)
                if not r:
                    break
                for s in r:
                    chunk = s.recv(32768)
                    if not chunk:
                        return
                    (rem_sock if s is client_sock else client_sock).sendall(chunk)
        except Exception:
            pass
        finally:
            try:
                client_sock.close()
            except Exception:
                pass
            if rem_sock:
                try:
                    rem_sock.close()
                except Exception:
                    pass


def extract_specs_from_text(text: str) -> Dict[str, Any]:
    res: Dict[str, Any] = {"power_kw": None, "has_separate_entrance": False}
    if not text:
        return res
    low = text.lower()
    if any(p in low for p in ("отдельный вход", "отдельных входа", "вход с улицы", "первая линия")):
        res["has_separate_entrance"] = True
    m_kw = re.search(r"(\d+(?:[.,]\d+)?)\s*квт", low)
    if m_kw:
        try:
            val = float(m_kw.group(1).replace(",", "."))
            if 3.0 <= val <= 5000.0:
                res["power_kw"] = val
        except ValueError:
            pass
    return res


class BaseCollector(ABC):
    def __init__(
        self,
        name: str,
        domain: str,
        proxy: Optional[str] = None,
        rate_limiter: Optional[AdaptiveRateLimiter] = None,
        impersonate: str = "chrome120",
        verify: bool = False,
    ):
        self.name = name
        self.domain = domain
        self.proxy = proxy or _DirectInterfaceBridge.get_proxy_url()
        self.rate_limiter = rate_limiter or AdaptiveRateLimiter()
        self.session = curl_requests.Session(
            impersonate=impersonate,
            proxies=self.build_proxy_dict(),
            verify=verify,
        )

    @abstractmethod
    async def collect(self, limit: int = 50) -> List[CanonicalProperty]:
        pass

    def build_proxy_dict(self) -> Optional[Dict[str, str]]:
        if self.proxy:
            return {"http": self.proxy, "https": self.proxy}
        return None

    async def pace(self, min_delay: float = None, max_delay: float = None):
        await self.rate_limiter.wait_before_request(self.domain, min_delay=min_delay, max_delay=max_delay)

    def begin_collection(self, limit: int):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit должен быть целым числом не меньше 1")
        self.collection_warnings = []
        self.http_statuses = []
        self.parsed_pages = 0
        self.parse_error_count = 0
        self.access_issue = None

    def source_access_ready(self):
        """Do not repeat a blocked collection while its session is cooling down."""
        remaining=getattr(self,'_access_pause_until',0)-time.monotonic()
        if remaining<=0:return True
        issue={**self._last_access_issue,'retry_after_s':round(remaining,1),'cooldown':True}
        self.access_issue=issue
        self.record_collection_error('Повторный сбор приостановлен после ограничения доступа источником')
        return False

    def record_access_issue(self,response,*,has_data=False):
        from .access_state import response_access_issue
        issue=response_access_issue(response,has_data=has_data)
        if issue is None:return False
        self.access_issue=issue;self._last_access_issue=issue
        self._access_pause_until=time.monotonic()+issue['retry_after_s']
        messages={'captcha':'Источник вернул CAPTCHA вместо объявлений; требуется подтверждение доступа',
            'forbidden':'Источник запретил автоматический запрос (HTTP 403)',
            'rate_limited':'Источник ограничил частоту запросов (HTTP 429)',
            'security_challenge':'Проверка безопасности источника не завершена (HTTP 439)'}
        self.record_collection_error(messages[issue['kind']])
        return True

    def record_collection_error(self, message: str):
        if message not in self.collection_warnings:
            self.collection_warnings.append(message)

    def record_parsed_page(self):
        self.parsed_pages += 1

    def record_parse_error(self):
        self.parse_error_count += 1
        self.record_collection_error("Часть объявлений не удалось разобрать")

    def collection_summary(self, collected: int):
        warnings = list(getattr(self, "collection_warnings", []))
        parsed_pages = getattr(self, "parsed_pages", 0)
        if warnings:
            status = "partial" if collected else "unavailable"
        elif parsed_pages:
            status = "ok" if collected else "empty"
        else:
            status = "unknown"
        return {"status": status, "collected": collected, "parsed_pages": parsed_pages,
                "http_statuses": list(getattr(self, "http_statuses", [])), "warnings": warnings,
                # A capped search is not evidence that unseen old advertisements were withdrawn.
                "coverage_complete": False,
                "parse_error_count": getattr(self, "parse_error_count", 0),
                **({'access_issue':self.access_issue} if getattr(self,'access_issue',None) else {})}

    def record_status(self, status_code: int):
        self.http_statuses.append(status_code)
        if status_code != 200:
            self.record_collection_error(f"HTTP {status_code}")
        self.rate_limiter.record_status(self.domain, status_code)
