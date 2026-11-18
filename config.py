import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    MODE: str = "url_status" # or "link_audit"
    
    HTTP_CONCURRENCY: int = 200
    BROWSER_CONCURRENCY: int = 2
    
    NAVIGATION_TIMEOUT: int = 15000
    EXTRACTION_TIMEOUT: int = 5000
    SETTLING_TIME: float = 0.5
    WAIT_FOR_SELECTOR: str = ""
    
    BATCH_SIZE: int = 1000
    WORKER_COUNT: int = int(os.getenv("WORKER_COUNT", os.getenv("MAX_CONCURRENT_JOBS", "8")))
    MAX_CONCURRENT_JOBS: int = int(os.getenv("WORKER_COUNT", os.getenv("MAX_CONCURRENT_JOBS", "8")))
    
    REQUESTS_PER_SECOND: float = 100.0
    PER_DOMAIN_CONCURRENCY: int = 20
    
    CONNECT_TIMEOUT: float = 5.0
    READ_TIMEOUT: float = 15.0
    WRITE_TIMEOUT: float = 10.0
    POOL_TIMEOUT: float = 10.0
    
    MAX_RETRIES: int = 1
    
    CACHE_ENABLED: bool = True
    CACHE_TTL_HOURS: int = 24
    
    BROWSER_FALLBACK: bool = True
    RESPECT_ROBOTS: bool = True
    
    DB_BATCH_SIZE: int = 500
    DB_FILE: str = "audit_cache.db"
    DB_BUSY_TIMEOUT: int = 5000
    
    USE_HEAD_FIRST: bool = False
    
    CONTENT_SELECTORS: list = [
        "main", 
        "article", 
        "[role='main']", 
        "#main-content", 
        ".main-content", 
        "#content", 
        ".content",
        "body" # Fallback
    ]
    
    DAM_PATTERNS: list = ["/content/dam/"]
    
    PAGETYPE_DELIMITER: str = "-"
    PAGETYPE_MAPPING: dict = {
        # Raw exact matches take precedence
        "b2b-clp": {"business": "B2B", "page_type": "CLP"},
        "b2c-plp": {"business": "B2C", "page_type": "PLP"},
        "b2c-pdp": {"business": "B2C", "page_type": "PDP"},
        "b2b-plp": {"business": "B2B", "page_type": "PLP"},
    }
    
    API_AUDIT_TIMEOUT: int = 20000
    API_AUDIT_SETTLE_MS: int = 3000
    ARTIFACTS_DIR: str = "artifacts/api_response_audit"

    # Phase 11.2 Authentication & Operational Limits
    AUTH_REQUIRED: bool = False
    PROXY_SECRET_HEADER: str = "X-Proxy-Secret"
    PROXY_SECRET_VALUE: str = ""
    USER_HEADER: str = "X-Remote-User"
    
    MAX_URLS_PER_JOB: int = int(os.getenv("MAX_URLS_PER_JOB", "1000000"))
    LINK_AUDIT_MAX_URL_RECORDS: int = int(os.getenv("LINK_AUDIT_MAX_URL_RECORDS", "1000000"))
    MAX_PAYLOAD_SIZE_BYTES: int = 1048576 # 1 MB maximum response body read
    SCREENSHOT_RETENTION_DAYS: int = 30
    BATCH_LEASE_DURATION_SEC: int = 60

    # Phase 10.9 Link Audit Parallel Workers & Concurrency Settings
    LINK_AUDIT_CONCURRENCY: int = 32              # Bounded concurrency (range 1-50)
    LINK_AUDIT_PER_HOST_CONCURRENCY: int = 10     # Max concurrent requests per target host
    LINK_AUDIT_CONNECT_TIMEOUT: float = 5.0      # Connection timeout in seconds
    LINK_AUDIT_READ_TIMEOUT: float = 15.0        # Read timeout in seconds
    LINK_AUDIT_TOTAL_TIMEOUT: float = 20.0       # Total request timeout in seconds
    LINK_AUDIT_MAX_RETRIES: int = 2              # Max retries for transient failures
    LINK_AUDIT_MAX_QUEUE_SIZE: int = 1000        # Max bounded queue size
    LINK_AUDIT_BROWSER_FALLBACK: bool = False    # Disable 30s Playwright fallback for link destination checks

    # Phase 10.12 Sitemap & 403 Diagnostics Settings
    SITEMAP_MAX_DEPTH: int = 5
    SITEMAP_MAX_RESPONSE_SIZE: int = 10 * 1024 * 1024 # 10 MB limit
    SITEMAP_MAX_URLS: int = 50000
    SITEMAP_TIMEOUT: float = 15.0
    SITEMAP_CONCURRENCY: int = 5
    ALLOW_LOCAL_TESTING: bool = False

    # On-page image audit: configurable image-bearing attributes (one parse pass collects them all)
    ON_PAGE_IMAGE_ATTRIBUTES: list = ["src", "data-src", "data-lazy-src", "data-original", "data-image", "srcset", "data-srcset"]
    ON_PAGE_IMAGE_SOURCE_ATTRIBUTES: list = ["srcset", "data-srcset"]          # <picture><source ...>
    ON_PAGE_IMAGE_META_KEYS: list = ["og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src"]

    class Config:
        env_file = ".env"

settings = Settings()

