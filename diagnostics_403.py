import re
import json
import html
from typing import Dict, Any, Optional, List

# List of safe HTTP headers that may be logged for diagnostics without privacy risk
SAFE_HEADER_KEYS = {
    "server", "cf-ray", "cf-cache-status", "x-amz-cf-id", "x-amz-cf-pop", 
    "x-sucuri-id", "x-sucuri-cache", "x-cache", "x-cache-lookup", "via",
    "retry-after", "vary", "content-type", "content-length", "date",
    "strict-transport-security", "x-frame-options", "x-content-type-options",
    "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
    "x-akamai-transformed", "x-akam-sw-version", "x-bfl-grn", "x-custom-check",
    "x-cdn", "x-edge-location", "x-edge-connect", "x-iinfo"
}

# Forbidden header keys that must NEVER be logged or leaked
FORBIDDEN_HEADER_KEYS = {
    "cookie", "set-cookie", "authorization", "proxy-authorization", 
    "x-api-key", "x-proxy-secret", "token", "secret"
}

class Diagnostics403:
    """
    HTTP 403 and access failure diagnostic engine.
    Categorizes the root cause of 403 Forbidden errors across sitemap retrieval,
    page crawling, redirects, and browser fallbacks.
    """

    @staticmethod
    def filter_safe_headers(headers: Dict[str, Any]) -> Dict[str, str]:
        """Filter headers to only known safe diagnostic headers, strictly omitting sensitive data."""
        safe = {}
        for k, v in headers.items():
            k_lower = k.lower().strip()
            if k_lower in FORBIDDEN_HEADER_KEYS:
                continue
            if k_lower in SAFE_HEADER_KEYS or k_lower.startswith(("x-cf-", "x-amz-", "x-sucuri-", "cf-", "x-akam-", "x-akamai-")):
                safe[k_lower] = str(v)[:256]
        return safe

    @staticmethod
    def inspect_html_body(body_snippet: str) -> Dict[str, bool]:
        """Detect known CDN, WAF, or CAPTCHA challenge indicators in HTML response snippet."""
        if not body_snippet:
            return {"challenge_detected": False, "waf_detected": False, "captcha_detected": False}
            
        unescaped_snippet = html.unescape(body_snippet[:4096]).lower()
        snippet_lower = body_snippet[:4096].lower()
        
        has_cloudflare = any(term in snippet_lower or term in unescaped_snippet for term in [
            "cf-browser-verification", "cf-chl-bypass", "challenge-platform", 
            "cloudflare-static", "turnstile", "cf-spinner", "please wait...|cloudflare",
            "checking your browser before accessing", "attention required! | cloudflare"
        ])
        
        has_aws_waf = any(term in snippet_lower or term in unescaped_snippet for term in [
            "awswaf", "x-amzn-waf-action", "aws waf"
        ])
        
        has_akamai = any(term in snippet_lower or term in unescaped_snippet for term in [
            "akamai ghost", "reference #", "reference&#", "access denied - akamai",
            "edgesuite.net", "errors.edgesuite.net", "akamaitechnologies.com",
            "you don't have permission to access"
        ])
        
        has_imperva = any(term in snippet_lower or term in unescaped_snippet for term in [
            "incapsula", "imperva", "_incident", "incapsula incident id"
        ])
        
        has_datadome = "datadome" in snippet_lower
        has_perimeterx = "perimeterx" in snippet_lower or "px-captcha" in snippet_lower
        
        has_captcha = any(term in snippet_lower for term in [
            "recaptcha", "hcaptcha", "turnstile", "captcha-form", "robot check",
            "verify you are human", "solving the challenge"
        ])
        
        has_generic_waf = any(term in snippet_lower for term in [
            "web application firewall", "security policy violation", "blocked by waf",
            "request blocked", "access denied by security rule"
        ])
        
        has_bot_block = any(term in snippet_lower for term in [
            "scripts should use", "bot policy", "automated requests", "crawler policy"
        ])
        
        waf_detected = has_cloudflare or has_aws_waf or has_akamai or has_imperva or has_datadome or has_perimeterx or has_generic_waf
        challenge_detected = has_captcha or has_cloudflare or has_datadome or has_perimeterx
        
        return {
            "challenge_detected": challenge_detected,
            "waf_detected": waf_detected,
            "captcha_detected": has_captcha,
            "bot_block_detected": has_bot_block,
            "cloudflare": has_cloudflare,
            "aws_waf": has_aws_waf,
            "akamai": has_akamai,
            "imperva": has_imperva,
            "datadome": has_datadome,
            "perimeterx": has_perimeterx
        }

    @classmethod
    def classify(
        cls,
        status_code: int,
        headers: Dict[str, Any],
        body_snippet: str = "",
        stage: str = "page_crawl",
        robots_allowed: bool = True,
        redirect_chain: Optional[List[Dict[str, Any]]] = None,
        error_type: str = ""
    ) -> Dict[str, Any]:
        """
        Produce a structured classification and user-facing explanation of the access failure.
        Categories:
        - ROBOTS_BLOCKED
        - ORIGIN_FORBIDDEN
        - CDN_WAF_BLOCKED_OR_CHALLENGED
        - AUTH_REQUIRED
        - RATE_LIMITED
        - REDIRECT_REJECTED
        - HTTP_403_UNCLASSIFIED
        - SITEMAP_XML_INVALID
        - SITEMAP_FETCH_FAILED
        - NETWORK_ERROR
        """
        safe_headers = cls.filter_safe_headers(headers)
        body_indicators = cls.inspect_html_body(body_snippet)
        
        # 1. Robots.txt block or Bot policy block
        if not robots_allowed or body_indicators.get("bot_block_detected"):
            return {
                "classification": "ROBOTS_BLOCKED",
                "evidence_type": "VERIFIED_EVIDENCE",
                "what_happened": "Access was blocked by the target website's robots.txt or automated crawler / User-Agent policy.",
                "where_it_happened": f"During {stage} request evaluation.",
                "what_verified": "Verified against target host's robots.txt or bot / User-Agent restriction notice.",
                "recommended_action": "Verify if the target URL path is disallowed in robots.txt or if origin requires an authorized crawler User-Agent.",
                "retry_appropriate": False,
                "safe_headers": safe_headers,
                "body_indicators": body_indicators
            }
            
        # 2. Redirect followed to prohibited target
        if redirect_chain and any(
            r.get("status") in [301, 302, 303, 307, 308] and "prohibited" in str(r.get("error", "")).lower() 
            for r in redirect_chain
        ):
            return {
                "classification": "REDIRECT_REJECTED",
                "evidence_type": "VERIFIED_EVIDENCE",
                "what_happened": "The request followed a redirect chain that terminated at a prohibited or unsafe destination.",
                "where_it_happened": f"During redirect execution in {stage}.",
                "what_verified": "Verified redirect chain headers and destination IP safety constraints.",
                "recommended_action": "Check the origin URL redirect configuration to ensure it targets a valid, public destination.",
                "retry_appropriate": False,
                "safe_headers": safe_headers,
                "body_indicators": body_indicators
            }

        # 3. Network or DNS or Timeout failure
        if error_type in ["DNS Error", "Timeout", "Connection Error", "SSL Error", "Network Error"]:
            is_sitemap = "sitemap" in stage.lower()
            return {
                "classification": "SITEMAP_FETCH_FAILED" if is_sitemap else "NETWORK_ERROR",
                "evidence_type": "VERIFIED_EVIDENCE",
                "what_happened": f"Network transport error encountered: {error_type}.",
                "where_it_happened": f"During {stage} network connection.",
                "what_verified": f"Connection socket failed: {error_type}.",
                "recommended_action": "Check host DNS resolution, SSL certificate validity, and origin server uptime. A retry may succeed for transient network issues.",
                "retry_appropriate": True,
                "safe_headers": safe_headers,
                "body_indicators": body_indicators
            }

        # 4. Sitemap XML Invalid
        if error_type == "SITEMAP_XML_INVALID":
            return {
                "classification": "SITEMAP_XML_INVALID",
                "evidence_type": "VERIFIED_EVIDENCE",
                "what_happened": "The sitemap payload was not valid XML or could not be decompressed.",
                "where_it_happened": f"During sitemap parsing in {stage}.",
                "what_verified": "XML parser encountered malformed syntax, unescaped characters, or corrupt gzip stream.",
                "recommended_action": "Validate the sitemap XML against the standard sitemaps.org schema, or upload the local XML file directly.",
                "retry_appropriate": False,
                "safe_headers": safe_headers,
                "body_indicators": body_indicators
            }

        # 5. Rate limiting (429 or 403 with RateLimit indicators)
        if status_code == 429 or (status_code == 403 and any(k in safe_headers for k in ["retry-after", "x-ratelimit-remaining", "x-ratelimit-limit"])):
            return {
                "classification": "RATE_LIMITED",
                "evidence_type": "VERIFIED_EVIDENCE",
                "what_happened": "The server or edge gateway throttled requests due to request rate limits.",
                "where_it_happened": f"During {stage} execution (HTTP {status_code}).",
                "what_verified": f"Status code {status_code} and rate-limit diagnostic headers present.",
                "recommended_action": "Reduce crawler concurrency or wait for the specified Retry-After window before retrying.",
                "retry_appropriate": True,
                "safe_headers": safe_headers,
                "body_indicators": body_indicators
            }

        # 6. Auth Required (401 or 403 with WWW-Authenticate header)
        if status_code == 401 or (status_code == 403 and "www-authenticate" in [k.lower() for k in headers.keys()]):
            return {
                "classification": "AUTH_REQUIRED",
                "evidence_type": "VERIFIED_EVIDENCE",
                "what_happened": "HTTP Authentication is required to access this resource.",
                "where_it_happened": f"During {stage} request evaluation.",
                "what_verified": "Server returned 401/403 with authentication challenge.",
                "recommended_action": "Provide authorized credentials or configure authorized intranet access for the crawler.",
                "retry_appropriate": False,
                "safe_headers": safe_headers,
                "body_indicators": body_indicators
            }

        # 7. CDN / WAF Blocked or Challenged
        # Evidence required: verified CDN header (cf-ray, server: cloudflare, x-amz-cf-id, etc.) OR body challenge indicator
        server_val = str(safe_headers.get("server", "")).lower()
        has_akamai_headers = any(k in safe_headers for k in ["x-akamai-transformed", "x-akam-sw-version", "x-bfl-grn"]) or any(k.startswith(("x-akam-", "x-akamai-")) for k in safe_headers)
        has_cdn_headers = (
            any(k in safe_headers for k in ["cf-ray", "cf-cache-status", "x-amz-cf-id", "x-sucuri-id"])
            or has_akamai_headers
            or any(c in server_val for c in ["cloudflare", "cloudfront", "akamai", "sucuri", "imperva"])
        )
        
        if status_code == 403 and (has_cdn_headers or body_indicators["waf_detected"] or body_indicators["challenge_detected"]):
            waf_name = "Cloudflare" if "cf-ray" in safe_headers or "cloudflare" in server_val or body_indicators.get("cloudflare") else \
                       "AWS CloudFront / WAF" if "x-amz-cf-id" in safe_headers or "cloudfront" in server_val or body_indicators.get("aws_waf") else \
                       "Akamai" if has_akamai_headers or "akamai" in server_val or body_indicators.get("akamai") else \
                       "Sucuri" if "x-sucuri-id" in safe_headers or body_indicators.get("imperva") else \
                       "CDN / Edge WAF"
                       
            is_challenge = body_indicators["challenge_detected"]
            what_happened = f"{waf_name} issued an interactive security challenge (CAPTCHA / JavaScript verification) and blocked automated crawling." if is_challenge else \
                            f"{waf_name} blocked the request due to an edge security rule or bot management policy."
            
            matched_evidence = [k for k in safe_headers if k in ['cf-ray', 'x-amz-cf-id', 'server', 'x-akam-sw-version', 'x-bfl-grn'] or k.startswith(('x-akam-', 'x-akamai-'))]
            if body_indicators.get("akamai") and "Akamai" in waf_name:
                matched_evidence.append("Akamai Edge Reference / EdgeSuite signature in response body")
            evidence_str = f" ({', '.join(matched_evidence)})" if matched_evidence else ""
            what_verified = f"Verified edge signature: {waf_name} indicators present{evidence_str}. Robots policy allowed."
            
            recommended_action = (
                f"If you own or administer this website, add an authorized exception in {waf_name} "
                "for the crawler's IP address or User-Agent. Do not attempt unauthorized circumvention. "
                "For sitemaps, you may upload the XML sitemap file directly to bypass remote retrieval blocks."
            )
            
            return {
                "classification": "CDN_WAF_BLOCKED_OR_CHALLENGED",
                "evidence_type": "VERIFIED_EVIDENCE" if (has_cdn_headers or (body_indicators.get("akamai") and "Akamai" in waf_name)) else "INFERRED_CAUSE",
                "what_happened": what_happened,
                "where_it_happened": f"During {stage} (HTTP 403 from edge proxy/WAF).",
                "what_verified": what_verified,
                "recommended_action": recommended_action,
                "retry_appropriate": False,
                "safe_headers": safe_headers,
                "body_indicators": body_indicators
            }

        # 8. Origin Forbidden (standard Apache / Nginx / IIS / directory listing forbidden)
        if status_code == 403:
            if any(s in server_val for s in ["apache", "nginx", "microsoft-iis", "gunicorn", "uvicorn", "envoy"]):
                return {
                    "classification": "ORIGIN_FORBIDDEN",
                    "evidence_type": "INFERRED_CAUSE",
                    "what_happened": f"Origin web server ({server_val}) explicitly denied access to this resource.",
                    "where_it_happened": f"During {stage} at origin server.",
                    "what_verified": f"Origin server signature '{server_val}' returned HTTP 403 Forbidden with standard origin headers.",
                    "recommended_action": "Check web server filesystem permissions, directory index configuration, and origin access control rules.",
                    "retry_appropriate": False,
                    "safe_headers": safe_headers,
                    "body_indicators": body_indicators
                }
            else:
                # 9. HTTP 403 Unclassified (no definitive CDN/WAF signature, cannot confirm cause)
                return {
                    "classification": "HTTP_403_UNCLASSIFIED",
                    "evidence_type": "INFERRED_CAUSE",
                    "what_happened": "The target returned HTTP 403 Forbidden, but the specific origin vs CDN/WAF cause could not be conclusively determined.",
                    "where_it_happened": f"During {stage} request.",
                    "what_verified": "Received HTTP 403 without definitive CDN, WAF, or standard origin signatures.",
                    "recommended_action": "Inspect server access logs and edge firewall logs for this request URL to verify access policy rules.",
                    "retry_appropriate": False,
                    "safe_headers": safe_headers,
                    "body_indicators": body_indicators
                }

        # Catch-all
        return {
            "classification": "HTTP_403_UNCLASSIFIED" if status_code == 403 else "NETWORK_ERROR",
            "evidence_type": "INFERRED_CAUSE",
            "what_happened": f"Request failed with HTTP status {status_code}.",
            "where_it_happened": f"During {stage}.",
            "what_verified": f"HTTP status code {status_code}.",
            "recommended_action": "Check server response logs.",
            "retry_appropriate": False,
            "safe_headers": safe_headers,
            "body_indicators": body_indicators
        }
