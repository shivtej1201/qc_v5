"""Configuration from environment variables (prefix QC6_). No secrets have defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(f"QC6_{name}", default)


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "postgresql://postgres@127.0.0.1:5433/qc6"))
    data_dir: str = field(default_factory=lambda: _env("DATA_DIR", os.path.abspath("./var")))
    # honest identification (blueprint §1.1): real Chromium UA + this suffix, plus the per-site X-QC-Audit header
    ua_suffix: str = field(default_factory=lambda: _env("UA_SUFFIX", "QC-Audit/6.0 (+site quality check)"))
    access_header: str = "X-QC-Audit"
    visitors_per_host: int = field(default_factory=lambda: int(_env("VISITORS_PER_HOST", "6")))
    visitors_max: int = 20
    visit_share: float = field(default_factory=lambda: float(_env("VISIT_SHARE", "0.2")))   # of the host budget
    allow_private_targets: bool = field(default_factory=lambda: _env("ALLOW_PRIVATE_TARGETS", "false") == "true")  # tests only
    worker_id: str = field(default_factory=lambda: _env("WORKER_ID", f"w-{os.getpid()}"))

    @property
    def objects_dir(self) -> str:
        return os.path.join(self.data_dir, "objects")


settings = Settings()
