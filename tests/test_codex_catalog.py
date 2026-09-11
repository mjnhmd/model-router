"""Verify the catalog with the installed Codex parser, in an isolated home."""
import asyncio
import json
import os
from pathlib import Path
import shutil

import pytest

from model_router.codex_catalog import build_catalog
from model_router.codex_session import CodexSession


async def list_codex_models(binary, home):
    process = await asyncio.create_subprocess_exec(
        binary, 'app-server', '--stdio', cwd=home,
        env={**os.environ, 'CODEX_HOME': str(home)},
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    async def request(identifier, method, params):
        process.stdin.write((json.dumps({'id': identifier, 'method': method, 'params': params}) + '\n').encode())
        await process.stdin.drain()
        while True:
            line = await asyncio.wait_for(process.stdout.readline(), 20)
            assert line, 'Codex exited before answering'
            message = json.loads(line)
            if message.get('id') == identifier:
                assert 'error' not in message, message.get('error')
                return message['result']
    try:
        await request(1, 'initialize', {'clientInfo': {'name': 'router_catalog_test', 'version': '1.0'}})
        return (await request(2, 'model/list', {}))['data']
    finally:
        if process.returncode is None:
            process.terminate()
        await asyncio.wait_for(process.wait(), 5)


def test_installed_codex_lists_exact_mapped_catalog_and_restores(tmp_path):
    desktop = Path('/Applications/ChatGPT.app/Contents/Resources/codex')
    binary = os.environ.get('MODEL_ROUTER_TEST_CODEX') or (str(desktop) if desktop.exists() else shutil.which('codex'))
    if not binary:
        pytest.skip('Install Codex or set MODEL_ROUTER_TEST_CODEX for real parser verification')
    names = ['Service A/model-one', 'Service B/model-two']
    exposed = [{'id': name, 'key': name} for name in names]
    catalog = build_catalog(exposed, [])
    config = tmp_path / 'config.toml'
    config.write_text('model = "original"\n')
    original = config.read_bytes()
    session = CodexSession(config)
    # A provider URL and default model alone must not be confused with a picker catalog.
    session.start(8765, names[0])
    try:
        baseline = asyncio.run(list_codex_models(binary, tmp_path))
        assert not set(names).intersection(item['model'] for item in baseline)
    finally:
        session.restore()
    session.start(8765, names[0], catalog=catalog)
    try:
        models = asyncio.run(list_codex_models(binary, tmp_path))
        assert [model['model'] for model in models] == names
        assert [model['displayName'] for model in models] == names
    finally:
        session.restore()
    assert config.read_bytes() == original
