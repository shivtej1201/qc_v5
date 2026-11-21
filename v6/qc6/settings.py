"""Configuration from environment variables (prefix QC6_). No secrets have defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(f"QC6_{name}", default)


def clean_dsn(dsn: str) -> str:
    """Accept a database address whose password contains @ : / # ? and so on, typed as-is (e.g. Pass@143).

    Splits user:password at the LAST '@' before the host and percent-encodes them, so the host is read correctly.
    QC6_DB_PASSWORD, when set, replaces the password in the address (no encoding needed at all)."""
    import urllib.parse
    if "://" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    # the host part ends at the first '/' or '?' AFTER the last '@'
    at = rest.rfind("@")
    tail = rest[at + 1:] if at >= 0 else rest
    cut = min([i for i in (tail.find("/"), tail.find("?")) if i >= 0] or [len(tail)])
    host, path = tail[:cut], tail[cut:]
    user, password = "", None
    if at >= 0:
        userinfo = rest[:at]
        user, _, pw = userinfo.partition(":")
        password = pw if ":" in userinfo else None
        user = urllib.parse.quote(urllib.parse.unquote(user), safe="")
        if password is not None:
            password = urllib.parse.quote(urllib.parse.unquote(password), safe="")
    if os.environ.get("QC6_DB_PASSWORD"):
        password = urllib.parse.quote(os.environ["QC6_DB_PASSWORD"], safe="")
        user = user or "postgres"
    auth = (user + (f":{password}" if password is not None else "") + "@") if (user or password is not None) else ""
    return f"{scheme}://{auth}{host}{path}"


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: clean_dsn(_env("DATABASE_URL", "postgresql://postgres@127.0.0.1:5433/qc6")))
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
