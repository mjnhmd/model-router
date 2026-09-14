from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_spec_bundles_web_assets_but_not_user_config():
    spec = (ROOT / "ModelRouter.spec").read_text(encoding="utf-8")
    assert '"web" / "index.html"' in spec
    assert '"web" / "console.js"' in spec
    assert "config.example.yaml" in spec
    assert '"config.yaml"' not in spec
    assert '"config.state.yaml"' not in spec


def test_build_script_uses_windowed_pyinstaller():
    script = (ROOT / "scripts/build_app.sh").read_text(encoding="utf-8")
    assert "ModelRouter" in script
    spec = (ROOT / "ModelRouter.spec").read_text(encoding="utf-8")
    assert "console=False" in spec


def test_release_script_creates_installable_macos_artifacts():
    script = (ROOT / "scripts/package_release.sh").read_text(encoding="utf-8")
    assert "ditto" in script
    assert "hdiutil create" in script
    assert "/Applications" in script
    assert 'release="ModelRouter-macos-${arch}"' in script
    assert '"dist/${release}.dmg"' in script


def test_wheel_declares_embedded_console_resources():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    force_include = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert force_include["web"] == "model_router/web"
    api = (ROOT / "src/model_router/api.py").read_text(encoding="utf-8")
    assert "Path(__file__).resolve().parent / \"web\"" in api


def test_package_version_fallback_matches_project_metadata():
    import model_router

    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    spec = (ROOT / "ModelRouter.spec").read_text(encoding="utf-8")

    assert model_router.__version__ == metadata["project"]["version"]
    # 打包 App 没有发行元数据，必须回落到包内版本，否则界面显示 0.0.0
    assert '"desktop_entry.py"' in spec
    from model_router import api

    assert api.CURRENT_VERSION == metadata["project"]["version"]
