class StatusClassifier:
    @staticmethod
    def classify(status_code: int) -> str:
        if not status_code:
            return "Failed"
        if 200 <= status_code <= 299:
            return "Working"
        if 300 <= status_code <= 399:
            return "Redirect"
        if status_code == 400:
            return "Bad Request"
        if status_code == 401:
            return "Unauthorized"
        if status_code == 403:
            return "Forbidden"
        if status_code == 404:
            return "Broken URL"
        if status_code == 410:
            return "Gone"
        if status_code == 429:
            return "Rate Limited"
        if 500 <= status_code <= 599:
            return "Server Error"
        return "Unknown"


# ---------------------------------------------------------------------------------------------
# URL-status audit outcome taxonomy (one bucket per submitted URL)
# ---------------------------------------------------------------------------------------------
# validated = 1 -> an authoritative HTTP response was obtained (any status code)
# validated = 0 -> the URL could not be validated; error_type/error_message record why
OUTCOMES_VALIDATED = ("OK", "REDIRECT", "CLIENT_ERROR", "BLOCKED", "SERVER_ERROR")
OUTCOMES_FAILED = (
    "TIMEOUT", "CONNECTION_ERROR", "DNS_ERROR", "SSL_ERROR", "REDIRECT_LOOP",
    "NETWORK_ERROR", "BLOCKED_SSRF", "INVALID_URL", "VALIDATION_ERROR",
)

_ERROR_TYPE_OUTCOME = {
    "Timeout": "TIMEOUT",
    "Connection Error": "CONNECTION_ERROR",
    "DNS Error": "DNS_ERROR",
    "SSL Error": "SSL_ERROR",
    "Redirect Loop": "REDIRECT_LOOP",
    "Blocked (SSRF)": "BLOCKED_SSRF",
    "Invalid URL": "INVALID_URL",
    "Validation Error": "VALIDATION_ERROR",
    "BROWSER_CHALLENGE_BLOCKED": "BLOCKED",
}


def classify_outcome(final_status_code, initial_status_code=None, redirect_count=0,
                     error_type: str = "", browser_verified: str = "NO"):
    """Return (outcome, validated) for one URL-status result.
    Redirects keep the REDIRECT outcome; the post-redirect status stays in final_status_code."""
    if browser_verified == "YES":
        return "OK", 1
    if error_type:
        oc = _ERROR_TYPE_OUTCOME.get(error_type)
        if oc == "BLOCKED":
            return "BLOCKED", 1 if final_status_code else 0
        if oc:
            return oc, 0
        # e.g. BROWSER_HTTP_ERROR: the browser got an HTTP status -> classify by code below
        if not final_status_code:
            return "NETWORK_ERROR", 0
    code = final_status_code or 0
    if not code:
        return "NETWORK_ERROR", 0
    if (redirect_count or 0) > 0 or (initial_status_code and 300 <= initial_status_code <= 399):
        return "REDIRECT", 1
    if 200 <= code <= 299:
        return "OK", 1
    if 300 <= code <= 399:
        return "REDIRECT", 1
    if code in (401, 403, 429):
        return "BLOCKED", 1
    if 400 <= code <= 499:
        return "CLIENT_ERROR", 1
    if 500 <= code <= 599:
        return "SERVER_ERROR", 1
    return "NETWORK_ERROR", 0
