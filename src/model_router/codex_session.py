from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import time
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
from tomlkit import TOMLDocument, parse, table
from tomlkit.exceptions import ParseError
from tomlkit.items import Table


_MISSING_HASH = None
_STATE_VERSION = 1


class CodexConfigConflictError(RuntimeError):
    """Codex 文件在本次接管外发生变化，不能覆盖。"""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _reject_symlink(path: Path, label: str) -> None:
    if _lexists(path) and path.is_symlink():
        raise ValueError(f"Codex {label} 不支持软链接: {path}")


def _parse_config(raw: bytes) -> TOMLDocument:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Codex 配置不是有效的 UTF-8: {exc}") from exc

    try:
        document = parse(text)
        tomllib.loads(text)
    except (ParseError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"Codex TOML 无效，未修改配置: {exc}") from exc
    return document


def _validate_editable_shape(document: TOMLDocument) -> None:
    for key in ("model", "model_provider"):
        if key in document and not isinstance(document[key].unwrap(), str):
            raise ValueError(f"Codex 配置中的 {key} 不是字符串，拒绝覆盖")

    providers = document.get("model_providers")
    if providers is not None and not isinstance(providers, Table):
        raise ValueError("Codex 配置中的 model_providers 不是 TOML 表，拒绝修改")
    if isinstance(providers, Table) and "local_router" in providers:
        if not isinstance(providers["local_router"], Table):
            raise ValueError("Codex 配置中的 local_router 不是 TOML 表，拒绝覆盖")


def _render_managed_config(raw: str, port: int, public_model: str) -> bytes:
    if not isinstance(public_model, str) or not public_model.strip():
        raise ValueError("public_model 不能为空")
    if not 1 <= port <= 65535:
        raise ValueError("代理端口必须在 1 到 65535 之间")

    document = _parse_config(raw.encode("utf-8"))
    _validate_editable_shape(document)

    if "model_provider" in document:
        document["model_provider"] = "local_router"
    else:
        document.add("model_provider", "local_router")
    if "model" in document:
        document["model"] = public_model
    else:
        document.add("model", public_model)

    providers = document.get("model_providers")
    if providers is None:
        providers = table()
        document.add("model_providers", providers)

    if "local_router" in providers:
        providers.remove("local_router")

    managed = table()
    managed.add("name", "local_router")
    managed.add("wire_api", "responses")
    managed.add("requires_openai_auth", False)
    managed.add("base_url", f"http://127.0.0.1:{port}/v1")
    providers.add("local_router", managed)

    rendered = document.as_string().encode("utf-8")
    try:
        tomllib.loads(rendered.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"生成的 Codex TOML 无效，未修改配置: {exc}") from exc
    return rendered


def _load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CodexConfigConflictError(f"Codex 接管状态损坏，未自动恢复: {path}") from exc
    if not isinstance(data, dict) or data.get("version") != _STATE_VERSION:
        raise CodexConfigConflictError(f"Codex 接管状态版本不受支持，未自动恢复: {path}")
    if not isinstance(data.get("managed_sha256"), str):
        raise CodexConfigConflictError("Codex 接管状态缺少目标文件指纹，未自动恢复")
    if not isinstance(data.get("original_exists"), bool):
        raise CodexConfigConflictError("Codex 接管状态缺少原文件存在标记，未自动恢复")
    mode = data.get("original_mode")
    if not isinstance(mode, int) or not 0 <= mode <= 0o777:
        raise CodexConfigConflictError("Codex 接管状态中的文件权限无效，未自动恢复")
    original_hash = data.get("original_sha256")
    if original_hash not in (None, "") and not isinstance(original_hash, str):
        raise CodexConfigConflictError("Codex 接管状态中的原文件指纹无效，未自动恢复")
    return data


