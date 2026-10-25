"""Worker orchestration. GUI threads never own the serial port or renew output."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import uuid

from .data import analyze_run,export_session,load_campaign,raw_samples,read_json,supplement_levels,target_window,write_json
from .model import CAMERA_CONFIG,DATA,ROOT,MAIN_LEVELS,Plan,Step,build_command,python_executable,validate_profile

HIDDEN = getattr(subprocess,'CREATE_NO_WINDOW',0)
STAGE_ATTEMPTS = 2  # One automatic retry, only when the reading window had too few samples.
# The operator explicitly removed the inter-stage residual-thrust limit: after
# the first run, any valid stopped thrust value is accepted. Electrical fields
# must still be zero and the first stage of a session remains strict.
RESUMABLE_STATUSES = ('aborted_operator_confirmed','cancelled_operator_confirmed')

class WorkspaceLock:
    def __init__(self,path=None):
        self.path=Path(path or ROOT/'bench_app/runtime/device.lock')
        self.token=uuid.uuid4().hex
        self.mutex=None
    def acquire(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        if os.name=='nt':
            import ctypes
            from ctypes import wintypes
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.CreateMutexW.argtypes=[ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR]
            kernel.CreateMutexW.restype=wintypes.HANDLE
            kernel.CloseHandle.argtypes=[wintypes.HANDLE]
            key=hashlib.sha256(str(self.path.resolve()).lower().encode()).hexdigest()[:24]
            handle=kernel.CreateMutexW(None,False,'Local\\MotorPropBench_'+key)
            error=ctypes.get_last_error()
            if not handle:raise OSError(error,'无法获取设备会话锁')
            if error==183:
                kernel.CloseHandle(handle)
                raise RuntimeError('已有实机测试或预检窗口占用设备。')
            self.mutex=(kernel,handle)
            try:write_json(self.path,dict(pid=os.getpid(),token=self.token))
            except OSError:
                kernel.CloseHandle(handle);self.mutex=None;raise
            return
        try:
            fd=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
        except FileExistsError:
            prior=read_json(self.path,{})
            pid=prior.get('pid')
            if not isinstance(pid,int):
                raise RuntimeError('设备会话锁无法识别，请先检查是否有另一测试窗口。')
            try:
                os.kill(pid,0)
            except ProcessLookupError:
                self.path.unlink()
                return self.acquire()
            except OSError:
                raise RuntimeError('另一个测试会话可能仍在运行，拒绝占用设备。')
            raise RuntimeError('已有实机测试或预检窗口占用设备。')
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(dict(pid=os.getpid(),token=self.token),stream)
    def release(self):
        try:
            if read_json(self.path,{}).get('token')==self.token:
                self.path.unlink(missing_ok=True)
        finally:
            if self.mutex:
                kernel,handle=self.mutex;kernel.CloseHandle(handle);self.mutex=None

class StageFailure(RuntimeError):
    def __init__(self,message,report=None):
        super().__init__(message);self.report=report or {}

class Controller:
    def __init__(self,events=None,process_factory=None):
        self.events=events or queue.Queue()
        self.process_factory=process_factory or subprocess.Popen
        self.cancelled=threading.Event()
        self.thread=None
        self.session=None
        self.folder=None
        self.current_run=None
        self.active=False

    def emit(self,kind,**values):
        self.events.put(dict(kind=kind,**values))

    def start(self,plan,mode='demo',port='COM42',config=CAMERA_CONFIG,specimen='',operator=None,prepare_only=False):
        if self.active:
            raise RuntimeError('请先结束当前会话。')
        if self.session and self.session['status'].endswith('waiting_operator'):
            raise ValueError('请先确认上一轮现场已经完全停转。')
        if not isinstance(plan,Plan) or mode not in ('demo','real'):
            raise ValueError('测试方案或模式无效。')
        if plan.auto_supplement and mode!='real':
            raise ValueError('自动主测与补点仅用于实机；历史回放请选择低输出验证等已有档位。')
        if plan.auto_supplement and plan.levels!=MAIN_LEVELS:
            raise ValueError('自动补点方案的主测必须为10%、20%、…、100%；自定义方案请取消自动补点。')
        if mode=='real' and not prepare_only:
            if not operator or operator.get('confirmed') is not True or operator.get('plan_signature')!=plan.signature:
                raise ValueError('本轮方案必须经过现场确认。')
        profile=validate_profile(config) if mode=='real' else None
        self.cancelled.clear();self.current_run=None
        key=datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6]
        self.folder=DATA/key
        self.folder.mkdir(parents=True,exist_ok=False)
        self.session=dict(id=key,created_at=datetime.now().astimezone().isoformat(),
            internal_smoke=getattr(self,'internal_smoke',False),
            status='preparing',mode='历史回放' if mode=='demo' else '实机预检' if prepare_only else '实机测试',
            specimen='2006-2150KV / D90S-3（历史记录）' if mode=='demo' else specimen,
            plan=plan.as_dict(),points=[],run_dirs=[],operator=operator,
            physical_confirmation=None,port=port if mode=='real' else None)
        self.session['execution_steps']=[dict(**Step(v).as_dict(),phase='main') for v in plan.levels]
        if plan.auto_supplement and not prepare_only:
            self.session['supplement']=dict(status='pending',low_g=300,high_g=500,count=8,
                max_level=max(plan.levels),levels=[],source='this_session_main_points')
        if mode=='real' and not prepare_only:
            self.session['residual_thrust_policy']=dict(
                unlimited=True,max_absolute_g=None,first_stage_strict=not plan.followup_first,
                applies_to='later stages and every retry',electrical_fields_exact=True,
                note=('The inter-stage residual-thrust limit was removed by the operator: any valid stopped thrust value is treated as stable. It does not enter the target measurement window.'
                      + (' This plan explicitly continues a previous run, so its first stage uses the same policy.' if plan.followup_first else '')))
        if profile:
            reference=Path(profile['reference_image'])
            copied=self.folder/('calibration_reference'+reference.suffix)
            shutil.copy2(reference,copied)
            profile['reference_image']=str(copied.resolve())
            write_json(self.folder/'camera_config.json',profile)
        if operator:write_json(self.folder/'operator.json',operator)
        export_session(self.session,self.folder)
        self.active=True
        self.touch_heartbeat()
        self.emit('session',session=deepcopy(self.session),folder=str(self.folder))
        self.thread=threading.Thread(target=self._work,args=(plan,mode,port,prepare_only),
                                     kwargs=dict(followup_first=plan.followup_first),daemon=True)
        self.thread.start()

    def stop(self):
        self.cancelled.set()
        self._touch_cancel()
        self.emit('phase',text='正在撤销输出并等待停机回执…')

    def touch_heartbeat(self):
        if self.active and self.session and self.session['mode']!='历史回放':
            write_json(self.folder/'ui_heartbeat.json',dict(monotonic_ns=time.monotonic_ns()))

    def _touch_cancel(self):
        run=self.current_run
        if run and run.exists():
            try:(run/'CANCEL').touch()
            except OSError as exc:self.emit('alarm',text='停止请求写入失败，请独立断电：'+str(exc))

    def _work(self,plan,mode,port,prepare_only,stage_offset=0,residual_from_start=False,
              resume_number=None,operator_path=None,followup_first=False):
        lock=WorkspaceLock();locked=False
        try:
            if mode=='real':lock.acquire();locked=True
            steps=list(plan.levels[:1] if prepare_only else plan.levels)
            resume_suffix=f'_resume{resume_number}' if resume_number else ''
            for local_index,level in enumerate(steps):
                index=stage_offset+local_index
                if self.cancelled.is_set():break
                supplement=self.session.get('supplement') or {}
                reserved=8 if supplement.get('status')=='pending' and not prepare_only else 0
                stage=self._session_steps(self.session)[index]
                self.emit('step',index=local_index,total=len(steps)+reserved,level=level,
                          stage_number=index+1,resume_number=resume_number,phase=stage.get('phase','main'))
                if stage.get('phase')=='supplement':
                    self.session['supplement']['status']='running'
                if mode=='demo':
                    point=self._demo_step(level,plan.settle_seconds)
                else:
                    point=None
                    for attempt in range(1,STAGE_ATTEMPTS+1):
                        if self.cancelled.is_set():break
                        suffix='' if attempt==1 else f'_retry{attempt-1}'
                        folder=self.folder/f'stage_{index+1:02d}_{level:g}{suffix}{resume_suffix}'
                        self.current_run=folder
                        self.session['run_dirs'].append(str(folder))
                        # The first stage keeps the strict pre-run zero baseline.
                        # Later stages, retries and resumed runs follow a motor
                        # run: the operator removed the residual-thrust limit, so
                        # any valid stopped thrust is accepted. Electrical fields
                        # must still be zero.
                        residual_unlimited = (not prepare_only
                                    and (residual_from_start or index>0 or attempt>1
                                         or (index==0 and followup_first)))
                        command=build_command(Step(level),port,self.folder/'camera_config.json',folder,
                            operator_record=(operator_path or self.folder/'operator.json') if not prepare_only else None,
                            prepare_only=prepare_only,ui_heartbeat=self.folder/'ui_heartbeat.json',
                            residual_unlimited=residual_unlimited)
                        try:
                            report=self._run_child(command,folder)
                        except StageFailure as exc:
                            # Stop and camera/process faults are terminal; never auto-retry them.
                            message=self.explain_failure(exc.report,str(exc))
                            self._record_failed_stage(index,level,folder,attempt,message,False)
                            raise StageFailure(message,exc.report)
                        if prepare_only:
                            if report.get('status')!='preparation_passed_no_motor_commands':
                                raise StageFailure(report.get('error','预检失败'),report)
                            self.session['status']='preparation_passed_no_motor_commands'
                            self.emit('phase',text='预检通过；没有发送电机测试指令')
                            break
                        if self.cancelled.is_set():break
                        point=analyze_run(folder,plan.settle_seconds)
                        if point['quality']['passed'] or attempt>=STAGE_ATTEMPTS:
                            break
                        # The motor is already stopped and output is zero. Retry
                        # only this throttle level, then continue with the plan.
                        self.session.setdefault('retry_log',[]).append(dict(
                            level=level,attempt=attempt,folder=str(folder),stage_number=index+1,
                            phase=stage.get('phase','main'),
                            issues=point['quality']['issues'],
                            warnings=point['quality'].get('warnings',[]),
                            samples=point.get('samples'),
                            metrics={key:value.get('mean') for key,value in point['metrics'].items()}))
                        export_session(self.session,self.folder)
                        self.emit('live',state=None)
                        self.emit('phase',text=(f'{level:g} 档统计样本不足，已停机；等待 {plan.cooldown_seconds:g} 秒后'
                                                f'只重测本档（第 {attempt+1}/{STAGE_ATTEMPTS} 次）'))
                        self.cancelled.wait(plan.cooldown_seconds)
                    if prepare_only and self.session['status']=='preparation_passed_no_motor_commands':
                        break
                if self.cancelled.is_set() or point is None:break
                point.update(stage_number=index+1,phase=stage.get('phase','main'))
                self.session['points'].append(point)
                self.emit('point',point=point)
                export_session(self.session,self.folder)
                if mode=='real' and not point['quality']['passed']:
                    message='该档已停机，但单独重测后统计样本仍不足，后续档位已暂停：'+'；'.join(point['quality']['issues'])
                    self._record_failed_stage(index,level,folder,STAGE_ATTEMPTS,message,True)
                    raise StageFailure(message)
                if (not prepare_only and (self.session.get('supplement') or {}).get('status')=='pending'
                        and index==len(self.session['plan']['steps'])-1):
                    # Generation is pure planning after the last main stage has
                    # stopped. Persist it even if Stop arrives here, so a later
                    # explicit resume can execute the exact saved supplement.
                    levels=self._generate_supplement()
                    steps.extend(levels)
                    self.emit('schedule',levels=list(levels),total=len(steps),
                              text='主测完成，已生成300–500g自动补点：'+
                                   '、'.join(f'{v:.3f}' for v in levels))
                # Compare against the original plan length. A resumed run enumerates
                # only the remaining levels, so len(steps)-1 would skip the cooldown
                # between the final two resumed stages.
                if index<stage_offset+len(steps)-1:
                    self.emit('phase',text=f'档间停机等待 {plan.cooldown_seconds:g} 秒')
                    self.emit('live',state=None)
                    self.cancelled.wait(.2 if mode=='demo' else plan.cooldown_seconds)
            if self.cancelled.is_set():
                if (self.session.get('supplement') or {}).get('levels'):
                    self.session['supplement']['status']='paused'
                self.session['status']='cancelled' if mode=='demo' else 'cancelled_waiting_operator'
            elif not prepare_only:
                if self.session.get('supplement'):
                    self.session['supplement']['status']='completed'
                self.session['failed_stage']=None
                self.session['status']='demo_complete' if mode=='demo' else 'software_complete_waiting_operator'
        except Exception as exc:
            if (self.session.get('supplement') or {}).get('levels'):
                self.session['supplement']['status']='paused'
            self.session.update(status='aborted' if mode=='demo' or prepare_only else 'aborted_waiting_operator',error=str(exc))
            self.emit('error',text=str(exc))
        finally:
            self.session['ended_at']=datetime.now().astimezone().isoformat()
            try:export_session(self.session,self.folder)
            except OSError as exc:
                self.session.update(status='storage_error_waiting_operator' if mode=='real' and not prepare_only else 'storage_error',error='记录保存失败：'+str(exc))
                self.emit('alarm',text=self.session['error']+'；本轮不会接续其他档位。')
            finally:
                if locked:lock.release()
                self.active=False
                self.emit('finished',session=deepcopy(self.session),folder=str(self.folder))

    @staticmethod
    def _session_steps(session):
        return (session or {}).get('execution_steps') or ((session or {}).get('plan') or {}).get('steps') or []

    def _generate_supplement(self):
        policy=self.session['supplement']
        count=len(self.session['plan']['steps'])
        primary=[point for point in self.session['points'] if point.get('phase')=='main']
        if (len(primary)!=count or any(not (p.get('quality') or {}).get('passed',False) for p in primary)
                or {p.get('stage_number') for p in primary}!=set(range(1,count+1))):
            raise StageFailure('主测未全部有效完成，未启动自动补点。')
        try:
            levels=supplement_levels(primary,policy['low_g'],policy['high_g'],policy['count'])
            if max(levels)>policy['max_level']:
                raise ValueError('补点指令超出已确认的主测范围。')
        except ValueError as exc:
            policy.update(status='planning_failed',error=str(exc))
            raise StageFailure('主测已停机并保存，但自动补点无法生成：'+str(exc)) from exc
        policy.update(status='generated',levels=list(levels),generated_at=datetime.now().astimezone().isoformat())
        self.session['execution_steps'].extend(dict(**Step(v).as_dict(),phase='supplement') for v in levels)
        write_json(self.folder/'supplement_plan.json',policy)
        export_session(self.session,self.folder)
        return levels

    def _record_failed_stage(self,index,level,folder,attempt,error,point_recorded):
        entry=dict(index=int(index),stage_number=int(index)+1,level=float(level),
                   folder=str(folder),attempt=int(attempt),point_recorded=bool(point_recorded),
                   error=str(error),recorded_at=datetime.now().astimezone().isoformat())
        self.session['failed_stage']=entry
        self.session.setdefault('failure_log',[]).append(deepcopy(entry))

    @staticmethod
    def _level_points(session):
        points={}
        for point in (session or {}).get('points') or []:
            try:level=float(point.get('level'))
            except (TypeError,ValueError):continue
            points.setdefault(level,[]).append(point)
        return points

    def _resume_start_index(self,session,steps):
        points=self._level_points(session)
        for index,step in enumerate(steps):
            level=float(step['level'])
            matches=([p for p in session.get('points',[]) if p.get('stage_number')==index+1]
                     if session.get('execution_steps') else points.get(level) or [])
            if not any((point.get('quality') or {}).get('passed',True) for point in matches):
                return index
        return None

    def resume_remaining_levels(self,session=None):
        """Return (original index, remaining levels) from the first unfinished stage."""
        session=self.session if session is None else session
        steps=self._session_steps(session)
        if not steps:return None
        index=self._resume_start_index(session,steps)
        if index is None:return None
        return index,tuple(float(step['level']) for step in steps[index:])

    def can_resume(self,session=None):
        session=self.session if session is None else session
        return bool(session and session.get('mode')=='实机测试'
                    and session.get('status') in RESUMABLE_STATUSES
                    and (session.get('physical_confirmation') or {}).get('stopped') is True
                    and self.resume_remaining_levels(session))

    def latest_resumable(self):
        """Return the newest session only when it is a stopped, unfinished real run.

        Looking only at the newest session avoids offering an old failure after
        the operator has already completed a newer run or precheck.
        """
        candidates=[]
        for path in DATA.glob('*/session.json'):
            session=read_json(path)
            if not session or session.get('mode')!='实机测试':continue
            try:candidates.append((path.stat().st_mtime,path,session))
            except OSError:continue
        if not candidates:return None
        mtime,path,session=max(candidates,key=lambda item:item[0])
        if not self.can_resume(session):return None
        folder=path.parent
        if not (folder/'camera_config.json').is_file():return None
        return mtime,folder,session

    def load_latest_resumable(self):
        """Load the newest confirmed-stop session for an explicit operator resume."""
        if self.active:raise RuntimeError('请先结束当前会话。')
        latest=self.latest_resumable()
        if not latest:raise ValueError('没有可续测的已停机实机会话。')
        _,folder,session=latest
        self.session=session;self.folder=folder;self.current_run=None
        return session

    def resume(self,plan,operator=None,port=None):
        """Run the unfinished part of a confirmed-stop session in the same folder.

        The original points and raw stage folders are preserved. The operator
        must confirm the reduced plan again; the first resumed stage is treated
        as a follow-up run, so it receives the small residual-thrust allowance.
        """
        if self.active:raise RuntimeError('请先结束当前会话。')
        if not self.session or not self.folder:raise ValueError('没有可续测的会话。')
        if self.session.get('status') not in RESUMABLE_STATUSES:
            raise ValueError('只有已确认现场停转的中止会话可以续测。')
        if (self.session.get('physical_confirmation') or {}).get('stopped') is not True:
            raise ValueError('现场停转确认缺失，不能续测。')
        if not isinstance(plan,Plan):raise ValueError('续测方案无效。')
        original=self.session.get('plan') or {}
        steps=self._session_steps(self.session)
        remaining_info=self.resume_remaining_levels(self.session)
        if not remaining_info:raise ValueError('原方案没有未完成的档位，不能续测。')
        start_index,remaining=remaining_info
        if plan.levels!=remaining:
            raise ValueError('续测方案必须从失败档开始并包含原方案剩余的全部档位。')
        if (plan.cooldown_seconds!=original.get('cooldown_seconds')
                or plan.settle_seconds!=original.get('settle_seconds')
                or plan.auto_supplement!=original.get('auto_supplement',False)):
            raise ValueError('续测方案的档间停机与统计设置必须与原方案一致。')
        if (not operator or operator.get('confirmed') is not True
                or operator.get('plan_signature')!=plan.signature):
            raise ValueError('续测方案必须经过现场确认。')
        if not (self.folder/'camera_config.json').is_file():
            raise ValueError('原会话缺少相机标定快照，不能续测。')
        port=(port or self.session.get('port') or 'COM42').strip()
        number=len(self.session.get('resume_log') or [])+1
        operator_path=self.folder/f'operator_resume{number}.json'
        write_json(operator_path,operator)
        failed_level=float(steps[start_index]['level'])
        superseded=[]
        for point in list(self.session.get('points') or []):
            try:
                same=(point.get('stage_number')==start_index+1 if self.session.get('execution_steps')
                      else abs(float(point.get('level'))-failed_level)<1e-9)
            except (TypeError,ValueError):same=False
            if same and not (point.get('quality') or {}).get('passed',True):
                superseded.append(deepcopy(point))
                self.session['points'].remove(point)
        previous_failed=deepcopy(self.session.get('failed_stage'))
        entry=dict(number=number,started_at=datetime.now().astimezone().isoformat(),
                   start_index=start_index,stage_number=start_index+1,level=failed_level,
                   remaining_levels=list(remaining),operator_file=operator_path.name,
                   operator_signature=plan.signature,previous_error=self.session.get('error'),
                   failed_stage=previous_failed,
                   reconstructed_failed_stage=not bool(previous_failed),
                   superseded_points=superseded)
        self.session.setdefault('resume_log',[]).append(entry)
        self.session['status']='resuming'
        self.session['error']=None
        self.session['failed_stage']=None
        self.session['port']=port
        self.cancelled.clear();self.current_run=None
        export_session(self.session,self.folder)
        self.active=True
        self.touch_heartbeat()
        self.emit('session',session=deepcopy(self.session),folder=str(self.folder))
        self.emit('resume',number=number,start_index=start_index,level=failed_level,
                  remaining_levels=list(remaining))
        self.thread=threading.Thread(target=self._work,args=(plan,'real',port,False),
            kwargs=dict(stage_offset=start_index,residual_from_start=True,
                        resume_number=number,operator_path=operator_path),daemon=True)
        self.thread.start()

    def _run_child(self,command,folder):
        self.emit('phase',text='连接设备，检查零点与输出状态…')
        process=self.process_factory(command,cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
            text=True,encoding='utf-8',errors='replace',creationflags=HIDDEN)
        lines=queue.Queue()
        transcript=[]
        def read_output():
            for line in process.stdout:
                transcript.append(line);lines.put(line.rstrip())
        reader=threading.Thread(target=read_output,daemon=True);reader.start()
        last_marker=None;started=time.monotonic();warned=False
        while True:
            if self.cancelled.is_set():self._touch_cancel()
            if time.monotonic()-started>130:
                self.cancelled.set();self._touch_cancel()
                if not warned:
                    self.emit('alarm',text='控制进程超时，已请求停止；请立即检查现场并独立断电。');warned=True
            while not lines.empty():
                line=lines.get();self.emit('log',text=line)
                if line.startswith('STEP_START:'):
                    self.emit('phase',text='缓升与目标档采集中')
                elif line.startswith('STEP_ENDED:') or line.startswith('HOLD_ENDED:'):
                    self.emit('phase',text='输出已回零，正在收尾并保存')
            report=read_json(folder/'result.json',{})
            state=read_json(folder/'live_cv.json')
            if state and state.get('run_id')==report.get('run_id'):
                marker=(state.get('frame_seq'),state.get('state'))
                if marker!=last_marker:
                    self.emit('live',state=state,preview=str(folder/'live_preview.jpg'))
                    last_marker=marker
            code=process.poll()
            if code is not None:break
            time.sleep(.10)
        reader.join(timeout=1)
        folder.mkdir(parents=True,exist_ok=True)
        (folder/'process.log').write_text(''.join(transcript),encoding='utf-8')
        report=read_json(folder/'result.json',{})
        if code!=0:
            raise StageFailure(report.get('error','控制进程未完成，请查看原始记录。'),report)
        return report

    @staticmethod
    def explain_failure(report,fallback):
        """Turn a stopped-baseline timeout into an actionable Chinese message."""
        diagnosis=report.get('baseline_diagnosis') or {}
        readable=diagnosis.get('readable_fraction') or {}
        labels={'upper':'拉力','voltage':'电压','current':'电流','power':'功率','energy':'电量'}
        detail='、'.join(f'{labels.get(k,k)} {v:.0%}' for k,v in readable.items() if k in labels)
        if not detail:
            return fallback
        text='零载基线未通过：未形成稳定读数，本次没有发送电机指令。窗口读数有效率：'+detail+'。'
        if readable.get('upper',1)<.70:
            text+='拉力读数频繁缺笔画，多为LED刷新与相机曝光不同步；请检查仪表亮度/刷新、相机曝光或对焦后重试。'
        if diagnosis.get('samples'):
            text+=f"已识别{diagnosis['samples']}帧，原始画面和逐帧记录已保存在本轮目录。"
        return text

    def _demo_step(self,level,settle_seconds):
        matches=[p for p in load_campaign() if p['level']==level]
        if not matches:raise ValueError('历史数据中没有这个档位。')
        folder=Path(matches[-1]['run_dir'])
        report=read_json(folder/'result.json');start,duration=target_window(report)
        rows=[s for s in raw_samples(folder) if start<=s.get('captured_monotonic_ns',0)/1e9<start+duration]
        self.emit('phase',text='历史回放中 · 不连接飞控或摄像头')
        previews=list(folder.glob('*review_frame.png'))
        for state in rows:
            if self.cancelled.is_set():break
            state=deepcopy(state)
            state['demo']=True
            state['demo_elapsed_seconds']=state['captured_monotonic_ns']/1e9-start
            self.emit('live',state=state,preview=str(previews[0]) if previews else None)
            self.cancelled.wait(.025)
        return analyze_run(folder,settle_seconds,'historical_replay')

    def confirm_physical_stop(self):
        if self.active or not self.session or not self.session['status'].endswith('waiting_operator'):
            raise ValueError('没有等待现场确认的实机会话。')
        self.session['physical_confirmation']=dict(stopped=True,clamps_secure=True,no_abnormality=True,
            confirmed_at=datetime.now().astimezone().isoformat())
        self.session['status']=self.session['status'].replace('_waiting_operator','_operator_confirmed')
        for point in self.session['points']:point['physical_confirmation_pending']=False
        export_session(self.session,self.folder)
        self.emit('finished',session=deepcopy(self.session),folder=str(self.folder))

def scan_ports():
    result=subprocess.run([python_executable(),'-X','utf8',str(ROOT/'px4_mvp/px4_control_mvp.py'),'ports'],
        cwd=ROOT,capture_output=True,text=True,encoding='utf-8',errors='replace',creationflags=HIDDEN,timeout=15)
    if result.returncode:raise RuntimeError(result.stderr.strip() or '串口扫描失败')
    return json.loads(result.stdout)
