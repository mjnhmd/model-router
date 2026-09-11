"""模型启停状态持久化：独立 state 文件，不改动主 config.yaml（避免毁注释/格式）。"""
from __future__ import annotations

from pathlib import Path

import yaml

DEFAULT_STATE_FILE = "config.state.yaml"


class RouterState:
    def __init__(self, state_file: str = DEFAULT_STATE_FILE):
        self.state_file = Path(state_file)
        self.disabled: set[str] = set()
        self.priority: list[str] = []
        self.mapped_models: set[str] = set()
        self.benchmarks: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.state_file.exists():
            return
        try:
            raw = yaml.safe_load(self.state_file.read_text(encoding="utf-8")) or {}
            self.disabled = set(raw.get("disabled", []))
            priority = raw.get("priority", [])
            self.priority = [key for key in priority if isinstance(key, str)] if isinstance(priority, list) else []
            mapped = raw.get("mapped_models", [])
            self.mapped_models = {key for key in mapped if isinstance(key, str)} if isinstance(mapped, list) else set()
            raw_benchmarks = raw.get("benchmarks", {})
            self.benchmarks = {
                key: value for key, value in raw_benchmarks.items()
                if isinstance(key, str) and isinstance(value, dict)
            } if isinstance(raw_benchmarks, dict) else {}
        except Exception:
            # 状态文件损坏不致命，回退默认
            self.disabled = set()
            self.priority = []
            self.mapped_models = set()
            self.benchmarks = {}

    def save(self) -> None:
        data = {
            "disabled": sorted(self.disabled),
            "priority": self.priority,
            "mapped_models": sorted(self.mapped_models),
            "benchmarks": self.benchmarks,
        }
        tmp = self.state_file.with_suffix(".yaml.tmp")
        tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        tmp.replace(self.state_file)

    def is_disabled(self, service: str, model: str) -> bool:
        return f"{service}/{model}" in self.disabled

    def set_disabled(self, service: str, model: str, disabled: bool) -> bool:
        key = f"{service}/{model}"
        old_disabled = set(self.disabled)
        if disabled:
            changed = key not in self.disabled
            self.disabled.add(key)
        else:
            changed = key in self.disabled
            self.disabled.discard(key)
        if changed:
            try:
                self.save()
            except Exception:
                self.disabled = old_disabled
                raise
        return changed

    def set_priority(self, keys: list[str]) -> bool:
        old_priority = list(self.priority)
        new_priority = list(dict.fromkeys(keys))
        if new_priority == self.priority:
            return False
        self.priority = new_priority
        try:
            self.save()
        except Exception:
            self.priority = old_priority
            raise
        return True

    def set_mapped_models(self, keys: list[str]) -> bool:
        old = set(self.mapped_models)
        new = {key for key in keys if isinstance(key, str) and key}
        if new == old:
            return False
        self.mapped_models = new
        try:
            self.save()
        except Exception:
            self.mapped_models = old
            raise
        return True

    def set_benchmark(self, key: str, result: dict) -> bool:
        old = self.benchmarks.get(key)
        if old == result:
            return False
        self.benchmarks[key] = dict(result)
        try:
            self.save()
        except Exception:
            if old is None:
                self.benchmarks.pop(key, None)
            else:
                self.benchmarks[key] = old
            raise
        return True

    def set_disabled_many(self, keys: set[str], disabled: bool) -> int:
        old_disabled = set(self.disabled)
        changed = 0
        for key in keys:
            if disabled:
                if key not in self.disabled:
                    self.disabled.add(key)
                    changed += 1
            elif key in self.disabled:
                self.disabled.remove(key)
                changed += 1
        if changed:
            try:
                self.save()
            except Exception:
                self.disabled = old_disabled
                raise
        return changed
