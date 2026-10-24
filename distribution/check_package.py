"""Offline acceptance check for the shipped runtime. No ports/cameras are opened."""
import argparse
import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-files',action='store_true')
    args=parser.parse_args()
    checks={}
    for name in ('numpy','cv2','matplotlib','PIL','tkinter','mediapipe',
                 'cv2_enumerate_cameras','serial','pymavlink.mavutil'):
        module=importlib.import_module(name)
        location=Path(module.__file__).resolve()
        if not location.is_relative_to(ROOT):
            raise RuntimeError(f'External dependency: {name}: {location}')
        checks[name]=str(location.relative_to(ROOT))
    from bench_app.model import validate_profile,CAMERA_CONFIG,python_executable
    from bench_app.data import load_campaign
    config=validate_profile(CAMERA_CONFIG)
    if not Path(config['reference_image']).resolve().is_relative_to(ROOT):
        raise RuntimeError('Calibration reference points outside the package')
    checks['reference_image']=config['reference_image']
    checks['history_rows']=len(load_campaign())
    checks['child_python']=python_executable()
    if Path(python_executable()).resolve()!=Path(sys.executable).resolve():
        raise RuntimeError('Child interpreter is not the shipped runtime')
    for relative in ('px4_mvp/timed_hold.py','cv_mvp/instrument_live.py'):
        completed=subprocess.run([sys.executable,'-I',str(ROOT/relative),'--help'],
                                  capture_output=True,text=True,encoding='utf-8',timeout=45)
        if completed.returncode:
            raise RuntimeError(relative+': '+completed.stderr)
        checks[relative]='argument parsing passed; no hardware access'
    import tkinter as tk
    window=tk.Tk();window.withdraw();window.update();window.destroy()
    checks['tk_window']='created and closed'
    if args.verify_files:
        entries=json.loads((ROOT/'FILE_MANIFEST.json').read_text(encoding='utf-8'))
        mutable=set(entries['path_migrated_files'])
        for item in entries['files']:
            if item['path'] in mutable:continue
            file=ROOT/item['path']
            if hashlib.sha256(file.read_bytes()).hexdigest()!=item['sha256']:
                raise RuntimeError('File differs from delivery: '+item['path'])
        checks['immutable_file_hashes']='passed'
    result={'status':'passed','motor_commands':0,'camera_opened':False,'checks':checks}
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
