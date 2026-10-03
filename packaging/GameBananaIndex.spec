# Build only from the clean exported source tree; never collect working directories.
from pathlib import Path, PureWindowsPath
import os
import re
import sys
import pefile

# Keep unrelated software/tool DLLs on the caller's PATH out of the bundle.
# Windows' ICU and UCRT/API-set libraries must be provided by Windows itself.
os.environ['PATH'] = os.pathsep.join([
    str(Path(sys.executable).parent), sys.base_prefix,
    str(Path(sys.base_prefix) / 'DLLs'),
    str(Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32'),
    os.environ.get('SystemRoot', 'C:/Windows'),
])

root = Path(SPECPATH).parent
version = re.search(r'__version__\s*=\s*"([0-9.]+)"',
                    (root / 'client/__init__.py').read_text(encoding='utf-8')).group(1)
datas = [
    (str(root / 'client/webpage/translate.js'), 'client/webpage'),
    (str(root / 'client/translate/glossary.json'), 'client/translate'),
    (str(root / 'client/translate/skip_rules.json'), 'client/translate'),
]
a = Analysis([str(root / 'packaging/launcher.py')], pathex=[str(root)],
             binaries=[], datas=datas,
             hiddenimports=['PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
                            'PySide6.QtWebChannel'],
             hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=['tkinter', 'unittest', 'pip', 'setuptools'], noarchive=False)
a.binaries = [item for item in a.binaries
              if PureWindowsPath(item[0]).name.lower() not in
                 {'icuuc.dll', 'icuin.dll', 'icu.dll', 'ucrtbase.dll'}
              and not PureWindowsPath(item[0]).name.lower().startswith(('api-ms-', 'ext-ms-'))]

# Qt's QML hook collects every installed optional module by default. This app
# uses QWidget/QWebEngineWidget and no QML files. Keep its native dependency
# closure, not unrelated Charts/Quick3D/VirtualKeyboard etc. from the wheel.
def normalized(value):
    return value.lower().replace('\\', '/')

plugin_categories = {'platforms', 'imageformats', 'iconengines', 'styles',
                     'tls', 'networkinformation', 'printsupport', 'platformthemes'}

def is_seed(item):
    name = normalized(item[0])
    if '/qml/' in name:
        return False
    if not name.startswith('pyside6/'):
        return True
    if '/plugins/' in name:
        return name.split('/plugins/', 1)[1].split('/', 1)[0] in plugin_categories
    return (name.endswith('.pyd') or name.endswith('/qtwebengineprocess.exe')
            or PureWindowsPath(name).name in
            {'pyside6.abi3.dll', 'd3dcompiler_47.dll', 'opengl32sw.dll', 'dxcompiler.dll', 'dxil.dll'})

by_basename = {}
for item in a.binaries:
    by_basename.setdefault(PureWindowsPath(item[0]).name.lower(), []).append(item)
queue = [item for item in a.binaries if is_seed(item)]
selected = {}
while queue:
    item = queue.pop()
    if item[0] in selected:
        continue
    selected[item[0]] = item
    image = pefile.PE(item[1], fast_load=True)
    image.parse_data_directories(directories=[
        pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_IMPORT'],
        pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT']])
    for imported in (getattr(image, 'DIRECTORY_ENTRY_IMPORT', [])
                     + getattr(image, 'DIRECTORY_ENTRY_DELAY_IMPORT', [])):
        queue.extend(by_basename.get(imported.dll.decode('ascii').lower(), []))
    image.close()
a.binaries = list(selected.values())
a.datas = [item for item in a.datas if '/qml/' not in normalized(item[0])]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
          name=f'GameBananaIndex-v{version}-win-x64',
          debug=False, strip=False, upx=False, console=False,
          version=str(root / 'packaging/version_info.txt'),
          uac_admin=False, uac_uiaccess=False)
