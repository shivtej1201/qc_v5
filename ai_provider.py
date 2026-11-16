"""
AI Explanation Provider for Link & Page Audit Remediations.
Provides deterministic, rule-based explanations with 100% offline availability,
SHA-256 caching, prompt injection defenses, and pluggable AI adapters.
"""

import abc
import hashlib
import json
import re
from typing import Dict, Any, Optional

# Prompt injection patterns and suspicious delimiters
INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior)\s+instructions", re.IGNORECASE),
    re.compile(r"system\s*:\s*", re.IGNORECASE),
    re.compile(r"assistant\s*:\s*", re.IGNORECASE),
    re.compile(r"user\s*:\s*", re.IGNORECASE),
    re.compile(r"<\s*script[^>]*>", re.IGNORECASE),
    re.compile(r"<\s*/\s*script\s*>", re.IGNORECASE),
    re.compile(r"eval\s*\(", re.IGNORECASE),
    re.compile(r"javascript\s*:", re.IGNORECASE),
]

def sanitize_untrusted_input(val: Any, max_len: int = 500) -> str:
    """
    Sanitizes untrusted text extracted from crawled web pages, headers, or URLs.
    Strips control characters, neutralizes injection patterns, and truncates length.
    """
    if val is None:
        return ""
    text = str(val).strip()
    # Strip null bytes and non-printable control chars (except standard whitespace)
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or (ch >= " " and ord(ch) != 127))
    
    # Neutralize prompt injection attempts
    for pat in INJECTION_PATTERNS:
        text = pat.sub("[SANITIZED]", text)
    
    # Enforce safe length limit
    if len(text) > max_len:
        text = text[:max_len] + "..."
    return text


def sanitize_cell_for_export(val: Any) -> Any:
    """
    Sanitizes values for safe CSV and XLSX spreadsheet exports:
    - Leaves numbers (int, float) as numbers
    - Strips non-printable XML control characters that crash openpyxl
    - Prepends single quote (') to strings that begin with formula execution triggers
      (=, +, -, @, \t, \r, %) even if preceded by whitespace
    - Preserves normal URLs (starting with http://, https://, or /)
    """
    if val is None:
        return ""
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return val
    s = str(val)
    # Strip illegal XML/Excel control characters (\x00-\x08, \x0b, \x0c, \x0e-\x1f, \x7f)
    s = "".join(ch for ch in s if ch in ("\n", "\r", "\t") or (ch >= " " and ord(ch) != 127))
    stripped = s.lstrip()
    if stripped and stripped[0] in ("=", "+", "-", "@", "\t", "\r", "%"):
        # Normal URLs with hyphen in path/query should remain unquoted
        if s.startswith(("http://", "https://", "/")):
            return s
        return "'" + s
    return s


def validate_explanation(explanation: Dict[str, str]) -> Dict[str, str]:
    """
    Validates and hardens generated explanation dictionary:
    Ensures all 5 required fields exist, non-empty, and free of illegal control characters.
    """
    required_keys = ("title", "description", "impact", "recommended_action", "priority_rationale")
    sanitized = {}
    for key in required_keys:
        raw_val = explanation.get(key, "")
        if not raw_val or not str(raw_val).strip():
            raw_val = f"No {key.replace('_', ' ')} provided."
        val_str = str(raw_val).strip()
        val_str = "".join(ch for ch in val_str if ch in ("\n", "\r", "\t") or (ch >= " " and ord(ch) != 127))
        sanitized[key] = val_str
    return sanitized


def compute_cache_key(finding: Dict[str, Any]) -> str:
    """
    Computes a deterministic SHA-256 cache key from the core finding attributes.
    """
    key_dict = {
        "category": finding.get("category", ""),
        "target_url": finding.get("target_url", ""),
        "status_code": finding.get("technical_details", {}).get("final_status_code") or finding.get("status_code", 0),
        "error_type": finding.get("technical_details", {}).get("error_type") or finding.get("error_type", ""),
        "redirect_count": finding.get("technical_details", {}).get("redirect_count", 0),
        "classification": finding.get("technical_details", {}).get("classification", "")
    }
    raw = json.dumps(key_dict, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class BaseAIExplanationProvider(abc.ABC):
    """Abstract base class for remediation explanation providers."""
    
    @abc.abstractmethod
    async def generate_explanation(self, finding: Dict[str, Any]) -> Dict[str, str]:
        """Generate structured title, description, impact, recommended_action, and priority_rationale."""
        pass


class RuleBasedProvider(BaseAIExplanationProvider):
    """
    100% offline, deterministic rule-based template provider.
    Zero external API dependencies. Fast, reliable, and auditable.
    """
    
    async def generate_explanation(self, finding: Dict[str, Any]) -> Dict[str, str]:
        category = finding.get("category", "GENERAL")
        target_url = sanitize_untrusted_input(finding.get("target_url", ""))
        source_count = finding.get("source_count", 1)
        tech = finding.get("technical_details", {})
        status_code = tech.get("final_status_code") or finding.get("status_code")
        error_type = sanitize_untrusted_input(tech.get("error_type") or finding.get("error_type", ""))
        redirect_count = tech.get("redirect_count", 0)
        source_count_str = f"{source_count} source page{'s' if source_count != 1 else ''}"
        
        if category == "BROKEN_LINK":
            if status_code == 404:
                title = f"Broken Link (404 Not Found): {target_url}"
                description = (
                    f"The destination URL returned an HTTP 404 Not Found error when requested. "
                    f"This link is referenced across {source_count_str}."
                )
                impact = (
                    "Users encounter dead ends leading to poor UX and high bounce rates. "
                    "Search engine crawlers waste crawl budget, degrading crawl efficiency and page rankings."
                )
                recommended_action = (
                    "Verify if the destination URL has moved. If moved, update the source links to the new canonical URL "
                    "or implement a 301 permanent redirect on the destination server. If obsolete, remove the link."
                )
            elif status_code and 500 <= status_code <= 599:
                title = f"Server Error (HTTP {status_code}): {target_url}"
                description = (
                    f"The destination URL returned server error HTTP {status_code}. "
                    f"The destination server failed to fulfill an apparently valid request."
                )
                impact = (
                    "Critical user friction and potential loss of conversions. "
                    "Repeated 5xx errors can cause search engines to de-index affected URLs."
                )
                recommended_action = (
                    "Inspect destination web server application logs, upstream proxies, and load balancers. "
                    "Ensure service health and check for unhandled server-side exceptions."
                )
            elif error_type and ("TIMEOUT" in error_type.upper() or "DEADLINE" in error_type.upper()):
                title = f"Connection Timeout: {target_url}"
                description = (
                    f"The request to destination URL timed out before receiving a response ({error_type})."
                )
                impact = (
                    "High latency and blocked user journeys. Degradation of search engine crawl coverage."
                )
                recommended_action = (
                    "Check network routing, DNS resolution, and destination server responsiveness under load. "
                    "Optimize backend response latency or update the link."
                )
            else:
                code_str = f"HTTP {status_code}" if status_code else (error_type or "Unreachable")
                title = f"Unhealthy Link ({code_str}): {target_url}"
                description = (
                    f"The link destination failed health checks with {code_str}. "
                    f"Referenced from {source_count_str}."
                )
                impact = (
                    "Inconsistent user experience and degraded link equity distribution."
                )
                recommended_action = (
                    "Audit the target URL availability and fix or replace the link target in source templates."
                )

        elif category == "REDIRECT_CHAIN":
            chain = tech.get("redirect_chain", [])
            chain_len = len(chain) if isinstance(chain, list) else redirect_count
            title = f"Multi-Hop Redirect Chain ({chain_len} hops): {target_url}"
            description = (
                f"The destination URL traverses {chain_len} intermediate redirect hops before reaching "
                f"the final destination ({sanitize_untrusted_input(tech.get('final_url', ''))})."
            )
            impact = (
                "Each redirect hop adds 100-400ms latency, delaying page render for users. "
                "Search engines may abandon deep redirect chains, resulting in lost link equity (PageRank)."
            )
            recommended_action = (
                f"Update all source links directly to the final resolved URL "
                f"({sanitize_untrusted_input(tech.get('final_url', target_url))}), bypassing intermediate hops."
            )

        elif category == "REDIRECT_LOOP":
            title = f"Circular Redirect Loop: {target_url}"
            description = (
                "The destination URL entered an infinite redirect loop and could not be resolved."
            )
            impact = (
                "Browser displays ERR_TOO_MANY_REDIRECTS; users cannot access destination content. "
                "Search crawlers will drop the URL from search index."
            )
            recommended_action = (
                "Audit server-side redirection rules (.htaccess, Nginx configs, Cloudflare Page Rules) "
                "and break circular redirect mappings."
            )

        elif category == "BROKEN_IMAGE":
            code_str = f"HTTP {status_code}" if status_code else "Fetch Failure"
            title = f"Broken Image Asset ({code_str}): {target_url}"
            description = (
                f"The image asset failed to load ({code_str}). "
                f"Referenced from {source_count_str}."
            )
            impact = (
                "Broken image icons damage brand perception, visual aesthetics, and accessibility."
            )
            recommended_action = (
                "Verify the image file exists in storage/CDN, verify file permissions, or replace the image path."
            )

        elif category == "MISSING_ALT_TEXT":
            title = f"Missing or Generic Image Alt Text: {target_url}"
            description = (
                f"The image element lacks descriptive alternative text (alt text is missing or generic). "
                f"Found on {source_count_str}."
            )
            impact = (
                "Violates WCAG 2.1 accessibility standards for screen-reader users. "
                "Missed opportunity for Google Image Search indexing and contextual relevancy."
            )
            recommended_action = (
                "Add meaningful, contextual alt text describing the content and function of the image."
            )

        elif category == "FAILED_API":
            api_status = tech.get("api_status", "API_ERROR")
            title = f"Client-Side Subrequest Failure ({api_status}): {target_url}"
            description = (
                f"An asynchronous background API subrequest failed on page with status {api_status} "
                f"(HTTP {status_code or 'N/A'})."
            )
            impact = (
                "Dynamic interactive components (search, cart, product listings) may break silently, "
                "resulting in abandoned user actions."
            )
            recommended_action = (
                "Inspect API endpoint health, CORS configurations, authentication tokens, and payload schemas."
            )

        elif category == "METADATA_ISSUE":
            reason = tech.get("reason", "Incomplete Classification")
            title = f"Metadata Classification Needs Review: {target_url}"
            description = (
                f"Source page classification requires review ({reason}). Meta tags (tag-1 / tag-2) were "
                f"missing, ambiguous, or unclassified."
            )
            impact = (
                "Audit reporting and analytics cannot reliably categorize traffic, page types, or business owners."
            )
            recommended_action = (
                "Ensure standard meta tags (<meta name=\"tag-1\" content=\"...\"/> and <meta name=\"tag-2\" content=\"...\"/>) "
                "are emitted by the server-side rendering pipeline."
            )

        elif category == "PAGE_ERROR":
            code_str = f"HTTP {status_code}" if status_code else (error_type or "Failed to Load")
            title = f"Page Render/Load Error ({code_str}): {target_url}"
            description = (
                f"The target page failed to load or render properly during browser execution ({code_str}). "
                f"Page status: {tech.get('page_status', 'PAGE_ERROR')}."
            )
            impact = (
                "Users encounter incomplete or broken web pages, leading to immediate bounce. "
                "Search crawlers will fail to index page contents and metadata."
            )
            recommended_action = (
                "Check server application logs, runtime JavaScript errors, and upstream reverse proxies. "
                "Ensure server responds with HTTP 200 and complete HTML."
            )

        else:
            title = f"Audit Finding: {target_url}"
            description = f"Audit flagged an issue of category {category}."
            impact = "Potential impact on site performance and SEO rankings."
            recommended_action = "Review the technical details and resolve the underlying issue."

        # Compute priority rationale
        priority = finding.get("priority", "P3")
        score = finding.get("priority_score", 50)
        rationale = (
            f"Assigned {priority} (Score {score}/100) based on severity '{finding.get('severity', 'MEDIUM')}', "
            f"broad impact across {source_count_str}, and detection confidence ({finding.get('confidence', 1.0):.0%})."
        )

        return validate_explanation({
            "title": title,
            "description": description,
            "impact": impact,
            "recommended_action": recommended_action,
            "priority_rationale": rationale
        })


class RemediationAIProvider(BaseAIExplanationProvider):
    """
    Unified provider managing caching, optional external AI adapter delegation,
    and automatic deterministic fallback.
    """

    def __init__(self, secondary_adapter: Optional[BaseAIExplanationProvider] = None, db: Optional[Any] = None):
        self.fallback = RuleBasedProvider()
        self.secondary = secondary_adapter
        self.db = db

    async def generate_explanation(self, finding: Dict[str, Any]) -> Dict[str, str]:
        cache_key = compute_cache_key(finding)
        
        # 1. Check DB Cache
        if self.db:
            try:
                cached = await self.db.get_cached_explanation(cache_key)
                if cached and all(k in cached for k in ("title", "description", "impact", "recommended_action", "priority_rationale")):
                    return validate_explanation({
                        "title": cached["title"],
                        "description": cached["description"],
                        "impact": cached["impact"],
                        "recommended_action": cached["recommended_action"],
                        "priority_rationale": cached["priority_rationale"]
                    })
            except Exception:
                pass

        explanation: Optional[Dict[str, str]] = None
        provider_name = "RULE_BASED"
        model_name = "deterministic-v1"

        # 2. Try Secondary Adapter if configured
        if self.secondary:
            try:
                raw_exp = await self.secondary.generate_explanation(finding)
                if raw_exp and isinstance(raw_exp, dict):
                    explanation = validate_explanation(raw_exp)
                    provider_name = "EXTERNAL_AI"
                    model_name = getattr(self.secondary, "model_name", "custom")
            except Exception:
                explanation = None

        # 3. Fallback to Rule-Based
        if not explanation:
            explanation = await self.fallback.generate_explanation(finding)
            provider_name = "RULE_BASED"
            model_name = "deterministic-v1"

        # 4. Save to DB Cache
        if self.db and explanation:
            try:
                await self.db.set_cached_explanation(
                    cache_key=cache_key,
                    explanation_json=json.dumps(explanation),
                    provider=provider_name,
                    model=model_name
                )
            except Exception:
                pass

        return explanation
