"""Load proxy candidates from a local XLSX source file."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

from openpyxl import load_workbook

IP_PORT_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}:\d{2,5}$")

_SERVER_HEADERS = {
    "proxy",
    "proxy_server",
    "proxy_url",
    "server",
    "url",
}
_HOST_HEADERS = {"host", "ip", "ip_address", "address"}
_PORT_HEADERS = {"port"}
_PROTOCOL_HEADERS = {"protocol", "scheme", "type"}
_USERNAME_HEADERS = {"username", "user"}
_PASSWORD_HEADERS = {"password", "pass"}


def _normalize_header(value: object) -> str:
    text = str(value or "").strip().lower()
    return text.replace(" ", "_").replace("-", "_")


def _normalize_protocol(value: object) -> str:
    text = str(value or "").strip().lower()
    if text == "https":
        return "https"
    if text == "socks4":
        return "socks4"
    if text == "socks5":
        return "socks5"
    return "http"


def _infer_protocol(server: str, fallback: Optional[str] = None) -> str:
    if "://" in server:
        return _normalize_protocol(server.split("://", 1)[0])
    return _normalize_protocol(fallback)


def _build_server(
    raw_server: object,
    host: object,
    port: object,
    row_protocol: object,
    fallback_protocol: Optional[str],
) -> Optional[str]:
    server = str(raw_server or "").strip()
    if server:
        if "://" in server:
            return server
        if IP_PORT_RE.match(server):
            return f"{_normalize_protocol(row_protocol or fallback_protocol)}://{server}"
        return None

    host_text = str(host or "").strip()
    port_text = str(port or "").strip()
    if not host_text or not port_text:
        return None
    if not port_text.isdigit():
        return None
    protocol = _normalize_protocol(row_protocol or fallback_protocol)
    return f"{protocol}://{host_text}:{port_text}"


def load_proxy_candidates_from_xlsx(
    path: str | Path,
    protocol: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[dict]:
    """Return de-duplicated proxy dicts from the configured XLSX file."""
    xlsx_path = Path(path)
    if not xlsx_path.exists():
        return []

    requested_protocol = _normalize_protocol(protocol)
    wb = load_workbook(xlsx_path, read_only=True, data_only=True)
    try:
        ws = wb.active
        rows = ws.iter_rows(values_only=True)
        header_row = next(rows, None)
        if not header_row:
            return []

        header_index: Dict[str, int] = {
            _normalize_header(value): idx for idx, value in enumerate(header_row)
        }

        server_idx = next(
            (header_index[name] for name in _SERVER_HEADERS if name in header_index),
            None,
        )
        host_idx = next(
            (header_index[name] for name in _HOST_HEADERS if name in header_index),
            None,
        )
        port_idx = next(
            (header_index[name] for name in _PORT_HEADERS if name in header_index),
            None,
        )
        protocol_idx = next(
            (header_index[name] for name in _PROTOCOL_HEADERS if name in header_index),
            None,
        )
        username_idx = next(
            (header_index[name] for name in _USERNAME_HEADERS if name in header_index),
            None,
        )
        password_idx = next(
            (header_index[name] for name in _PASSWORD_HEADERS if name in header_index),
            None,
        )

        if server_idx is None and (host_idx is None or port_idx is None):
            return []

        proxies: List[dict] = []
        seen = set()

        for row in rows:
            raw_protocol = row[protocol_idx] if protocol_idx is not None and protocol_idx < len(row) else None
            server = _build_server(
                row[server_idx] if server_idx is not None and server_idx < len(row) else None,
                row[host_idx] if host_idx is not None and host_idx < len(row) else None,
                row[port_idx] if port_idx is not None and port_idx < len(row) else None,
                raw_protocol,
                requested_protocol,
            )
            if not server or server in seen:
                continue

            entry_protocol = _infer_protocol(server, raw_protocol or requested_protocol)
            if protocol and entry_protocol != requested_protocol:
                continue

            proxy = {"server": server}
            if username_idx is not None and username_idx < len(row) and row[username_idx]:
                proxy["username"] = str(row[username_idx]).strip()
            if password_idx is not None and password_idx < len(row) and row[password_idx]:
                proxy["password"] = str(row[password_idx]).strip()

            seen.add(server)
            proxies.append(proxy)

            if limit is not None and len(proxies) >= max(1, int(limit)):
                break

        return proxies
    finally:
        wb.close()
