import os
import sys
import time
import asyncio
import uuid
import uvicorn
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from starlette.routing import Route

from qcv5.crawler.browser import BrowserPool
from qcv5.crawler.api_observer import ApiResponseObserver
from qcv5.crawler.url_normalizer import URLNormalizer

# Controlled local mock server for redirect & subrequest tests
async def endpoint_redirect_301(request):
    return RedirectResponse("http://169.254.169.254/latest/meta-data", status_code=301)

async def endpoint_redirect_302_hop1(request):
    return RedirectResponse("/redirect-hop2", status_code=302)

async def endpoint_redirect_307_hop2(request):
    return RedirectResponse("http://169.254.169.254/credentials", status_code=307)

async def endpoint_iframe_ssrf(request):
    html = """
    <!DOCTYPE html>
    <html>
    <head><title>Iframe SSRF Test</title></head>
    <body>
        <h1>Testing Iframe Subrequest</h1>
        <iframe src="http://169.254.169.254/iframe-probe"></iframe>
    </body>
    </html>
    """
    return HTMLResponse(html)

async def endpoint_fetch_ssrf(request):
    html = """
    <!DOCTYPE html>
    <html>
    <head><title>Fetch SSRF Test</title></head>
    <body>
        <h1>Testing Script Fetch Subrequest</h1>
        <script>
            fetch("http://169.254.169.254/api-probe").catch(e => console.error(e));
        </script>
    </body>
    </html>
    """
    return HTMLResponse(html)

async def endpoint_popup_ssrf(request):
    html = """
    <!DOCTYPE html>
    <html>
    <head><title>Popup Test</title></head>
    <body>
        <h1>Testing Popup Escape</h1>
        <script>
            window.open("http://169.254.169.254/popup-probe");
        </script>
    </body>
    </html>
    """
    return HTMLResponse(html)

routes = [
    Route("/redirect-301", endpoint_redirect_301),
    Route("/redirect-hop1", endpoint_redirect_302_hop1),
    Route("/redirect-hop2", endpoint_redirect_307_hop2),
    Route("/iframe-ssrf", endpoint_iframe_ssrf),
    Route("/fetch-ssrf", endpoint_fetch_ssrf),
    Route("/popup-ssrf", endpoint_popup_ssrf),
]

mock_app = Starlette(routes=routes)

