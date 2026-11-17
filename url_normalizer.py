import urllib.parse
import ipaddress
import socket
from functools import lru_cache
from typing import Optional, Union

def _try_parse_numeric_ip(host: str) -> Optional[ipaddress.IPv4Address]:
    host = host.strip("[]")
    # Decimal or hex integer: e.g. "2130706433", "0x7f000001"
    try:
        if host.startswith(("0x", "0X")) or (host.isdigit() and len(host) > 4):
            val = int(host, 16) if host.startswith(("0x", "0X")) else int(host, 10)
            if 0 <= val <= 0xFFFFFFFF:
                return ipaddress.IPv4Address(val)
    except Exception:
        pass

    # Dotted quad with decimal, octal, or hex parts: e.g. "0177.0.0.1", "0x7f.0.0.1"
    parts = host.split(".")
    if len(parts) == 4:
        try:
            byte_vals = []
            for p in parts:
                if p.startswith(("0x", "0X")):
                    val = int(p, 16)
                elif len(p) > 1 and p.startswith("0") and p.isdigit():
                    val = int(p, 8)
                else:
                    val = int(p, 10)
                if 0 <= val <= 255:
                    byte_vals.append(val)
                else:
                    return None
            return ipaddress.IPv4Address(bytes(byte_vals))
        except Exception:
            pass
    return None

def _is_prohibited_ip(ip: Union[ipaddress.IPv4Address, ipaddress.IPv6Address], allow_local: bool = False) -> bool:
    if ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return True
    if ip.is_loopback or ip.is_private:
        if not allow_local:
            return True
    # Handle IPv4-mapped IPv6: ::ffff:127.0.0.1 or ::ffff:7f00:1
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        mapped = ip.ipv4_mapped
        if mapped.is_link_local or mapped.is_multicast or mapped.is_reserved or mapped.is_unspecified:
            return True
        if mapped.is_loopback or mapped.is_private:
            if not allow_local:
                return True
    return False

@lru_cache(maxsize=32768)
def _resolve_and_check_hostname(hostname: str, allow_local: bool = False) -> bool:
    if not hostname:
        return False

    # Unquote percent-encoded hostnames up to 2 times
    h_clean = urllib.parse.unquote(hostname).strip()
    if "%" in h_clean:
        h_clean = urllib.parse.unquote(h_clean).strip()
    
    h_lower = h_clean.lower().strip("[]")
    
    # RFC 6761 localhost checks
    if h_lower in ["localhost", "0.0.0.0", "::1", "127.0.0.1", "ip6-localhost", "ip6-loopback"] or h_lower.endswith(".localhost"):
        return True if allow_local else False

    # 1. Try alternative numeric/hex/octal IPv4
    numeric_ip = _try_parse_numeric_ip(h_lower)
    if numeric_ip:
        return not _is_prohibited_ip(numeric_ip, allow_local=allow_local)

    # 2. Try direct standard IP parsing
    try:
        ip = ipaddress.ip_address(h_lower)
        return not _is_prohibited_ip(ip, allow_local=allow_local)
    except ValueError:
        pass
        
    # 3. Resolve DNS and inspect all returned IP addresses
    try:
        addr_info = socket.getaddrinfo(h_clean, None)
        if not addr_info:
            return False
        for item in addr_info:
            ip_str = item[4][0]
            ip = ipaddress.ip_address(ip_str)
            if _is_prohibited_ip(ip, allow_local=allow_local):
                return False
        return True
    except Exception:
        return False

class URLNormalizer:
    @staticmethod
    def is_safe_url(url: str, allow_local: bool = False) -> bool:
        try:
            if not url or not isinstance(url, str):
                return False
            parsed = urllib.parse.urlsplit(url.strip())
            if parsed.scheme.lower() not in ["http", "https"]:
                return False
                
            hostname = parsed.hostname
            if not hostname:
                return False
                
            return _resolve_and_check_hostname(hostname, allow_local=allow_local)
        except Exception:
            return False

    @staticmethod
    def normalize(href: str) -> str:
        href = href.strip()
        parsed = urllib.parse.urlparse(href)
        normalized = parsed._replace(fragment="").geturl()
        return normalized

    @staticmethod
    def detect_language(url: str) -> str:
        url = url.lower()
        if "/hindi/" in url or "/hi/" in url: return "hi"
        if "/telugu/" in url or "/te/" in url: return "te"
        if "/marathi/" in url or "/mr/" in url: return "mr"
        if "/tamil/" in url or "/ta/" in url: return "ta"
        return "en"
