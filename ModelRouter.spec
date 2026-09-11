from pathlib import Path


project_root = Path(SPECPATH)
source_root = project_root / "src"

a = Analysis(
    [str(source_root / "model_router" / "desktop_entry.py")],
    pathex=[str(source_root)],
    binaries=[],
    datas=[
        (str(project_root / "web" / "index.html"), "web"),
        (str(project_root / "web" / "console.js"), "web"),
        (str(project_root / "config.example.yaml"), "."),
    ],
    hiddenimports=["model_router.gui"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ModelRouter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="ModelRouter",
)

app = BUNDLE(
    coll,
    name="ModelRouter.app",
    icon=None,
)
