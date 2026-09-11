from __future__ import annotations

import time


class ModelScore:
    """单个模型的实时测速评分。tokens/s 越高越好；错误会让分数快速衰减。"""

    ALPHA = 0.7  # EWMA 平滑系数：新观测权重。越大越跟手，越小越稳。
    ERROR_PENALTY = 0.5  # 失败后分数衰减因子

    def __init__(self, model_key: str, default_tps: float = 20.0):
        self.model_key = model_key
        self.tps = default_tps
        self.latency = 0.0
        self.time_to_first_token = 0.0
        self.sample_count = 0
        self.error_rate = 0.0
        self.total_requests = 0
        self.failed_requests = 0
        self.last_update = 0.0

    TIMEOUT_PENALTY = 0.7  # 探测超时（慢）：只降权，不判死

    def observe(self, tokens_per_sec: float, elapsed: float, is_timeout: bool = False, time_to_first_token: float = 0.0) -> None:
        self.total_requests += 1
        self.last_update = time.time()
        self.sample_count += 1
        if tokens_per_sec <= 0:
            self.failed_requests += 1
            if is_timeout:
                # 慢 ≠ 挂：降权但不抬 error_rate，模型仍可被选中（只是排后面）
                self.tps *= self.TIMEOUT_PENALTY
                return
            self.tps *= self.ERROR_PENALTY
            self.error_rate = 0.8 * self.error_rate + 0.2
            return
        self.tps = self.ALPHA * tokens_per_sec + (1 - self.ALPHA) * self.tps
        self.latency = 0.6 * elapsed + 0.4 * self.latency
        if time_to_first_token > 0:
            self.time_to_first_token = 0.6 * time_to_first_token + 0.4 * self.time_to_first_token
        self.error_rate *= 0.6

    def observe_probe_ok(self, elapsed: float) -> None:
        """流式探测成功但未测出吞吐：只更新延迟与可用性，不判失败。"""
        self.total_requests += 1
        self.last_update = time.time()
        self.sample_count += 1
        self.latency = 0.6 * elapsed + 0.4 * self.latency
        self.error_rate *= 0.6  # 可用 → 错误率衰减

    @property
    def healthy(self) -> bool:
        # 连续失败或近期失败都不健康
        return self.error_rate < 0.15