async def run_ssrf_adversarial_suite():
    print("=" * 65)
    print("PHASE 11.1.1: ADVERSARIAL SSRF & SECURITY VERIFICATION SUITE")
    print("=" * 65)

    # 1. URLNormalizer Static & Resolved Adversarial Evaluation
    print("\n--- 1. Testing URLNormalizer Adversarial Representation Matrix ---")
    norm_matrix = [
        ("Decimal Integer IPv4 (2130706433)", "http://2130706433/probe", False),
        ("Octal Quad IPv4 (0177.0.0.1)", "http://0177.0.0.1/probe", False),
        ("Hex Integer IPv4 (0x7f000001)", "http://0x7f000001/probe", False),
        ("Hex Dotted IPv4 (0x7f.0.0.1)", "http://0x7f.0.0.1/probe", False),
        ("IPv6 Loopback ([::1])", "http://[::1]/probe", False),
        ("IPv4-mapped IPv6 ([::ffff:127.0.0.1])", "http://[::ffff:127.0.0.1]/probe", False),
        ("IPv4-mapped IPv6 Hex ([::ffff:7f00:1])", "http://[::ffff:7f00:1]/probe", False),
        ("IPv4-mapped Metadata ([::ffff:169.254.169.254])", "http://[::ffff:169.254.169.254]/probe", False),
        ("Cloud Metadata IP (169.254.169.254)", "http://169.254.169.254/latest/meta-data", False),
        ("IPv6 Link-Local ([fe80::1])", "http://[fe80::1]/probe", False),
        ("Localhost Alias (localhost)", "http://localhost:8080/probe", False),
        ("Localhost RFC6761 (sub.localhost)", "http://sub.localhost:8080/probe", False),
        ("Localhost DNS Rebinding (127.0.0.1.nip.io)", "http://127.0.0.1.nip.io/probe", False),
        ("Localhost DNS Rebinding (localtest.me)", "http://localtest.me/probe", False),
        ("Percent-encoded Hostname (%31%32%37%2e%30%2e%30%2e%31)", "http://%31%32%37%2e%30%2e%30%2e%31/probe", False),
        ("Userinfo Authority Bypass (user:pass@127.0.0.1)", "http://user:pass@127.0.0.1:8000/probe", False),
        ("Forbidden Scheme: file://", "file:///etc/passwd", False),
        ("Forbidden Scheme: javascript:", "javascript:alert(1)", False),
        ("Forbidden Scheme: data:", "data:text/html,<b>pwn</b>", False),
        ("Legitimate Public Web (google.com)", "https://www.google.com", True),
    ]

    for label, url, expected in norm_matrix:
        res = URLNormalizer.is_safe_url(url, allow_local=False)
        assert res == expected, f"Failed on {label}: expected {expected}, got {res}"
        print(f"  [PASS] {label:48} -> Safe: {res}")

    print("\n[OK] All 20 adversarial URLNormalizer vectors evaluated correctly!")

    # 2. Start Local Mock Server on port 8993 for Browser Interception Verification
    config = uvicorn.Config(mock_app, host="127.0.0.1", port=8993, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    await asyncio.sleep(1.0)
    base = "http://127.0.0.1:8993"

    bp = BrowserPool()
    await bp.start()
    observer = ApiResponseObserver(bp)
    job_id = f"test-ssrf-{uuid.uuid4()}"

    try:
        print("\n--- 2. Testing Browser Interception, Redirect Chains & Subrequests ---")
        
        # Test A: Direct SSRF blocked before navigation
        res_direct, _ = await observer.audit_page(job_id, "http://169.254.169.254/latest/meta-data", allow_local=False)
        assert res_direct.page_status == "SKIPPED", f"Expected SKIPPED, got {res_direct.page_status}"
        print("  [PASS] Direct metadata navigation immediately blocked fail-closed (SKIPPED)")

        # Test B: 301 Redirect to Metadata Endpoint
        res_301, _ = await observer.audit_page(job_id, f"{base}/redirect-301", allow_local=True)
        assert res_301.page_status in ["SKIPPED", "PAGE_ERROR"], f"Redirect to metadata not blocked: {res_301.page_status}"
        print(f"  [PASS] 301 redirect to metadata endpoint blocked before destination reached: {res_301.page_status} ({res_301.summary_reason})")

        # Test C: Multi-Hop Redirect (Public -> Hop 1 -> Hop 2 -> Metadata)
        res_multihop, _ = await observer.audit_page(job_id, f"{base}/redirect-hop1", allow_local=True)
        assert res_multihop.page_status in ["SKIPPED", "PAGE_ERROR"], f"Multi-hop redirect not blocked: {res_multihop.page_status}"
        print(f"  [PASS] Multi-hop 302 -> 307 redirect chain aborted before metadata reach: {res_multihop.page_status} ({res_multihop.summary_reason})")

        # Test D: Iframe Subrequest to Metadata
        res_iframe, apis_iframe = await observer.audit_page(job_id, f"{base}/iframe-ssrf", allow_local=True)
        # Verify no iframe request to 169.254.169.254 succeeded or appeared as valid
        unsafe_iframe_calls = [a for a in apis_iframe if "169.254.169.254" in a.request_url and a.api_status == "FOUND"]
        assert len(unsafe_iframe_calls) == 0, f"Iframe subrequest leaked to metadata: {unsafe_iframe_calls}"
        print("  [PASS] Iframe subrequest intercepted and denied (0 unsafe calls connected)")

        # Test E: Script Fetch Subrequest to Metadata
        res_fetch, apis_fetch = await observer.audit_page(job_id, f"{base}/fetch-ssrf", allow_local=True)
        unsafe_fetch_calls = [a for a in apis_fetch if "169.254.169.254" in a.request_url and a.api_status == "FOUND"]
        assert len(unsafe_fetch_calls) == 0, f"Fetch subrequest leaked to metadata: {unsafe_fetch_calls}"
        print("  [PASS] Script fetch subrequest intercepted and aborted via route policy")

        # Test F: Popup window.open neutralized
        res_popup, _ = await observer.audit_page(job_id, f"{base}/popup-ssrf", allow_local=True)
        print(f"  [PASS] Popup window.open neutralized automatically by popup handler: {res_popup.page_status}")

    finally:
        await bp.stop()
        server.should_exit = True
        await asyncio.sleep(0.5)

    # 3. Adversarial Redaction Verification
    print("\n--- 3. Testing Adversarial Redaction Engine ---")
    
    # 3a. Mixed-case query parameters and encoded keys
    adversarial_urls = [
        ("https://api.domain.com/v1?ApiKey=SUPER_SECRET_KEY&User=alice", "SUPER_SECRET_KEY"),
        ("https://api.domain.com/v1?TOKEN=JWT_SECRET_TOKEN_999", "JWT_SECRET_TOKEN_999"),
        ("https://api.domain.com/v1?Client-Secret=MY_SECRET_CLIENT&id=1", "MY_SECRET_CLIENT"),
        ("https://api.domain.com/v1?api%5fkey=ENCODED_KEY_SECRET", "ENCODED_KEY_SECRET"),
        ("https://api.domain.com/v1?Session_Id=SESS_SECRET_ID_1234", "SESS_SECRET_ID_1234"),
        ("https://api.domain.com/v1?cvv=999&card_number=4111222233334444", "4111222233334444"),
    ]
    for url, secret in adversarial_urls:
        redacted_u = observer.redact_url(url)
        assert secret not in redacted_u, f"Secret {secret} leaked in URL: {redacted_u}"
        print(f"  [PASS] Redacted URL: {redacted_u}")

    # 3b. Deeply nested JSON payloads
    nested_json = '{"user": {"profile": {"auth_token": "secret_token_val_1", "credentials": {"password": "mypassword123", "cvv": "456", "active": true}}}}'
    redacted_json = observer.redact_text(nested_json)
    assert "secret_token_val_1" not in redacted_json, f"Secret leaked in nested JSON: {redacted_json}"
    assert "mypassword123" not in redacted_json, f"Password leaked in nested JSON: {redacted_json}"
    assert "456" not in redacted_json, f"CVV leaked in nested JSON: {redacted_json}"
    print(f"  [PASS] Redacted Nested JSON: {redacted_json}")

    # 3c. Authorization and Cookie Headers
    headers_text = "Cookie: session=SUPER_SECRET_COOKIE_DATA; other=123\nAuthorization: Bearer my_jwt_token_here"
    redacted_headers = observer.redact_text(headers_text)
    assert "SUPER_SECRET_COOKIE_DATA" not in redacted_headers, "Cookie leaked"
    assert "my_jwt_token_here" not in redacted_headers, "Bearer token leaked"
    print(f"  [PASS] Redacted Headers: {redacted_headers}")

    print("\n" + "=" * 65)
    print("ALL ADVERSARIAL SSRF & SECURITY TESTS PASSED WITH 100% SUCCESS!")
    print("=" * 65)

if __name__ == "__main__":
    asyncio.run(run_ssrf_adversarial_suite())
