import asyncio
import time

class ConcurrencyTuner:
    def __init__(self, initial_concurrency: int):
        self.current_concurrency = initial_concurrency
        self.min_concurrency = 10
        self.max_concurrency = 500
        
        self.success_count = 0
        self.error_count = 0
        self.total_latency = 0.0
        self.check_count = 0
        
        self.lock = asyncio.Lock()
        self.last_adjustment = time.time()
        
        self.active_requests = 0
        self.condition = asyncio.Condition()

    async def acquire(self):
        async with self.condition:
            while self.active_requests >= self.current_concurrency:
                await self.condition.wait()
            self.active_requests += 1

    def release(self):
        # We need to use a task for the async lock, or just schedule it
        async def _release():
            async with self.condition:
                self.active_requests -= 1
                self.condition.notify()
        asyncio.create_task(_release())

    async def record_result(self, status_code: int, latency_ms: float):
        async with self.lock:
            self.check_count += 1
            self.total_latency += latency_ms
            
            if status_code in [429, 500, 502, 503, 504] or status_code == 0:
                self.error_count += 1
            elif status_code < 400:
                self.success_count += 1
                
            if self.check_count >= 100 or (time.time() - self.last_adjustment > 10):
                self._tune()

    def _tune(self):
        if self.check_count == 0: return
        
        error_rate = self.error_count / self.check_count
        avg_latency = self.total_latency / self.check_count
        
        old_concurrency = self.current_concurrency
        
        if error_rate > 0.05 or avg_latency > 3000:
            # Scale down
            self.current_concurrency = max(self.min_concurrency, int(self.current_concurrency * 0.8))
        elif error_rate < 0.01 and avg_latency < 1000:
            # Scale up
            self.current_concurrency = min(self.max_concurrency, int(self.current_concurrency * 1.1))
            
        if self.current_concurrency != old_concurrency:
            print(f"[Tuner] Adjusted concurrency from {old_concurrency} to {self.current_concurrency} (ErrRate: {error_rate:.2f}, AvgLatency: {avg_latency:.0f}ms)")
            # Notify waiters in case capacity increased
            async def _notify():
                async with self.condition:
                    self.condition.notify_all()
            asyncio.create_task(_notify())
            
        self.success_count = 0
        self.error_count = 0
        self.total_latency = 0.0
        self.check_count = 0
        self.last_adjustment = time.time()

