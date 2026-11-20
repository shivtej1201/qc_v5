"""Human pacing for real-visitor checks and journeys (blueprint §6.1, §8.2, §10.2)."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class Persona:
    think_median_s: float = 2.5
    think_sigma: float = 0.5
    think_min_s: float = 1.0
    think_max_s: float = 8.0
    typing_wpm: float = 38.0
    scroll_step_min: float = 0.6        # fraction of the viewport per scroll step
    scroll_step_max: float = 0.9
    scroll_pause_min_s: float = 0.3
    scroll_pause_max_s: float = 0.9


class Pacer:
    """All randomness comes from one seeded RNG so a visit can be replayed exactly."""

    def __init__(self, persona: Persona = Persona(), seed: Optional[int] = None) -> None:
        self.p = persona
        self.rng = random.Random(seed)

    def think(self) -> float:
        """Log-normal pause: most near the median, a few long ones, like people reading."""
        v = self.rng.lognormvariate(math.log(self.p.think_median_s), self.p.think_sigma)
        return min(self.p.think_max_s, max(self.p.think_min_s, v))

    def scroll_plan(self, page_height: int, viewport: int) -> List[Tuple[int, float]]:
        """(y, pause_s) steps from the top to the bottom of the page."""
        y, plan = 0, []
        bottom = max(0, page_height - viewport)
        while y < bottom and len(plan) < 200:
            y = min(bottom, y + max(40, int(viewport * self.rng.uniform(self.p.scroll_step_min, self.p.scroll_step_max))))
            plan.append((y, self.rng.uniform(self.p.scroll_pause_min_s, self.p.scroll_pause_max_s)))
        return plan

    def key_delays(self, text: str) -> List[float]:
        """Per-character delays for typing at the persona's words per minute (5 chars per word), +-35% jitter."""
        base = 60.0 / (self.p.typing_wpm * 5.0)
        return [base * self.rng.uniform(0.65, 1.35) for _ in text]

    def mouse_path(self, x0: float, y0: float, x1: float, y1: float, steps: int = 12) -> List[Tuple[float, float]]:
        """Eased path to a target (ease-in-out), ending exactly on it."""
        pts = []
        for i in range(1, steps + 1):
            t = i / steps
            e = t * t * (3 - 2 * t)
            pts.append((x0 + (x1 - x0) * e, y0 + (y1 - y0) * e))
        return pts
