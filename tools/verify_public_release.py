"""Audit a release offline; never output a credential value or copy personal state."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import marshal
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import types
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.search(r'__version__\s*=\s*"([0-9.]+)"',
                    (ROOT / 'client/__init__.py').read_text(encoding='utf-8')).group(1)
DEFAULT_OUTPUT = ROOT.parent / f'GameBananaIndex-v{VERSION}-release'
DENIED_PARTS = {'.banana-index', '.webengine-data', '.agent', '.git', '.venv', '__pycache__',
                'webprofile', 'chromium', 'original', 'handoff', 'node_modules'}
DENIED_NAMES = {'settings.json', 'settings.broken.json', 'session-cookies.json', 'api-cache.json',
                'cookies', 'cookies-journal', 'local state', 'login data', 'cookies.json', '.env'}
USERNAME_PATH = re.compile(rb'[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s"\']+[\\/]', re.I)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def private_baseline():
    """Read only known local sensitive files. Values stay in memory and are never reported."""
    roots = {ROOT / '.banana-index', Path.home() / '.banana-index'}
    if os.environ.get('APPDATA'):
        roots.add(Path(os.environ['APPDATA']) / 'banana-index')
    if os.environ.get('BANANA_INDEX_CONFIG_DIR'):
        roots.add(Path(os.environ['BANANA_INDEX_CONFIG_DIR']))
    baseline, secrets = {}, set()
    if os.environ.get('DEEPSEEK_API_KEY'):
        secrets.add(os.environ['DEEPSEEK_API_KEY'].encode('utf-8'))

    def collect(value, field=''):
        if isinstance(value, dict):
            if field == 'cookies':
                # LiveIntent's domain cookie holds the public site hostname,
                # not a session identifier. All other cookie values still count.
                secrets.update(item.encode('utf-8') for name, item in value.items()
                               if isinstance(item, str) and len(item) >= 8
                               and not (name == '_li_dcdm_c'
                                        and item in {'gamebanana.com', '.gamebanana.com'}))
            for key, item in value.items():
                collect(item, str(key).lower())
        elif isinstance(value, list):
            for item in value:
                collect(item, field)
        elif isinstance(value, str) and len(value) >= 8:
            if any(word in field for word in ('api_key', 'token', 'secret', 'password')) or field == 'value':
                secrets.add(value.encode('utf-8'))

    for root in roots:
        for name in ['settings.json', 'session-cookies.json', 'settings.broken.json']:
            path = root / name
            if path.is_file():
                baseline[path] = sha(path)
                try:
                    collect(json.loads(path.read_text(encoding='utf-8-sig')))
                except (ValueError, OSError):
                    pass
    return baseline, secrets


def check_bytes(data, secrets, *, user_paths=True):
    assert not any(secret in data for secret in secrets), 'Sensitive value detected; content withheld'
    if user_paths:
        assert not USERNAME_PATH.search(data), 'Personal absolute user path detected; content withheld'
    # OpenSSL binaries contain PEM marker strings as parsers. Require actual
    # base64 key material, rather than flagging the marker alone as a secret.
    assert not re.search(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\s+[A-Za-z0-9+/=\r\n]{128,}', data), 'Private key material detected'


def check_name(name):
    parts = Path(name).parts
    assert not any(part.lower() in DENIED_PARTS for part in parts), 'Runtime directory in source export'
    assert Path(name).name.lower() not in DENIED_NAMES, 'Personal state file in source export'
    assert not Path(name).name.lower().endswith(('.pyc', '.pfx', '.p12', '.key', '.log', '.dmp')), 'Private/generated file in source'


def audit_exe(exe, secrets):
    """Inspect decompressed PyInstaller entries, not just the compressed outer EXE."""
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(exe))
    code_count = 0

    def check_code(code):
        nonlocal code_count
        code_count += 1
        # co_filename in a bundled module must not expose the builder's user path.
        check_bytes(code.co_filename.encode('utf-8'), secrets)
        for item in code.co_consts:
            if isinstance(item, types.CodeType):
                check_code(item)
            elif isinstance(item, str):
                check_bytes(item.encode('utf-8'), secrets)
            elif isinstance(item, bytes):
                check_bytes(item, secrets)

    for name, entry in archive.toc.items():
        lower = name.lower().replace('\\', '/')
        assert not any(part in DENIED_PARTS for part in lower.split('/')), 'Runtime state bundled in EXE'
        assert lower.rsplit('/', 1)[-1] not in DENIED_NAMES, 'Personal file bundled in EXE'
        assert lower.rsplit('/', 1)[-1] not in {'icu.dll', 'icuin.dll', 'icuuc.dll', 'ucrtbase.dll'}, 'Windows system library incorrectly bundled'
        assert '/qml/' not in lower, 'Unneeded QML package in QWidget release'
        if lower.startswith('pyside6/qt6') and lower.endswith('.dll'):
            assert not any(word in lower for word in ['charts', 'graphs', 'datavisualization', 'quick3d', 'virtualkeyboard']), 'Unused optional Qt module bundled'
        kind = entry[-1]
        if kind == 'z':
            pyz = archive.open_embedded_archive(name)
            for module in pyz.toc:
                code = pyz.extract(module)
                if isinstance(code, types.CodeType):
                    check_code(code)
            continue
        data = archive.extract(name)
        # Vendor binaries may contain upstream developer build paths. Credentials
        # are still scanned in all extracted data; project text has stricter checks.
        check_bytes(data, secrets, user_paths=lower.startswith(('client/', 'client\\')))
        if kind == 's':
            check_code(marshal.loads(data))
        if lower.endswith('base_library.zip'):
            with zipfile.ZipFile(io.BytesIO(data)) as library:
                for item in library.namelist():
                    contents = library.read(item)
                    check_bytes(contents, secrets, user_paths=False)
                    if item.endswith('.pyc'):
                        check_code(marshal.loads(contents[16:]))
    return {'bundle_entries': len(archive.toc), 'code_objects_scanned': code_count}


def zip_folder(folder, destination, prefix):
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(folder.rglob('*')):
            if path.is_file():
                archive.write(path, str(Path(prefix) / path.relative_to(folder)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release-dir', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--finalize', action='store_true', help='Create ZIPs, hashes and sanitized report after checks pass')
    parser.add_argument('--live', action='store_true', help='Also check the actual EXE against a public detail API without login')
    args = parser.parse_args()
    vendor_inputs = ROOT / 'packaging/vendor-license-inputs.json'
    if vendor_inputs.is_file():
        for relative, digest in json.loads(vendor_inputs.read_text(encoding='utf-8')).items():
            assert sha(ROOT / relative) == digest, 'Vendor license input changed since registration'
    target = args.release_dir.resolve()
    source = target / 'source'
    release = target / 'windows-x64'
    exe = release / f'GameBananaIndex-v{VERSION}-win-x64.exe'
    manifest = json.loads((target / 'EXPORT_MANIFEST.json').read_text(encoding='utf-8'))
    baseline, secrets = private_baseline()
    expected = set(manifest['source_files'])
    actual = {p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_file()}
    assert actual == expected, 'Export does not match public allowlist'
    for relative in sorted(actual):
        check_name(relative)
        check_bytes((source / relative).read_bytes(), secrets)
    assert 'MIT License' in (source / 'LICENSE').read_text(encoding='utf-8')
    assert exe.is_file()
    assert not (source / '.git').exists()
    for path in source.rglob('*.py'):
        compile(path.read_text(encoding='utf-8-sig'), path.relative_to(source).as_posix(), 'exec')
    print('PASS: public source allowlist, MIT license, syntax and credential/path audit', flush=True)
    bundled = audit_exe(exe, secrets)
    print('PASS: decompressed EXE module/data credential and runtime-state audit', flush=True)

    import pefile
    pe = pefile.PE(str(exe))
    assert pe.FILE_HEADER.Machine == 0x8664, 'EXE is not x64'
    fixed = pe.VS_FIXEDFILEINFO[0]
    assert (fixed.FileVersionMS >> 16, fixed.FileVersionMS & 0xffff,
            fixed.FileVersionLS >> 16) == tuple(map(int, VERSION.split('.')))
    # No requested elevation in the embedded Windows manifest.
    for resource in pe.DIRECTORY_ENTRY_RESOURCE.entries:
        if resource.id == 24:
            for language in resource.directory.entries:
                for item in language.directory.entries:
                    blob = pe.get_data(item.data.struct.OffsetToData, item.data.struct.Size)
                    assert b'asInvoker' in blob and b'requireAdministrator' not in blob
    pe.close()

    # Test in a new directory containing only the EXE, with no Python/.venv nearby
    # and no real credentials inherited. All runtime state is isolated.
    with tempfile.TemporaryDirectory(prefix='banana-release-check-') as temporary:
        sandbox = Path(temporary)
        clean_exe = sandbox / exe.name
        shutil.copyfile(exe, clean_exe)
        config = sandbox / 'profile'
        config.mkdir()
        env = {k: v for k, v in os.environ.items()
               if k not in {'DEEPSEEK_API_KEY', 'BANANA_INDEX_CONFIG_DIR', 'PYTHONPATH',
                            'PYTHONHOME', 'QTWEBENGINE_CHROMIUM_FLAGS', 'QT_QPA_PLATFORM'}
               and not any(word in k.upper() for word in ('API_KEY', 'TOKEN', 'SECRET', 'PASSWORD'))}
        env.update(BANANA_INDEX_CONFIG_DIR=str(config), APPDATA=str(sandbox / 'roaming'),
                   LOCALAPPDATA=str(sandbox / 'local'), USERPROFILE=str(sandbox / 'home'),
                   HOME=str(sandbox / 'home'), TEMP=str(sandbox), TMP=str(sandbox))
        report = sandbox / 'startup.json'
        completed = subprocess.run([str(clean_exe), '--release-self-test', str(report)],
                                   cwd=sandbox, env=env, timeout=90)
        result = json.loads(report.read_text(encoding='utf-8')) if report.is_file() else {}
        assert completed.returncode == 0, f'Standalone EXE health check failed: {result.get("error", completed.returncode)}'
        assert result.get('ok'), 'Standalone EXE health check failed'
        assert result['version'] == VERSION
        assert result['integrity_level'] == 8192
        for item in ['webengine', 'fresh_profile', 'native_download', 'download_controls',
                     'bundled_assets', 'window_version', 'single_close']:
            assert result.get(item), item
        if args.live:
            access_report = sandbox / 'access.json'
            access_process = subprocess.run([str(clean_exe), '--release-access-check', str(access_report)],
                                            cwd=sandbox, env=env, timeout=90)
            access = json.loads(access_report.read_text(encoding='utf-8')) if access_report.is_file() else {}
            assert access_process.returncode == 0 and access.get('ok'), 'Actual EXE anonymous public-detail check failed'
            assert access.get('anonymous') and access.get('detail_loaded') and access.get('files')
            result['anonymous_live_detail'] = True
            print('PASS: actual standalone EXE loads public detail and files without login or existing cache', flush=True)
    assert all(path.is_file() and sha(path) == previous for path, previous in baseline.items()), 'Original private files changed'
    print('PASS: isolated EXE startup, embedded browser/download, version and one-click close; private files unchanged', flush=True)

    if args.finalize:
        for name in ['LICENSE', 'THIRD_PARTY_NOTICES.md', 'PRIVACY.md']:
            shutil.copyfile(source / name, release / name)
        shutil.copytree(source / 'licenses', release / 'licenses', dirs_exist_ok=True)
        (release / '使用说明.txt').write_text(
            f'GameBanana 中文索引 v{VERSION}\n\n双击同名 EXE，无需 Python，不需要管理员权限。\n'
            '首次启动请等待内置浏览器组件解压。翻译功能在设置中填写自己的密钥。\n'
            '下载支持直接下载、取消、重试和关闭提示。源码与许可在同次发布中提供。\n', encoding='utf-8')
        source_zip = target / f'GameBananaIndex-v{VERSION}-source.zip'
        exe_zip = target / f'GameBananaIndex-v{VERSION}-win-x64.zip'
        zip_folder(source, source_zip, f'GameBananaIndex-v{VERSION}-source')
        zip_folder(release, exe_zip, f'GameBananaIndex-v{VERSION}-win-x64')
        sums = [(sha(exe), f'windows-x64/{exe.name}'), (sha(source_zip), source_zip.name), (sha(exe_zip), exe_zip.name)]
        (target / 'SHA256SUMS.txt').write_text(''.join(f'{digest}  {name}\n' for digest, name in sums), encoding='ascii')
        hashes = {p.relative_to(source).as_posix(): sha(p) for p in sorted(source.rglob('*')) if p.is_file()}
        (target / 'SOURCE_SHA256.json').write_text(json.dumps(hashes, indent=2) + '\n', encoding='utf-8')
        privacy = {'version': VERSION, 'status': 'PASS', 'source_files': len(actual), **bundled,
                   'source_allowlist': True, 'decompressed_exe_audit': True,
                   'known_private_values_not_present': True, 'private_files_unchanged': True,
                   'isolated_exe_startup': result,
                   'excluded': ['credentials', 'cookies', 'browser profiles', 'cache', 'logs',
                                'session records', 'screenshots', 'agent records', 'venv', 'git history']}
        (target / 'PRIVACY_CHECK.json').write_text(json.dumps(privacy, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print('PASS: ZIPs, SHA256 sums and sanitized audit report generated', flush=True)
    else:
        checksums = (target / 'SHA256SUMS.txt').read_text(encoding='ascii').splitlines()
        for line in checksums:
            digest, name = line.split('  ', 1)
            assert sha(target / name) == digest
        for kind, folder in [('source', source), ('win-x64', release)]:
            zpath = target / f'GameBananaIndex-v{VERSION}-{kind}.zip'
            with zipfile.ZipFile(zpath) as archive:
                assert archive.testzip() is None
                zipped = {name.split('/', 1)[1] for name in archive.namelist()}
                current = {p.relative_to(folder).as_posix() for p in folder.rglob('*') if p.is_file()}
                assert zipped == current
                for relative in zipped:
                    if kind == 'source':
                        check_name(relative)
                    # Byte identity proves ZIP entries equal already audited files.
                    info = next(x for x in archive.infolist() if x.filename.split('/', 1)[1] == relative)
                    assert hashlib.sha256(archive.read(info)).hexdigest() == sha(folder / relative)
        print('PASS: source/EXE ZIP contents and checksums match audited folders', flush=True)


if __name__ == '__main__':
    main()