def _current_bytes(path: Path) -> bytes | None:
    if not _lexists(path):
        return None
    if path.is_symlink():
        raise CodexConfigConflictError(f"Codex 配置在接管期间变成软链接，未覆盖: {path}")
    if not path.is_file():
        raise CodexConfigConflictError(f"Codex 配置在接管期间不再是文件，未覆盖: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise CodexConfigConflictError(f"无法读取 Codex 配置，未覆盖: {path}") from exc


class CodexSession:
    """临时接管 Codex 配置，使用结构化编辑和指纹校验安全恢复。"""

    def __init__(self, config_path: Path | str):
        self.config_path = Path(config_path).expanduser()
        self._active = False
        self._lock_fd: int | None = None

    @property
    def backup_path_for_write(self) -> Path:
        return self.config_path.with_name(self.config_path.name + ".model-router.bak")

    @property
    def backup_path(self) -> Path | None:
        path = self.backup_path_for_write
        return path if _lexists(path) else None

    @property
    def state_path(self) -> Path:
        return self.config_path.with_name(self.config_path.name + ".model-router.state.json")

    @property
    def lock_path(self) -> Path:
        return self.config_path.with_name(self.config_path.name + ".model-router.lock")

    @property
    def active(self) -> bool:
        return self._active

    def start(self, port: int, public_model: str) -> None:
        if self._active:
            return

        self._acquire_lock()
        try:
            self._write_takeover_locked(port, public_model)
            self._active = True
        except Exception:
            try:
                self._recover_previous_locked()
            except Exception:
                pass
            raise
        finally:
            if not self._active:
                self._release_lock()

    def install_persistent(self, port: int, public_model: str) -> None:
        """安全安装 CLI 使用的持久接入，直到 detach 才恢复原配置。"""
        if self._active:
            raise RuntimeError("当前 Codex 接管会话仍在运行，不能持久安装")
        self._acquire_lock()
        try:
            self._write_takeover_locked(port, public_model)
        finally:
            self._release_lock()

    def remove_persistent(self) -> None:
        """移除本工具留下的持久接入；无法安全合并的外部修改保留备份。"""
        self._acquire_lock()
        try:
            if self.backup_path is None or not self.state_path.exists():
                raise FileNotFoundError("未找到完整的 model-router Codex 接入备份")
            self._recover_previous_locked(auto_archive_orphan=False)
        finally:
            self._release_lock()

    def _write_takeover_locked(self, port: int, public_model: str) -> None:
        self._recover_previous_locked(auto_archive_orphan=True)
        self._validate_paths()

        original = _current_bytes(self.config_path)
        original_exists = original is not None
        original_mode = (
            stat.S_IMODE(self.config_path.stat().st_mode) if original_exists else 0o600
        )
        original_hash = _sha256(original) if original is not None else _MISSING_HASH
        rendered = _render_managed_config(
            original.decode("utf-8") if original is not None else "",
            port,
            public_model,
        )

        config_write_attempted = False
        try:
            _atomic_write(self.backup_path_for_write, original or b"", mode=0o600)
            state = {
                "version": _STATE_VERSION,
                "original_exists": original_exists,
                "original_mode": original_mode,
                "original_sha256": original_hash,
                "managed_sha256": _sha256(rendered),
                "managed_model": public_model,
                "managed_model_provider": "local_router",
                "managed_local_router": tomllib.loads(rendered.decode("utf-8"))[
                    "model_providers"
                ]["local_router"],
            }
            _atomic_write(
                self.state_path,
                json.dumps(state, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n",
                mode=0o600,
            )

            if _current_bytes(self.config_path) != original:
                raise CodexConfigConflictError(
                    "Codex 配置在接管期间被外部修改，未覆盖"
                )

            config_write_attempted = True
            _atomic_write(self.config_path, rendered, mode=original_mode)
            if _current_bytes(self.config_path) != rendered:
                raise OSError("写入后的 Codex 配置校验不一致")
            _parse_config(rendered)
        except Exception:
            if not config_write_attempted:
                self._remove_artifact(self.backup_path_for_write)
                self._remove_artifact(self.state_path)
            else:
                try:
                    self._recover_previous_locked()
                except Exception:
                    pass
            raise

    def restore(self) -> None:
        if not self._active:
            return
        try:
            self._restore_locked()
        finally:
            self._active = False
            self._release_lock()

    def recover_previous(self) -> bool:
        """恢复已知托管字段；外部修改无法安全合并时保留配置和备份。"""
        self._acquire_lock()
        try:
            return self._recover_previous_locked(auto_archive_orphan=True)
        finally:
            self._release_lock()

    def _validate_paths(self) -> None:
        _reject_symlink(self.config_path, "配置文件")
        _reject_symlink(self.backup_path_for_write, "备份文件")
        _reject_symlink(self.state_path, "状态文件")

    def _acquire_lock(self) -> None:
        if self._lock_fd is not None:
            return
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        _reject_symlink(self.lock_path, "锁文件")
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise RuntimeError("Codex 配置已被占用，拒绝并发接管") from exc
        except Exception:
            os.close(fd)
            raise
        self._lock_fd = fd

    def _release_lock(self) -> None:
        if self._lock_fd is None:
            return
        fd, self._lock_fd = self._lock_fd, None
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _recover_previous_locked(self, auto_archive_orphan: bool = False) -> bool:
        backup_exists = _lexists(self.backup_path_for_write)
        state_exists = _lexists(self.state_path)
        if not backup_exists and not state_exists:
            return False
        self._validate_paths()

        if backup_exists != state_exists:
            raise CodexConfigConflictError("Codex 接管备份与状态不完整，未自动恢复")

        state = _load_state(self.state_path)
        backup = self.backup_path_for_write.read_bytes()
        original_exists = bool(state.get("original_exists"))
        original_hash = state.get("original_sha256")
        if original_exists:
            if not isinstance(original_hash, str) or _sha256(backup) != original_hash:
                raise CodexConfigConflictError("Codex 原始备份指纹不匹配，未自动恢复")
            _parse_config(backup)
        elif backup:
            raise CodexConfigConflictError("Codex 缺失文件备份不为空，未自动恢复")

        current = _current_bytes(self.config_path)
        current_hash = _sha256(current) if current is not None else _MISSING_HASH
        managed_hash = state["managed_sha256"]
        if current_hash == managed_hash:
            self._restore_from_backup(state, backup)
            return True
        if current_hash == original_hash:
            self._remove_artifacts()
            return True
        if current is not None:
            rebased = _rebase_managed_config(current, backup, state)
            if rebased is None:
                if auto_archive_orphan:
                    self._archive_artifacts()
                    return True
                raise CodexConfigConflictError(
                    "Codex 配置已被外部修改并替换，未覆盖；请确认后再处理备份和状态文件"
                )
            if rebased != current:
                if _current_bytes(self.config_path) != current:
                    raise CodexConfigConflictError(
                        "Codex 配置在恢复期间被外部修改，未覆盖；备份已保留"
                    )
                _atomic_write(
                    self.config_path,
                    rebased,
                    mode=int(state.get("original_mode") or 0o600),
                )
                if _current_bytes(self.config_path) != rebased:
                    raise OSError("自动整理后的 Codex 配置校验不一致，备份已保留")
            self._remove_artifacts()
            return True
        raise CodexConfigConflictError(
            "Codex 配置已被外部修改，未覆盖；请人工确认后再清理 .model-router.bak 和状态文件"
        )

    def _restore_locked(self) -> None:
        self._validate_paths()
        if self.backup_path is None or not self.state_path.exists():
            raise CodexConfigConflictError("找不到完整的 Codex 接管备份，未恢复")
        self._recover_previous_locked(auto_archive_orphan=False)

    def _restore_from_backup(self, state: dict, backup: bytes) -> None:
        original_exists = bool(state.get("original_exists"))
        if original_exists:
            mode = int(state.get("original_mode") or 0o600)
            _atomic_write(self.config_path, backup, mode=mode)
            if _current_bytes(self.config_path) != backup:
                raise OSError("恢复后的 Codex 配置校验不一致，备份已保留")
        else:
            if _lexists(self.config_path):
                if self.config_path.is_symlink() or not self.config_path.is_file():
                    raise CodexConfigConflictError("Codex 配置路径异常，未删除")
                self.config_path.unlink()
            if _lexists(self.config_path):
                raise OSError("无法删除临时 Codex 配置，备份已保留")
        self._remove_artifacts()

    def _remove_artifacts(self) -> None:
        self._remove_artifact(self.backup_path_for_write)
        self._remove_artifact(self.state_path)

    def _archive_artifacts(self) -> None:
        stamp = str(time.time_ns())
        backup_archive = self.backup_path_for_write.with_name(
            self.backup_path_for_write.name + f".orphan.{stamp}"
        )
        state_archive = self.state_path.with_name(self.state_path.name + f".orphan.{stamp}")
        self.backup_path_for_write.replace(backup_archive)
        try:
            self.state_path.replace(state_archive)
        except Exception:
            backup_archive.replace(self.backup_path_for_write)
            raise

    @staticmethod
    def _remove_artifact(path: Path) -> None:
        if not _lexists(path):
            return
        if path.is_symlink() or not path.is_file():
            raise CodexConfigConflictError(f"拒绝删除异常的 Codex 接管文件: {path}")
        path.unlink()


def _rebase_managed_config(current: bytes, backup: bytes, state: dict | None = None) -> bytes | None:
    """Keep external edits while removing the previous local_router takeover."""
    document = _parse_config(current)
    original = _parse_config(backup)
    state = state or {}
    if (
        not isinstance(state.get("managed_model"), str)
        or state.get("managed_model_provider") != "local_router"
        or not isinstance(state.get("managed_local_router"), dict)
    ):
        raise CodexConfigConflictError(
            "Codex 旧托管状态缺少完整字段，无法确认外部修改的归属；当前配置与备份已保留"
        )
    _validate_editable_shape(document)
    providers = document.get("model_providers")
    has_local_router = isinstance(providers, Table) and "local_router" in providers
    managed_provider = document.get("model_provider")
    provider_is_managed = (
        managed_provider is not None and managed_provider.unwrap() == "local_router"
    )
    if (not has_local_router and not provider_is_managed
            and not key_matches_managed(document, "model", state["managed_model"])):
        return None

    if has_local_router:
        original_providers = original.get("model_providers")
        original_local_router = (
            original_providers["local_router"]
            if isinstance(original_providers, Table) and "local_router" in original_providers
            else None
        )
        current_table = providers["local_router"].unwrap()
        original_table = original_local_router.unwrap() if original_local_router is not None else None
        if current_table == state["managed_local_router"]:
            if original_local_router is not None:
                providers["local_router"] = original_local_router
            else:
                providers.remove("local_router")
        elif current_table != original_table:
            raise CodexConfigConflictError(
                "Codex local_router 表已被外部修改，无法安全撤销接管；当前配置与备份已保留"
            )
    managed_model = state["managed_model"]
    managed_provider_name = state["managed_model_provider"]
    if key_matches_managed(document, "model", managed_model):
        _restore_string_key(document, original, "model")
    if key_matches_managed(document, "model_provider", managed_provider_name):
        _restore_string_key(document, original, "model_provider")

    return document.as_string().encode("utf-8")


def key_matches_managed(document: TOMLDocument, key: str, managed: object) -> bool:
    return managed is not None and key in document and document[key].unwrap() == managed


def _restore_string_key(document: TOMLDocument, original: TOMLDocument, key: str) -> None:
    if key in original:
        document[key] = original[key].unwrap()
    elif key in document:
        document.pop(key)


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink(path, "目标文件")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as temporary_file:
            temporary_file.write(data)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary_path.unlink(missing_ok=True)
