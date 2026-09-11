from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class Capabilities(BaseModel):
    """模型能力约束：不满足的模型不会被路由选中。"""

    model_config = ConfigDict(extra="forbid")

    context_window: int = 131072
    supports_tools: bool = True
    supports_vision: bool | None = None
    supports_reasoning: bool = True


class ModelPricing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_per_million: float | None = Field(default=None, ge=0)
    output_per_million: float | None = Field(default=None, ge=0)


class ModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    capabilities: Capabilities = Field(default_factory=Capabilities)
    pricing: ModelPricing | None = None


class ServiceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    id: str = ""
    base_url: str = Field(min_length=1)
    api_key: str = ""
    wire_api: Literal["responses", "chat"] = "responses"
    models: list[ModelSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def stable_identity(self):
        self.id = self.id or self.name
        if "/" in self.id:
            raise ValueError("服务 id 不能包含 /")
        return self


class CodexSettings(BaseModel):
    """Codex 接入开关及对外暴露模型集合。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    mode: Literal["fastest", "mapped"] = "fastest"
    models: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_mapping_keys(self):
        if len(self.models) != len(set(self.models)):
            raise ValueError("Codex 模型映射不能重复")
        for key in self.models:
            if not isinstance(key, str) or key.count("/") != 1:
                raise ValueError("Codex 模型映射必须使用 service-id/model-name 格式")
            service_id, model_name = key.split("/", 1)
            if not service_id or not model_name:
                raise ValueError("Codex 模型映射不能为空")
        return self


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_model: str = "route-fastest"
    bench_interval: int = Field(default=60, gt=0)
    stick_session_to_model: bool = True
    bench_concurrency: int = Field(default=4, gt=0)
    bench_rounds: int = Field(default=1, gt=0)
    bench_target_tokens: int = Field(default=256, gt=0)
    bench_timeout_seconds: float = Field(default=10, gt=0)
    timeout_seconds: int = Field(default=120, gt=0)
    codex: CodexSettings = Field(default_factory=CodexSettings)
    services: list[ServiceSpec]

    @model_validator(mode="after")
    def validate_unique_names(self):
        service_names = [service.name for service in self.services]
        if len(service_names) != len(set(service_names)):
            raise ValueError("服务名不能重复")
        ids = [service.id for service in self.services]
        if len(ids) != len(set(ids)):
            raise ValueError("服务 id 不能重复")
        for service in self.services:
            if "/" in service.name:
                raise ValueError("服务名不能包含 /")
            model_names = [model.name for model in service.models]
            if len(model_names) != len(set(model_names)):
                raise ValueError(f"服务 {service.name} 的模型名不能重复")
            if any("/" in model.name for model in service.models):
                raise ValueError(f"服务 {service.name} 的模型名不能包含 /")
        return self

    def all_models(self) -> list[tuple[ServiceSpec, ModelSpec]]:
        return [(s, m) for s in self.services for m in s.models]


def load_config(path: str) -> Config:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ValueError(f"配置 YAML 解析失败: {e}") from e
    if not isinstance(raw, dict):
        raise ValueError("配置必须是 YAML 映射")
    try:
        return Config.model_validate(raw)
    except ValidationError as e:
        raise ValueError(f"配置校验失败:\n{e}") from e


def save_config(path: str | Path, config: Config) -> None:
    """Validate-before-write and atomically replace the YAML config."""
    target = Path(path).expanduser()
    if target.is_symlink():
        raise ValueError(f"拒绝写入软链接配置文件：{target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = target.stat().st_mode & 0o777 if target.exists() else 0o600
    content = yaml.safe_dump(
        config.model_dump(exclude_none=True),
        allow_unicode=True,
        sort_keys=False,
    )
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(target)
        dir_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise
