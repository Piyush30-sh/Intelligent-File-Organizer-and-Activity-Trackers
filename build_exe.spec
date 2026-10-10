# -*- mode: python ; coding: utf-8 -*-

import os

project_dir = os.path.dirname(os.path.abspath(SPEC))

a = Analysis(
    [os.path.join(project_dir, 'finalprojectcode.py')],
    pathex=[project_dir],
    binaries=[],
    datas=[
        (os.path.join(project_dir, 'activity.ico'),  '.'),
        (os.path.join(project_dir, 'activity.png'),  '.'),
        (os.path.join(project_dir, 'upscalemedia-transformed.jpeg'), '.'),
    ],
    hiddenimports=[
        'database',
        'folder_guard',
        'session',
        'pymongo',
        'pymongo.monitoring',
        'pymongo.uri_parser',
        'pymongo.auth',
        'pymongo.ssl_support',
        'dns',
        'dns.resolver',
        'plyer',
        'plyer.platforms.win.notification',
        'watchdog',
        'watchdog.observers',
        'watchdog.observers.winapi',
        'watchdog.events',
        'tkinter',
        'tkinter.filedialog',
        'tkinter.scrolledtext',
        'tkinter.messagebox',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['streamlit'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Intelligent FO & AT',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # No console window (GUI app)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(project_dir, 'activity.ico'),
    onefile=True,           # Single .exe file
)
