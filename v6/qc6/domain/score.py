"""qcv6-health-1.0 (blueprint §10.5). Weights are versioned; history is never recomputed."""
from __future__ import annotations

from typing import Dict, Iterable, Mapping

FORMULA = "qcv6-health-1.0"
WEIGHTS = {"technical": 0.30, "links": 0.20, "content": 0.15, "experience": 0.20, "performance": 0.10, "security": 0.05}
SEV_W = {"critical": 1.0, "high": 0.7, "medium": 0.4, "low": 0.15}


def compute(pages_total: int, fixes: Iterable[Mapping]) -> Dict:
    """fixes: open fixes with severity, dimension, pages_affected (template fixes already projected to all pages of
    their template). Each dimension = 100 * (1 - weighted share of pages affected), floored at 0."""
    if pages_total <= 0:
        return {"formula": FORMULA, "score": None, "dimensions": {}}
    load = {d: 0.0 for d in WEIGHTS}
    for f in fixes:
        d = f["dimension"] if f["dimension"] in WEIGHTS else "technical"
        load[d] += SEV_W.get(f["severity"], 0.15) * min(int(f["pages_affected"]), pages_total) / pages_total
    dims = {d: round(max(0.0, 100.0 * (1.0 - min(1.0, load[d]))), 1) for d in WEIGHTS}
    score = round(sum(dims[d] * w for d, w in WEIGHTS.items()), 1)
    return {"formula": FORMULA, "score": score, "dimensions": dims, "weights": WEIGHTS}
