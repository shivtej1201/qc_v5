"""Bot-challenge detection (blueprint §10.3). On a hit we stop, never retry or solve."""
from __future__ import annotations

from typing import Mapping, Optional

MARKERS = ("_abck", "ak_bmsc", "bm_sz", "cf-chl", "cf_chl", "just a moment...", "attention required",
           "px-captcha", "_incapsula_resource", "datadome", "verify you are human", "access denied",
           "request unsuccessful", "pardon our interruption")
INTERSTITIAL = ("just a moment...", "verify you are human", "px-captcha", "checking your browser",
                "pardon our interruption")


def vendor_of(headers: Mapping[str, str]) -> Optional[str]:
    h = {str(k).lower(): str(v).lower() for k, v in headers.items()}
    server, cookies = h.get("server", ""), h.get("set-cookie", "")
    if "akamai" in server or "_abck" in cookies or "ak_bmsc" in cookies or "akamai-grn" in h:
        return "akamai"
    if "cloudflare" in server or "cf-ray" in h:
        return "cloudflare"
    if "x-iinfo" in h or "incap_ses" in cookies:
        return "imperva"
    if "x-datadome" in h or "datadome" in cookies:
        return "datadome"
    return None


def detect(status: Optional[int], headers: Mapping[str, str], body_head: str = "") -> Optional[str]:
    """Vendor name / 'challenge' / 'interstitial' when the response is a bot challenge, else None.
    Generic words count only with a 403/429/503, so ordinary pages mentioning 'access denied' pass."""
    text = (body_head or "")[:20_000].lower()
    vendor = vendor_of(headers)
    if status in (403, 429, 503) and (vendor or any(m in text for m in MARKERS)):
        return vendor or "challenge"
    if status is not None and status < 300 and any(m in text for m in INTERSTITIAL):
        return vendor or "interstitial"
    return None


def allowlist_rule(vendor: str, header: str, token: str, ips: list[str], rps: int = 40) -> str:
    """Text the CDN team can act on (blueprint §7.4). The token appears only here, for the site owner."""
    ip_part = f" and source IP {', '.join(ips)}" if ips else ""
    if vendor == "cloudflare":
        return (f"Cloudflare: create a WAF custom rule 'Allow QC Audit' with expression "
                f"(http.request.headers[\"{header.lower()}\"][0] eq \"{token}\"){' and ip.src in {' + ' '.join(ips) + '}' if ips else ''}, "
                f"action Skip (all remaining custom rules, rate limiting, Bot Fight Mode). Rate cap {rps} req/s.")
    return (f"Akamai Bot Manager: create a custom bot category 'QC Audit' matching request header {header} equals "
            f"{token}{ip_part}; action Allow; exclude it from rate controls up to {rps} req/s.")
