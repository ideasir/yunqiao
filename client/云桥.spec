# -*- mode: python ; coding: utf-8 -*-
# 云桥 MCP 桌面客户端打包配置
# 用法: pyinstaller 云桥.spec
# 产物: dist/云桥/云桥.exe（目录模式，含 ui.html 等资源）

a = Analysis(
    ['desktop.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('ui.html', '.'),                    # HTML 前端（PyInstaller 解包到 _MEIPASS）
        ('custom-commands', 'custom-commands'),  # 自定义命令脚本目录
    ],
    hiddenimports=[
        'webview',
        'websockets',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='云桥',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # 无控制台窗口（GUI 模式）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='云桥',
)
