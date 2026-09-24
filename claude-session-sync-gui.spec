# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['D:/Best Projects/Claude_acc_sync/build/entry_gui.py'],
    pathex=['D:/Best Projects/Claude_acc_sync'],
    binaries=[],
    datas=[('D:/Best Projects/Claude_acc_sync/claude_session_sync/ui', 'claude_session_sync/ui')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='claude-session-sync-gui',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
