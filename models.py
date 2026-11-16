from typing import Optional, List, Any
from pydantic import BaseModel
from datetime import datetime
import json

class UrlResult(BaseModel):
    normalized_url: str
    original_url: str
    initial_status_code: Optional[int] = None
    final_status_code: Optional[int] = None
    status: str = "PENDING"
    final_url: Optional[str] = None
    redirect_count: int = 0
    redirect_chain: str = "" # JSON string of list of dicts
    response_time_ms: Optional[float] = None
    content_type: str = ""
    content_length: int = 0
    error_type: str = ""
    error_message: str = ""
    check_method: str = ""
    browser_verified: str = "NO"
    checked_at: Optional[str] = None
    html: Optional[str] = None

    def to_db_dict(self):
        d = self.model_dump()
        d.pop('html', None)
        return d

class ImageResult(BaseModel):
    normalized_url: str
    initial_status_code: Optional[int] = None
    final_status_code: Optional[int] = None
    status: str = "Failed"
    image_format: Optional[str] = None
    image_size_kb: Optional[float] = None
    is_dam_asset: bool = False
    source_platform: str = "Unknown"
    error_type: str = ""
    error_message: str = ""
    checked_at: Optional[str] = None

    def to_db_dict(self):
        return self.model_dump()

class PageAuditResult(BaseModel):
    job_id: str
    batch_id: str
    page_url: str
    final_url: Optional[str] = None
    page_status: str = "INCONCLUSIVE" # FOUND, NOT_FOUND, API_ERROR, PAGE_ERROR, TIMEOUT, INCONCLUSIVE, SKIPPED
    http_status: Optional[int] = None
    response_time_ms: Optional[float] = None
    error_message: Optional[str] = None
    screenshot_path: Optional[str] = None
    api_count: int = 0
    failed_api_count: int = 0
    summary_reason: Optional[str] = None
    evaluated_at: Optional[str] = None

    def to_db_dict(self):
        return self.model_dump()

class PageApiResponse(BaseModel):
    job_id: str
    page_url: str
    request_url: str
    method: str = "GET"
    resource_type: str = "fetch" # fetch, xhr
    http_status: Optional[int] = None
    api_status: str = "INCONCLUSIVE" # FOUND, NOT_FOUND, API_ERROR, TIMEOUT, INCONCLUSIVE
    response_time_ms: Optional[float] = None
    content_type: str = ""
    error_message: Optional[str] = None
    response_snippet: Optional[str] = None
    observed_at: Optional[str] = None

    def to_db_dict(self):
        return self.model_dump()
