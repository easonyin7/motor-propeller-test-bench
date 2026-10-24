"""Exercise relocated copies of assembled packages without accessing hardware."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import time


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--test-root',type=Path,required=True)
    args=parser.parse_args()
    test_root=args.test_root.resolve()
    if test_root.exists():raise RuntimeError('Test root already exists')
    test_root.mkdir(parents=True)
    results=[]
    for original in sorted(args.output.resolve().glob('MotorBench*')):
        if not original.is_dir():continue
        root=test_root/original.name
        print('Copying into relocated path: '+str(root),flush=True)
        shutil.copytree(original,root,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        python=root/'runtime/python.exe'
        commands=[['-I',str(root/'distribution/prepare_package.py')],
                  ['-I',str(root/'distribution/check_package.py')],
                  ['-m','bench_app','--smoke-test']]
        if '完整工程' in original.name:
            commands += [['-m','unittest','bench_app.test_app','distribution.test_distribution'],
                         ['-m','unittest','discover','-s','px4_mvp','-p','test_*.py'],
                         ['-m','unittest','discover','-s','cv_mvp','-p','test_instrument_reader.py']]
        item={'package':original.name,'relocated_root':str(root),'commands':[]}
        for index,command in enumerate(commands):
            started=time.monotonic()
            completed=subprocess.run([str(python),'-X','utf8',*command],cwd=root,capture_output=True,
                                     text=True,encoding='utf-8',errors='replace',timeout=150)
            log=test_root/f'{original.name}_{index}.log'
            log.write_text(completed.stdout+'\n'+completed.stderr,encoding='utf-8')
            item['commands'].append(dict(args=command,returncode=completed.returncode,
                                          seconds=round(time.monotonic()-started,2),log=str(log)))
            print(original.name+' '+str(command[:3])+' -> '+str(completed.returncode),flush=True)
            if completed.returncode:
                raise RuntimeError('Verification failed: '+str(log))
        smoke=json.loads((root/'bench_app/runtime/smoke_report.json').read_text(encoding='utf-8'))
        if smoke['demo_points']!=5 or smoke['hardware_commands_sent'] is not False:
            raise RuntimeError('Invalid smoke result')
        item['smoke']=smoke
        # Verify the pythonw executable used by the real launchers as well.
        report=root/'bench_app/runtime/smoke_report.json'
        before=report.stat().st_mtime_ns
        completed=subprocess.run([str(root/'runtime/pythonw.exe'),'-m','bench_app','--smoke-test'],
                                 cwd=root,timeout=150)
        if completed.returncode or report.stat().st_mtime_ns<=before:
            raise RuntimeError('pythonw GUI launch verification failed')
        item['pythonw_smoke']='passed'
        results.append(item)
        (test_root/'verification.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    if len(results)!=2:raise RuntimeError('Expected both packages')
    print('BOTH_RELOCATED_PACKAGES_PASSED',flush=True)


if __name__=='__main__':main()
