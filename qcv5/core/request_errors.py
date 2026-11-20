"""Transport-exception classification shared by the URL checker and the on-page engine
(type-based first; message only as a tie-breaker)."""
import httpx


class SSRFRedirectBlocked(Exception):
    """A redirect hop pointed at a prohibited (private / link-local / metadata) address."""


def classify_request_exception(e: BaseException) -> str:
    """Map a transport exception to a QC V5 error_type using the exception TYPE first and the
    message only as a tie-breaker (httpx DNS failures say e.g. 'Name or service not known')."""
    for exc in (e, getattr(e, "__cause__", None), getattr(e, "__context__", None)):
        if isinstance(exc, SSRFRedirectBlocked):
            return "Blocked (SSRF)"
    if isinstance(e, httpx.TooManyRedirects):
        return "Redirect Loop"
    if isinstance(e, httpx.TimeoutException):
        return "Timeout"
    msg = str(e).lower()
    if "ssl" in msg or "certificate" in msg or "tls" in msg:
        return "SSL Error"
    if any(k in msg for k in ("name or service not known", "nodename nor servname", "getaddrinfo",
                              "name resolution", "no address associated", "name_not_resolved", "dns")):
        return "DNS Error"
    if isinstance(e, httpx.ConnectError) or "connect" in msg or "refused" in msg:
        return "Connection Error"
    if "timeout" in msg or "timed out" in msg:
        return "Timeout"
    return "Network Error"
