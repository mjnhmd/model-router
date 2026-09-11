import json
import os
from pathlib import Path

import pytest

import model_router.codex_session as codex_session_module
from model_router.codex_session import CodexConfigConflictError, CodexSession


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    test_home = tmp_path / "home"
    test_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: test_home)
    original_init = CodexSession.__init__

    def guarded_init(self, config_path):
        assert Path(config_path).expanduser().resolve().is_relative_to(tmp_path.resolve())
        original_init(self, config_path)

    monkeypatch.setattr(CodexSession, "__init__", guarded_init)


def test_start_preserves_unrelated_config_and_switches_active_provider(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text(
        'model = "old-model"\nmodel_provider = "custom"\n'
        '[model_providers.custom]\nname = "custom"\n',
        encoding="utf-8",
    )

    session = CodexSession(config)
    session.start(port=8765, public_model="route-fastest")

    text = config.read_text(encoding="utf-8")
    assert 'model_provider = "local_router"' in text
    assert 'model = "route-fastest"' in text
    assert 'wire_api = "responses"' in text
    assert "requires_openai_auth = false" in text
    assert '[model_providers.custom]' in text
    assert session.backup_path is not None
    assert session.backup_path.read_text(encoding="utf-8").startswith('model = "old-model"')


def test_start_replaces_existing_managed_provider(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text(
        'model = "old-model"\n'
        '[model_providers.local_router]\nname = "old"\n'
        '[model_providers.local_router.extra]\nvalue = true\n'
        '[model_providers.other]\nname = "other"\n',
        encoding="utf-8",
    )

    session = CodexSession(config)
    session.start(port=9876, public_model="router/model")

    text = config.read_text(encoding="utf-8")
    assert text.count("[model_providers.local_router]") == 1
    assert "[model_providers.local_router.extra]" not in text
    assert 'base_url = "http://127.0.0.1:9876/v1"' in text
    assert '[model_providers.other]' in text


def test_restore_returns_original_bytes(tmp_path: Path):
    config = tmp_path / "config.toml"
    original = b'model = "old-model"\nmodel_provider = "custom"\n'
    config.write_bytes(original)

    session = CodexSession(config)
    session.start(8765, "route-fastest")
    session.restore()

    assert config.read_bytes() == original
    assert not session.active
    assert not (tmp_path / "config.toml.model-router.bak").exists()


def test_recover_previous_restores_leftover_backup(tmp_path: Path):
    config = tmp_path / "config.toml"
    original = b'model = "old-model"\nmodel_provider = "custom"\n'
    config.write_bytes(original)

    session = CodexSession(config)
    session.start(8765, "route-fastest")
    assert session.backup_path is not None
    # 模拟进程崩溃：文件备份保留，但进程锁已由操作系统释放。
    session._release_lock()

    recovered = CodexSession(config).recover_previous()

    assert recovered
    assert config.read_bytes() == original
    assert not (tmp_path / "config.toml.model-router.bak").exists()


def test_missing_config_is_removed_on_restore(tmp_path: Path):
    config = tmp_path / "config.toml"
    session = CodexSession(config)

    session.start(8765, "route-fastest")
    assert config.exists()
    session.restore()

    assert not config.exists()
    assert not session.active


def test_start_is_idempotent_while_session_is_active(tmp_path: Path):
    config = tmp_path / "config.toml"
    original = b'model = "old-model"\n'
    config.write_bytes(original)
    session = CodexSession(config)

    session.start(8765, "route-fastest")
    first_backup = session.backup_path.read_bytes()
    first_config = config.read_bytes()
    session.start(9999, "another-model")

    assert session.backup_path.read_bytes() == first_backup
    assert config.read_bytes() == first_config


def test_invalid_toml_is_rejected_without_side_effects(tmp_path: Path):
    config = tmp_path / "config.toml"
    original = b'model = "old-model"\n[broken\n'
    config.write_bytes(original)

    with pytest.raises(ValueError, match="TOML"):
        CodexSession(config).start(8765, "route-fastest")

    assert config.read_bytes() == original
    assert not (tmp_path / "config.toml.model-router.bak").exists()
    assert not (tmp_path / "config.toml.model-router.state.json").exists()


def test_symlink_config_is_rejected_without_following_link(tmp_path: Path):
    target = tmp_path / "real-config.toml"
    target.write_text('model = "real"\n', encoding="utf-8")
    config = tmp_path / "config.toml"
    config.symlink_to(target)

    with pytest.raises(ValueError, match="软链接"):
        CodexSession(config).start(8765, "route-fastest")

    assert config.is_symlink()
    assert target.read_text(encoding="utf-8") == 'model = "real"\n'


def test_structured_edit_preserves_comments_and_unrelated_tables(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text(
        '# keep this comment\n'
        'model = "old-model" # replace only this value\n'
        'model_provider = "custom"\n'
        '\n[model_providers.custom]\n'
        'name = "custom"\n'
        '\n[[mcp_servers]]\n'
        'name = "keep-array-table"\n'
        '\n[model_providers.local_router]\n'
        'name = "old-local"\n'
        '\n[model_providers.other.nested]\n'
        'value = true\n',
        encoding="utf-8",
    )

    session = CodexSession(config)
    session.start(8765, "route-fastest")
    text = config.read_text(encoding="utf-8")

    assert "# keep this comment" in text
    assert "# replace only this value" in text
    assert '[model_providers.custom]' in text
    assert 'name = "custom"' in text
    assert '[[mcp_servers]]' in text
    assert 'name = "keep-array-table"' in text
    assert '[model_providers.other.nested]' in text
    assert 'value = true' in text
    assert 'name = "old-local"' not in text


def test_recovery_archives_external_replacement_without_overwriting_it(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text('model = "old-model"\n', encoding="utf-8")
    session = CodexSession(config)
    session.start(8765, "route-fastest")
    session._release_lock()

    external = b'model = "edited-by-user"\n'
    config.write_bytes(external)

    assert CodexSession(config).recover_previous()
    assert config.read_bytes() == external
    assert session.backup_path is None
    assert list(tmp_path.glob("config.toml.model-router.bak.orphan.*"))
    assert list(tmp_path.glob("config.toml.model-router.state.json.orphan.*"))


def test_recovery_restores_original_managed_fields_but_keeps_other_edits(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text('model = "old-model"\nmodel_provider = "custom"\n', encoding="utf-8")
    session = CodexSession(config)
    session.start(8765, "route-fastest")
    session._release_lock()
    config.write_text(
        'model = "route-fastest"\n'
        'model_provider = "local_router"\n'
        'model_context_window = 1000000\n'
        '[model_providers.local_router]\n'
        'name = "local_router"\n'
        'wire_api = "responses"\n'
        'requires_openai_auth = false\n'
        'base_url = "http://127.0.0.1:8765/v1"\n',
        encoding="utf-8",
    )

    assert CodexSession(config).recover_previous()
    assert config.read_text(encoding="utf-8") == (
        'model = "old-model"\nmodel_provider = "custom"\n'
        'model_context_window = 1000000\n'
    )


def test_recovery_keeps_external_provider_and_model_selection(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text('model = "old-model"\nmodel_provider = "custom"\n', encoding="utf-8")
    session = CodexSession(config)
    session.start(8765, "route-fastest")
    session._release_lock()
    edited = config.read_text().replace('model = "route-fastest"', 'model = "user-model"')
    edited = edited.replace('model_provider = "local_router"', 'model_provider = "user-provider"')
    config.write_text(edited + '[model_providers.user-provider]\nname = "user-provider"\n')

    assert CodexSession(config).recover_previous()
    text = config.read_text(encoding="utf-8")
    assert 'model = "user-model"' in text
    assert 'model_provider = "user-provider"' in text
    assert "[model_providers.local_router]" not in text


@pytest.mark.parametrize("preexisting", [False, True])
def test_recovery_preserves_edited_provider_and_backup_on_conflict(tmp_path, preexisting):
    config = tmp_path / "config.toml"
    original = 'model = "old-model"\nmodel_provider = "custom"\n'
    if preexisting:
        original += '[model_providers.local_router]\nname = "user-router"\n'
    config.write_text(original)
    session = CodexSession(config)
    session.start(8765, "route-fastest")
    session._release_lock()
    external = config.read_text().replace("http://127.0.0.1:8765/v1", "http://user-router/v1")
    config.write_text(external)

    with pytest.raises(CodexConfigConflictError, match="local_router"):
        CodexSession(config).recover_previous()

    assert config.read_text() == external
    assert session.backup_path.read_text() == original
    assert session.state_path.exists()


@pytest.mark.parametrize("has_provider_table", [False, True])
def test_legacy_recovery_keeps_backup_when_managed_model_is_unknown(tmp_path, has_provider_table):
    config = tmp_path / "config.toml"
    config.write_text('model = "old-model"\nmodel_provider = "custom"\n')
    session = CodexSession(config)
    session.start(8765, "route-fastest")
    session._release_lock()
    state = json.loads(session.state_path.read_text())
    for key in list(state):
        if key.startswith("managed_") and key != "managed_sha256":
            del state[key]
    session.state_path.write_text(json.dumps(state))
    external = 'model = "route-fastest"\nmodel_provider = "custom"\n'
    if has_provider_table:
        external += '[model_providers.local_router]\nname = "local_router"\n'
    config.write_text(external)

    if has_provider_table:
        with pytest.raises(CodexConfigConflictError, match="托管"):
            CodexSession(config).recover_previous()
    else:
        assert CodexSession(config).recover_previous()

    assert config.read_text() == external
    if has_provider_table:
        assert session.backup_path is not None
    else:
        assert session.backup_path is None
        assert list(tmp_path.glob("config.toml.model-router.bak.orphan.*"))


def test_recovery_restores_managed_model_when_external_tool_removed_provider_table(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('model = "old-model"\nmodel_provider = "custom"\n')
    session = CodexSession(config)
    session.start(8765, "route-fastest")
    session._release_lock()
    config.write_text('model = "route-fastest"\nmodel_provider = "user-provider"\n')

    assert CodexSession(config).recover_previous()

    assert config.read_text() == 'model = "old-model"\nmodel_provider = "user-provider"\n'


def test_normal_close_rebases_unrelated_edit_and_restores_original_selection(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('model = "old-model"\nmodel_provider = "custom"\n')
    session = CodexSession(config)
    session.start(8765, "route-fastest")
    config.write_text("# external comment\n" + config.read_text())

    session.restore()

    text = config.read_text()
    assert 'model = "old-model"' in text
    assert '# external comment' in text
    assert 'local_router' not in text
    assert session.backup_path is None


def test_recovery_rechecks_current_file_before_rebase_write(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('model = "old-model"\nmodel_provider = "custom"\n')
    session = CodexSession(config)
    session.start(8765, "route-fastest")
    session._release_lock()
    config.write_text(config.read_text() + "\n# external comment\n")
    real_rebase = codex_session_module._rebase_managed_config
    external = b'model = "edited-during-recovery"\n'

    def mutate_after_rebase(*args):
        rendered = real_rebase(*args)
        config.write_bytes(external)
        return rendered

    monkeypatch.setattr(codex_session_module, "_rebase_managed_config", mutate_after_rebase)
    with pytest.raises(CodexConfigConflictError, match="外部修改"):
        CodexSession(config).recover_previous()

    assert config.read_bytes() == external
    assert session.backup_path is not None


def test_recovery_keeps_new_user_fields_when_original_config_was_missing(tmp_path):
    config = tmp_path / "config.toml"
    session = CodexSession(config)
    session.start(8765, "route-fastest")
    session._release_lock()
    config.write_text('theme = "dark"\n' + config.read_text())

    assert CodexSession(config).recover_previous()

    assert codex_session_module.tomllib.loads(config.read_text()) == {"theme": "dark"}
    assert session.backup_path is None


def test_legacy_backup_still_restores_when_current_fingerprint_matches(tmp_path):
    config = tmp_path / "config.toml"
    original = b'model = "original"\n'
    config.write_bytes(original)
    session = CodexSession(config)
    session.start(8765, "route-fastest")
    session._release_lock()
    state = json.loads(session.state_path.read_text())
    for key in list(state):
        if key.startswith("managed_") and key != "managed_sha256":
            del state[key]
    session.state_path.write_text(json.dumps(state))

    assert CodexSession(config).recover_previous()
    assert config.read_bytes() == original


def test_restore_rejects_modified_backup_when_original_config_was_missing(tmp_path):
    config = tmp_path / "config.toml"
    session = CodexSession(config)
    session.start(8765, "route-fastest")
    managed = config.read_bytes()
    session.backup_path.write_bytes(b'model = "unexpected-backup"\n')

    with pytest.raises(CodexConfigConflictError, match="备份"):
        session.restore()

    assert config.read_bytes() == managed
    assert session.backup_path is not None


def test_recovery_preserves_preexisting_local_router_provider(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text(
        'model = "old-model"\nmodel_provider = "custom"\n'
        '[model_providers.local_router]\nname = "preexisting"\nbase_url = "http://user/v1"\n',
        encoding="utf-8",
    )
    session = CodexSession(config)
    session.start(8765, "route-fastest")
    session._release_lock()
    config.write_text(config.read_text(encoding="utf-8") + "\n# user edit\n", encoding="utf-8")

    assert CodexSession(config).recover_previous()
    text = config.read_text(encoding="utf-8")
    assert '[model_providers.local_router]' in text
    assert 'base_url = "http://user/v1"' in text


def test_second_session_cannot_take_over_while_first_holds_lock(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text('model = "old-model"\n', encoding="utf-8")
    first = CodexSession(config)
    first.start(8765, "route-fastest")

    with pytest.raises(RuntimeError, match="已被占用"):
        CodexSession(config).start(8765, "route-fastest")

    first.restore()


def test_restore_preserves_original_file_mode(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text('model = "old-model"\n', encoding="utf-8")
    os.chmod(config, 0o640)
    session = CodexSession(config)

    session.start(8765, "route-fastest")
    assert config.stat().st_mode & 0o777 == 0o640
    session.restore()

    assert config.stat().st_mode & 0o777 == 0o640


def test_start_rolls_back_if_post_write_validation_fails(tmp_path: Path, monkeypatch):
    config = tmp_path / "config.toml"
    original = b'model = "old-model"\n'
    config.write_bytes(original)
    real_parse = codex_session_module._parse_config

    def fail_after_write(raw):
        if raw != original:
            raise ValueError("模拟写入后校验失败")
        return real_parse(raw)

    monkeypatch.setattr(codex_session_module, "_parse_config", fail_after_write)

    with pytest.raises(ValueError, match="模拟写入后校验失败"):
        CodexSession(config).start(8765, "route-fastest")

    assert config.read_bytes() == original
    assert not (tmp_path / "config.toml.model-router.bak").exists()
    assert not (tmp_path / "config.toml.model-router.state.json").exists()


def test_start_aborts_if_config_changes_before_atomic_replace(tmp_path: Path, monkeypatch):
    config = tmp_path / "config.toml"
    original = b'model = "old-model"\n'
    config.write_bytes(original)
    real_atomic_write = codex_session_module._atomic_write

    def mutate_before_config_replace(path, data, mode):
        real_atomic_write(path, data, mode)
        if path == tmp_path / "config.toml.model-router.state.json":
            config.write_bytes(b'model = "edited-during-attach"\n')

    monkeypatch.setattr(codex_session_module, "_atomic_write", mutate_before_config_replace)

    with pytest.raises(CodexConfigConflictError, match="外部修改"):
        CodexSession(config).start(8765, "route-fastest")

    assert config.read_bytes() == b'model = "edited-during-attach"\n'
    assert not (tmp_path / "config.toml.model-router.bak").exists()
    assert not (tmp_path / "config.toml.model-router.state.json").exists()


def test_recovery_rejects_corrupt_original_backup(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text('model = "old-model"\n', encoding="utf-8")
    session = CodexSession(config)
    session.start(8765, "route-fastest")
    session._release_lock()
    session.backup_path.write_bytes(b"[broken\n")

    with pytest.raises(CodexConfigConflictError, match="备份"):
        CodexSession(config).recover_previous()

    assert config.read_text(encoding="utf-8").find('model = "route-fastest"') >= 0
    assert session.backup_path.exists()


def test_catalog_takeover_restores_previous_catalog_with_external_edits(tmp_path):
    config = tmp_path / 'config.toml'
    original = 'model = "old"\nmodel_catalog_json = "/old/catalog.json"\n'
    config.write_text(original)
    session = CodexSession(config)
    catalog = {"models": [{"slug": "A/model"}, {"slug": "B/model"}]}
    session.start(8765, 'A/model', catalog=catalog)
    managed = codex_session_module.tomllib.loads(config.read_text())
    catalog_path = Path(managed['model_catalog_json'])
    assert json.loads(catalog_path.read_text()) == catalog
    assert catalog_path.is_absolute()
    config.write_text('# unrelated edit\n' + config.read_text())
    session.restore()
    assert config.read_text().startswith('# unrelated edit\n')
    assert codex_session_module.tomllib.loads(config.read_text()) == codex_session_module.tomllib.loads(original)


def test_catalog_external_pointer_edit_survives_restore(tmp_path):
    config = tmp_path / 'config.toml'
    config.write_text('model = "old"\n')
    session = CodexSession(config)
    session.start(8765, 'A/model', catalog={"models": [{"slug": "A/model"}]})
    doc = codex_session_module._parse_config(config.read_bytes())
    doc['model_catalog_json'] = '/user/new-catalog.json'
    config.write_text(doc.as_string())
    session.restore()
    assert codex_session_module.tomllib.loads(config.read_text())['model_catalog_json'] == '/user/new-catalog.json'


def test_restore_after_codex_selects_another_catalog_model(tmp_path):
    config = tmp_path / 'config.toml'
    original = 'model = "original"\nmodel_provider = "custom"\n'
    config.write_text(original)
    session = CodexSession(config)
    session.start(8765, 'A/model', catalog={'models': [{'slug': 'A/model'}, {'slug': 'B/model'}]})
    config.write_text(config.read_text().replace('model = "A/model"', 'model = "B/model"'))
    session.restore()
    restored = codex_session_module.tomllib.loads(config.read_text())
    assert restored == codex_session_module.tomllib.loads(original)


def test_explicit_reconnect_archives_old_state_and_preserves_current_config(tmp_path):
    config = tmp_path / 'config.toml'
    config.write_text('model = "original"\n')
    previous = CodexSession(config)
    previous.start(8765, 'old-route')
    previous._release_lock()
    config.write_text(config.read_text().replace('127.0.0.1:8765', 'external.example:443'))
    current = config.read_bytes()
    old_backup = previous.backup_path.read_bytes()
    session = CodexSession(config)
    session.use_current_as_baseline()
    assert config.read_bytes() == current
    assert next(tmp_path.glob('*.bak.orphan.*')).read_bytes() == old_backup
    session.start(8765, 'new-route')
    session.restore()
    assert config.read_bytes() == current
