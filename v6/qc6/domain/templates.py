"""Page templates: group pages by layout so real-visitor review can sample every kind of page (blueprint §10.1).

A page's fingerprint is a 64-bit SimHash over shingles of its tag-path skeleton (tags + first class, no text), so
two product pages with different products land close together while a product page and an article land far apart.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from html.parser import HTMLParser
from typing import Dict, Iterable, List, Optional, Tuple

SKIP = {"script", "style", "noscript", "svg", "path", "br", "wbr", "template"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_NUM = re.compile(r"\d+")


class _Skeleton(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: List[str] = []
        self.paths: List[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIP:
            if tag not in VOID:
                self.skip_depth += 1
            return
        if self.skip_depth:
            return
        cls = next((v for k, v in attrs if k == "class" and v), "")
        first = _NUM.sub("#", cls.split()[0]) if cls else ""          # hashed/numbered class names fold together
        token = tag + ("." + first if first else "")
        if len(self.stack) < 12:                                     # deep nesting is noise, not layout
            self.paths.append("/".join(self.stack[-3:] + [token]))
        if tag not in VOID:
            self.stack.append(token)

    def handle_endtag(self, tag):
        if tag in SKIP:
            if tag not in VOID and self.skip_depth:
                self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i].split(".", 1)[0] == tag:
                del self.stack[i:]
                break


def _h64(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "big")


def skeleton_paths(html: str, limit: int = 400_000) -> List[str]:
    p = _Skeleton()
    try:
        p.feed(html[:limit])
    except Exception:          # malformed markup: use what was parsed
        pass
    return p.paths


def simhash(html: str, shingle: int = 3) -> int:
    paths = skeleton_paths(html)
    if not paths:
        return 0
    # weight each distinct shingle once: repeated list items (100 product tiles) must not swamp the layout
    shingles = Counter("|".join(paths[i:i + shingle]) for i in range(max(1, len(paths) - shingle + 1)))
    weights = [0] * 64
    for sh in shingles:
        h = _h64(sh)
        for b in range(64):
            weights[b] += 1 if (h >> b) & 1 else -1
    return sum(1 << b for b in range(64) if weights[b] > 0)


def distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def to_signed(fp: int) -> int:
    """Postgres BIGINT is signed."""
    return fp - (1 << 64) if fp >= (1 << 63) else fp


def from_signed(v: int) -> int:
    return v + (1 << 64) if v < 0 else v


def path_pattern(url_path: str) -> str:
    """/loans/personal-loan/pune-123 -> /loans/personal-loan/*  (last segment generalised, digits folded)."""
    parts = [p for p in url_path.split("?")[0].split("/") if p]
    if not parts:
        return "/"
    parts[-1] = "*"
    return "/" + "/".join(_NUM.sub("#", p) for p in parts)


class TemplateIndex:
    """Online clustering. Each page joins the nearest template within `radius` bits, otherwise starts a new one.
    Four 16-bit bands give candidate lookup in O(1); with radius <= 15 a true neighbour shares at least one band."""

    BANDS = 4

    def __init__(self, radius: int = 10) -> None:
        self.radius = radius
        self.centroids: List[int] = []
        self.counts: List[int] = []
        self.patterns: List[Counter] = []
        self._bands: List[Dict[int, List[int]]] = [dict() for _ in range(self.BANDS)]

    def _band_keys(self, fp: int) -> List[int]:
        return [(fp >> (16 * i)) & 0xFFFF for i in range(self.BANDS)]

    def nearest(self, fp: int) -> Optional[Tuple[int, int]]:
        cands = {tid for i, k in enumerate(self._band_keys(fp)) for tid in self._bands[i].get(k, ())}
        if not cands:
            return None
        tid = min(cands, key=lambda t: distance(fp, self.centroids[t]))
        return tid, distance(fp, self.centroids[tid])

    def assign(self, fp: int, url_path: str = "") -> int:
        hit = self.nearest(fp)
        if hit is not None and hit[1] <= self.radius:
            tid = hit[0]
        else:
            # radius larger than one band can hide a neighbour; fall back to a linear scan while the index is small
            tid = None
            if len(self.centroids) <= 2000:
                best = min(range(len(self.centroids)), key=lambda t: distance(fp, self.centroids[t]), default=None)
                if best is not None and distance(fp, self.centroids[best]) <= self.radius:
                    tid = best
            if tid is None:
                tid = len(self.centroids)
                self.centroids.append(fp)
                self.counts.append(0)
                self.patterns.append(Counter())
                for i, k in enumerate(self._band_keys(fp)):
                    self._bands[i].setdefault(k, []).append(tid)
        self.counts[tid] += 1
        if url_path:
            self.patterns[tid][path_pattern(url_path)] += 1
        return tid

    def name(self, tid: int) -> str:
        """Human name from the dominant URL pattern: '/loans/personal-loan/*' -> 'Personal loan pages'."""
        pat = self.patterns[tid].most_common(1)[0][0] if self.patterns[tid] else "/"
        return template_name(pat)

    def summary(self) -> List[Dict]:
        return [{"id": t, "pages": self.counts[t], "name": self.name(t),
                 "pattern": self.patterns[t].most_common(1)[0][0] if self.patterns[t] else "/"}
                for t in sorted(range(len(self.centroids)), key=lambda t: -self.counts[t])]


def template_name(pattern: str) -> str:
    segs = [s for s in pattern.split("/") if s and s != "*"]
    if not segs:
        return "Home and top-level pages"
    word = segs[-1].replace("#", "").replace("-", " ").replace("_", " ").strip() or segs[0]
    word = word[:1].upper() + word[1:]
    return f"{word} pages"


def cluster(pages: Iterable[Tuple[str, str]], radius: int = 10) -> Tuple[TemplateIndex, Dict[str, int]]:
    """pages: (url_path, html). Returns the index and url_path -> template id."""
    idx = TemplateIndex(radius)
    out = {}
    for path, html in pages:
        out[path] = idx.assign(simhash(html), path)
    return idx, out
