import time
import random
import asyncio

class DBMetrics:
    def __init__(self):
        self.write_latencies = []
        self.lock_errors = 0
        self.retries = 0
        self.exhausted_retries = 0
        self.total_persisted = 0
        self.failed_operations = 0
        self.batch_sizes = []
        
    def record_write(self, latency: float, batch_size: int):
        self.write_latencies.append(latency)
        self.batch_sizes.append(batch_size)
        self.total_persisted += batch_size
        
        # keep history bounded
        if len(self.write_latencies) > 10000:
            self.write_latencies = self.write_latencies[-10000:]
        if len(self.batch_sizes) > 10000:
            self.batch_sizes = self.batch_sizes[-10000:]

    def reset(self):
        self.write_latencies.clear()
        self.batch_sizes.clear()
        self.lock_errors = 0
        self.retries = 0
        self.exhausted_retries = 0
        self.total_persisted = 0
        self.failed_operations = 0

    def get_stats(self):
        lats = sorted(self.write_latencies)
        return {
            "median_latency_ms": lats[len(lats)//2] * 1000 if lats else 0,
            "p95_latency_ms": lats[int(len(lats)*0.95)] * 1000 if lats else 0,
            "lock_errors": self.lock_errors,
            "retries": self.retries,
            "exhausted_retries": self.exhausted_retries,
            "total_persisted": self.total_persisted,
            "failed_operations": self.failed_operations
        }

db_metrics = DBMetrics()
