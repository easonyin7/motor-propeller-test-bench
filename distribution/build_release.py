"""Assemble offline Windows packages from the verified local Python environment.

Run with the project's CV Python. Nothing opens a camera or serial port.
Existing output folders are never overwritten or recursively deleted.
"""
import argparse
import csv
from datetime import datetime,timezone,timedelta
import hashlib
from importlib import metadata
import json
from pathlib import Path
import shutil
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
VERSION='0.1.0-20261020'
SKIP_DIRS={'__pycache__','.git','.venv','.pytest_cache','releases'}


def copy_tree(source,dest,extra_skip=()):
    def ignore(directory,names):
        return [name for name in names if name in SKIP_DIRS or name in extra_skip
                or name.endswith(('.pyc','.pyo','.tmp'))]
    shutil.copytree(source,dest,ignore=ignore,dirs_exist_ok=True)


def json_file(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')


def copy_runtime(destination):
    base=Path(sys.base_prefix)
    if sys.version_info[:2]!=(3,12) or sys.platform!='win32' or sys.maxsize<=2**32:
        raise RuntimeError('Build with the verified Windows x64 Python 3.12 environment')
    destination.mkdir(parents=True)
    for file in base.iterdir():
        if file.is_file() and file.suffix.lower() in ('.exe','.dll','.txt'):
            shutil.copy2(file,destination/file.name)
    for name in ('Lib','DLLs','tcl'):
        copy_tree(base/name,destination/name,extra_skip=('site-packages',))
    packages=destination/'Lib/site-packages'
    copy_tree(Path(sys.prefix)/'Lib/site-packages',packages)
    copy_tree(ROOT/'px4_mvp/.deps',packages,extra_skip=('bin',))
    # _pth isolates imports from system/user Python and makes all paths relative
    # to this interpreter. The application directory is one level above it.
    (destination/'python312._pth').write_text(
        '.\nDLLs\nLib\nLib/site-packages\n..\n../px4_mvp\n../cv_mvp\nimport site\n',encoding='utf-8')
    (packages/'sitecustomize.py').write_text(
        'from pathlib import Path\nimport os,sys\n'
        'p=Path(sys.executable).resolve().parent\n'
        'os.environ["TCL_LIBRARY"]=str(p/"tcl/tcl8.6")\n'
        'os.environ["TK_LIBRARY"]=str(p/"tcl/tk8.6")\n',encoding='utf-8')


def documentation(destination,kind):
    copy_tree(ROOT/'distribution',destination/'distribution')
    for name in ('DELIVERY_README.md','部署与验收清单.md','开发交接说明.md'):
        shutil.copy2(ROOT/'distribution'/name,destination/name)
    shutil.copy2(ROOT/'bench_app/上位机操作指南.md',destination/'上位机操作指南.md')
    shutil.copy2(ROOT/'启动上位机.cmd',destination/'启动上位机.cmd')
    shutil.copy2(ROOT/'distribution/离线自检.cmd',destination/'distribution/离线自检.cmd')
    # An ASCII-named entry remains usable even on systems with different legacy
    # command-prompt code pages. The app/documents themselves retain Chinese UI.
    (destination/'Start_Bench.cmd').write_text(
        '@echo off\nsetlocal\ncd /d "%~dp0"\n'
        '"runtime\\python.exe" -I "distribution\\prepare_package.py"\n'
        'if errorlevel 1 (\n  echo Package preparation failed.\n  pause\n  exit /b 1\n)\n'
        'start "" "runtime\\pythonw.exe" -m bench_app\n',encoding='ascii')
    json_file(destination/'package_info.json',dict(
        version=VERSION,kind=kind,target='Windows x64',python_version=sys.version,
        original_project_root=str(ROOT),prepared_root=None,
        assembled_at=datetime.now(timezone(timedelta(hours=8))).isoformat(),
        hardware_testing_during_packaging=False))


def dependency_notices(destination):
    entries=[]
    for dist in metadata.distributions(path=[str(destination/'runtime/Lib/site-packages')]):
        entries.append(dict(name=dist.metadata.get('Name','unknown'),version=dist.version,
                            license=(dist.metadata.get('License-Expression') or
                                     dist.metadata.get('License') or 'See bundled metadata/licenses')))
    entries.sort(key=lambda entry:entry['name'].lower())
    json_file(destination/'distribution/dependency_versions.json',entries)
    lines=['# 随附第三方组件','',
           'Python及其许可见runtime/LICENSE.txt。第三方原始包元数据及许可文件随runtime/Lib/site-packages保留；下表不替代各组件许可条款。',
           '', '| 组件 | 版本 | 许可标识 / 摘要 |','|---|---|---|']
    for entry in entries:
        license=' '.join(str(entry['license']).split()).replace('|','/')[:160]
        lines.append(f"| {entry['name']} | {entry['version']} | {license} |")
    (destination/'THIRD_PARTY_NOTICES.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def portable_profile(destination):
    value=json.loads((ROOT/'cv_mvp/instrument_config.json').read_text(encoding='utf-8'))
    reference=Path(value['reference_image'])
    if not reference.is_absolute():reference=ROOT/'cv_mvp'/reference
    target=destination/'cv_mvp/calibration/release_reference.png'
    target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(reference,target)
    value['reference_image']='calibration/release_reference.png'
    json_file(destination/'cv_mvp/instrument_config.json',value)


def assemble(output):
    if output.exists():raise RuntimeError('Output already exists; choose a new directory')
    output.mkdir(parents=True)
    portable=output/'MotorBench_0.1_Windows_x64'
    source=output/'MotorBench_0.1_完整工程'
    portable.mkdir();source.mkdir()
    for name in ('bench_app','cv_mvp','px4_mvp'):
        target=portable/name;target.mkdir()
        for file in (ROOT/name).iterdir():
            if file.is_file() and file.suffix.lower() in ('.py','.md','.txt','.json','.cmd','.ps1'):
                shutil.copy2(file,target/file.name)
    historical=portable/'integration_runs'
    copy_tree(ROOT/'integration_runs/campaign_summary_20261018',
              historical/'campaign_summary_20261018')
    with (ROOT/'integration_runs/campaign_summary_20261018/全档位数据_合并.csv').open(
            encoding='utf-8-sig',newline='') as stream:
        runs={row['run'] for row in csv.DictReader(stream)}
    for name in sorted(runs):
        path=(ROOT/'integration_runs'/name).resolve()
        if not path.is_relative_to((ROOT/'integration_runs').resolve()):
            raise RuntimeError('History path escapes integration_runs')
        copy_tree(path,historical/name)
    portable_profile(portable)
    documentation(portable,'portable_runtime')
    print('Copying verified Python runtime and dependencies...',flush=True)
    copy_runtime(portable/'runtime')
    dependency_notices(portable)
    # Include the complete project sources, backups and original measurement
    # evidence. A full portable runtime replaces the non-movable source venv.
    print('Copying full project and historical evidence...',flush=True)
    for item in ROOT.iterdir():
        if item.name in SKIP_DIRS:continue
        target=source/item.name
        if item.is_dir():
            extra=('runtime',) if item.name=='bench_app' else ()
            copy_tree(item,target,extra_skip=extra)
        elif item.is_file():shutil.copy2(item,target)
    copy_tree(portable/'runtime',source/'runtime')
    portable_profile(source)
    documentation(source,'full_project')
    dependency_notices(source)
    json_file(output/'build_info.json',dict(version=VERSION,portable=str(portable),source=str(source)))
    print(json.dumps({'portable':str(portable),'source':str(source)},ensure_ascii=False),flush=True)


def finalize(output):
    info=json.loads((output/'build_info.json').read_text(encoding='utf-8'))
    archives=[]
    for key in ('portable','source'):
        root=Path(info[key])
        files=[];mutable=['package_info.json']
        for path in sorted(root.rglob('*')):
            if not path.is_file() or path.name=='FILE_MANIFEST.json' or '__pycache__' in path.parts:continue
            relative=path.relative_to(root).as_posix()
            if relative=='cv_mvp/instrument_config.json' or (
                    relative.startswith('bench_app/runs/') and path.name in ('session.json','camera_config.json')):
                mutable.append(relative)
            files.append(dict(path=relative,bytes=path.stat().st_size,
                              sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        json_file(root/'FILE_MANIFEST.json',dict(
            algorithm='sha256',path_migrated_files=mutable,files=files,
            note='Hashes describe delivery state. Migrated indexes may change on first launch; raw evidence is unchanged.'))
        archive=output/(root.name+'.zip')
        if archive.exists():raise RuntimeError('Archive already exists')
        print('Compressing '+archive.name,flush=True)
        with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as z:
            for path in sorted(root.rglob('*')):
                if path.is_file() and '__pycache__' not in path.parts:
                    z.write(path,arcname=root.name+'/'+path.relative_to(root).as_posix())
        with zipfile.ZipFile(archive) as z:
            corrupt=z.testzip()
            if corrupt:raise RuntimeError('Corrupt ZIP member: '+corrupt)
        archives.append(dict(file=archive.name,bytes=archive.stat().st_size,
                             sha256=hashlib.sha256(archive.read_bytes()).hexdigest()))
        print(json.dumps(archives[-1],ensure_ascii=False),flush=True)
    json_file(output/'SHA256.json',archives)
    (output/'SHA256SUMS.txt').write_text(''.join(
        f"{entry['sha256']}  {entry['file']}\n" for entry in archives),encoding='utf-8')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('assemble','finalize'))
    parser.add_argument('--output',type=Path,default=ROOT/'releases/0.1.0-20261020')
    args=parser.parse_args()
    (assemble if args.action=='assemble' else finalize)(args.output.resolve())
