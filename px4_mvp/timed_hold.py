"""Bounded guarded bench trials; each new increment is a separate invocation."""
import argparse
from datetime import datetime,timedelta,timezone
import json
import math
import shutil
from pathlib import Path
import subprocess
import sys
import time
import traceback
import uuid

from hold_core import CVGuard,LoadResponseGuard,stopped_baseline_for_run,finite_hold,finite_rise,finite_profile,rise_profile,finite_sweep,read_snapshot,wait_stopped_baseline,check_ui_heartbeat,high_range_profile,extreme_range_profile
from px4_control_mvp import BenchLink,mavutil,validate_pulse
from runtime_paths import project_python

ROOT = Path(__file__).resolve().parent.parent


def save(path,data):
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')


def derived_power(state):
    """High-range power display changes format above 99.9 W; fall back to V*A.

    The raw display value is retained for traceability. A reading is only
    replaced when it is missing or inconsistent with voltage times current.
    """
    fields={f['id']:f for f in state.get('fields',[])}
    def number(key):
        field=fields.get(key,{})
        if not field.get('valid') or field.get('value') is None:
            return None
        try:
            return float(field['value'])
        except (TypeError,ValueError):
            return None
    volts,amps=number('voltage'),number('current')
    if volts is None or amps is None:
        return state
    expected=volts*amps
    shown=number('power')
    if shown is not None and abs(shown-expected)<=max(.2,abs(expected)*.25):
        return state
    power=dict(fields.get('power',{}))
    power.update(value=f'{expected:.2f}',valid=True,reason=None,
                 derived_from='voltage_x_current',
                 display_value=fields.get('power',{}).get('value'),
                 display_valid=bool(fields.get('power',{}).get('valid')))
    return {**state,'fields':[power if f['id']=='power' else f for f in state['fields']]}


class TimedBench(BenchLink):
    def __init__(self,folder,max_value=.10,port='COM42'):
        self.events=(folder/'control_events.jsonl').open('w',encoding='utf-8')
        self.outputs=[]
        self.unexpected_output=False
        self.stop_acknowledged=False
        self.max_value=max_value
        super().__init__(port,115200,folder/'control.csv')

    def record(self,event,detail):
        self.events.write(json.dumps({'utc':datetime.now(timezone.utc).isoformat(),
            'monotonic_ns':time.monotonic_ns(),'event':event,'detail':detail},ensure_ascii=False)+'\n')
        self.events.flush()
        super().record(event,detail)

    def receive(self):
        msg=super().receive()
        if msg is not None and msg.get_type()=='SERVO_OUTPUT_RAW' and msg.port==0:
            values=[getattr(msg,f'servo{i}_raw',0) for i in range(1,17)]
            self.outputs.append({'t':time.monotonic(),'values':values})
            self.unexpected_output |= any(values[1:])
        return msg

    def check(self):
        self.ensure_disarmed()
        if self.unexpected_output:
            raise RuntimeError('Unexpected output on a channel other than Motor 1.')
        if not self.outputs or time.monotonic()-self.outputs[-1]['t']>1:
            raise RuntimeError('Output telemetry is missing or stale.')

    def command(self,value,lease,ack_timeout):
        validate_pulse(value,lease,self.max_value)
        self.stop_acknowledged=False
        self.send_command(mavutil.mavlink.MAV_CMD_ACTUATOR_TEST,[value,lease,0,0,1,0,0])
        self.wait_ack(mavutil.mavlink.MAV_CMD_ACTUATOR_TEST,timeout=ack_timeout)

    def stop(self):
        self.stop_acknowledged=False
        try:
            super().stop(1)
            self.stop_acknowledged=True
        except BaseException as exc:
            self.record('stop_unconfirmed',str(exc))
            print('STOP UNCONFIRMED: use independent ESC power cutoff.',file=sys.stderr,flush=True)
            raise

    def watch(self,seconds):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            self.receive()
            self.ensure_disarmed()

    def verify_zero(self):
        self.check()
        if len(self.outputs)<3 or any(any(s['values']) for s in self.outputs[-3:]):
            raise RuntimeError('Latest three output reports are not all zero.')

    def close(self):
        try:
            super().close()
        finally:
            self.events.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',default='COM42')
    parser.add_argument('--cv-config',type=Path,default=ROOT/'cv_mvp/instrument_config.json')
    parser.add_argument('--result-dir',type=Path)
    parser.add_argument('--operator-record',type=Path)
    parser.add_argument('--ui-heartbeat',type=Path)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--sweep-low',action='store_true',help='One 0.01/0.015/0.02 sweep, ten seconds per level, five seconds stopped between levels.')
    parser.add_argument('--next-step',action='store_true',help='Single selected low-output level; no fixed current/power/thrust abort thresholds.')
    parser.add_argument('--step-value',type=float,default=.025,help='Target 0.010..1.000 with at most three decimals; no fixed level list.')
    parser.add_argument('--step-seconds',type=float,choices=(2,10,15,20),default=2,help='Two-second probe or bounded 10/15/20-second measurement.')
    parser.add_argument('--gentle-rise',action='store_true',help='Bounded gentle rise to the selected target, total at most 20 seconds.')
    parser.add_argument('--high-range',action='store_true',
        help='High-range ladder to a 0.061..0.60 target; no fixed current/power/thrust abort thresholds. Requires --next-step --gentle-rise and 2, 10 or 15 seconds.')
    parser.add_argument('--extreme-range',action='store_true',
        help='Extreme-range targets 0.601..1.00; no fixed current/power/thrust abort thresholds. Requires --next-step --gentle-rise and 10 seconds.')
    parser.add_argument('--expanded-trial',action='store_true',help='Expanded low-range policy; 0.026..0.060 targets use a bounded gentle rise and no fixed abort thresholds.')
    parser.add_argument('--resume-final',action='store_true',help='Explicitly measure only the unmeasured final 0.02 level; requires --sweep-low.')
    parser.add_argument('--residual-thrust-limit',type=float,default=None,
                        help='Stopped upper baseline may keep up to this many grams before later stages; electrical fields still require zero. Default: strict near-zero limit.')
    parser.add_argument('--residual-thrust-unlimited',action='store_true',
                        help='Between stages, accept any valid stopped residual thrust; electrical fields still require zero. Mutually exclusive with --residual-thrust-limit.')
    parser.add_argument('--prepare-only',action='store_true',help='Check camera/configuration/zero output; send no motor commands.')
    parser.add_argument('--monitor-seconds',type=float,default=0,help='Preparation-only continuous CV/telemetry guard check, up to 30 seconds.')
    parser.add_argument('--guarded-prop-bench',action='store_true',
        help='Operator confirmed fixed bench, guarding/isolation, independent cutoff and onsite supervision.')
    args=parser.parse_args()
    if not (math.isfinite(args.step_value) and .010<=args.step_value<=1.000
            and round(args.step_value,3)==args.step_value):
        parser.error('--step-value must be 0.010..1.000 with at most three decimals.')
    expanded=args.expanded_trial or args.high_range or args.extreme_range
    # The thrust display has shown a stable 2..4 g zero offset on this bench. The
    # operator clarified that this stuck zero is not a real load. Allow 5 g only
    # for the extreme range, where it is 0.2% of the 2500 g backstop; electrical
    # baselines stay at zero. The measured offset is recorded in every report.
    near_zero_thrust_limit=5 if args.extreme_range else 2
    residual_limit=args.residual_thrust_limit
    residual_unlimited=args.residual_thrust_unlimited
    if residual_limit is not None and residual_unlimited:
        parser.error('Choose only one residual policy: --residual-thrust-limit or --residual-thrust-unlimited.')
    if args.prepare_only and (residual_limit is not None or residual_unlimited):
        parser.error('Residual-thrust policy is not allowed for preparation-only checks.')
    if residual_limit is not None and (not math.isfinite(residual_limit)
            or not near_zero_thrust_limit<=residual_limit<=6):
        parser.error('--residual-thrust-limit must be between the near-zero agreement limit and 6 g.')
    stopped_allow_upper=expanded or residual_unlimited or residual_limit is not None
    max_upper_g=(math.inf if residual_unlimited else
                 near_zero_thrust_limit if residual_limit is None else residual_limit)
    if sum((args.high_range,args.extreme_range,args.expanded_trial))>1:
        parser.error('Choose only one range policy: --high-range, --extreme-range or --expanded-trial.')
    gentle_target_ok=(args.high_range and .06<args.step_value<=.60 and args.step_seconds in (2,10,15)
                      or args.extreme_range and .60<args.step_value<=1.00 and args.step_seconds==10
                      or not (args.high_range or args.extreme_range) and .025<args.step_value<=.07)
    if args.gentle_rise and not (args.next_step and gentle_target_ok):
        parser.error('--gentle-rise requires --next-step and a target in 0.026..0.070 (or a --high-range 0.061..0.60 target for 2, 10 or 15 seconds, or a --extreme-range 0.601..1.00 target for 10 seconds).')
    if args.high_range and not (args.next_step and args.gentle_rise and args.step_seconds in (2,10,15)):
        parser.error('--high-range requires a bounded --next-step --gentle-rise for 2, 10 or 15 seconds.')
    if args.extreme_range and not (args.next_step and args.gentle_rise and args.step_seconds==10):
        parser.error('--extreme-range requires a bounded --next-step --gentle-rise for 10 seconds.')
    if args.expanded_trial and not (args.next_step and args.gentle_rise):
        parser.error('--expanded-trial requires a bounded --next-step --gentle-rise.')
    if args.step_value>.05 and not expanded:
        parser.error('Steps above 0.050 require explicit --expanded-trial or --high-range.')
    if args.step_value>.04 and not args.gentle_rise:
        parser.error('Steps above 0.040 require the bounded gentle-rise profile.')
    if args.next_step and (args.sweep_low or args.resume_final):
        parser.error('--next-step is a separate single-level trial.')
    if args.resume_final and not args.sweep_low:
        parser.error('--resume-final requires --sweep-low.')
    if not 0<=args.monitor_seconds<=30 or args.monitor_seconds and not args.prepare_only:
        parser.error('--monitor-seconds must be 0..30 and requires --prepare-only.')
    if not args.prepare_only and not (args.execute and args.guarded_prop_bench):
        parser.error('Explicit --execute and --guarded-prop-bench required.')
    run_id=uuid.uuid4().hex
    name=datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d_%H%M%S')+(f'_step{args.step_value:g}_{args.step_seconds:g}s_' if args.next_step else '_sweep_low_' if args.sweep_low else '_hold10_')+run_id[:6]
    folder=args.result_dir.resolve() if args.result_dir else ROOT/'integration_runs'/name
    folder.mkdir(parents=True,exist_ok=False)
    print('RUN_FOLDER: '+str(folder),flush=True)
    report={'run_id':run_id,'status':'preparing','value':.02,'motor':1,
            'port':args.port,'cv_config':str(args.cv_config.resolve()),
            'requested_seconds':10,'max_command_lease_seconds':1,
            'renew_interval_seconds':.35,'max_ack_wait_seconds':.3,
            'max_cv_age_seconds':1,
            'unreadable_field_timeout_seconds':{'upper':2.5,'voltage':1.5,'current':1.5,'power':1.5},
            'unreadable_timeout_note':'The LED thrust display loses segments on some frames; every frame is now scanned so the published sample can hold a complete reading up to 0.25 s old. Electrical LCD fields keep 1.5 s.',
            'operator_conditions':'Same guarded prop bench confirmed in this conversation.',
            'stopped_baseline_wait_seconds':10,
            'physical_observation':'pending operator feedback',
            'scope':'One 0.02 / 10-second run. No higher output or fault injection.'}
    if args.operator_record:
        operator=json.loads(args.operator_record.read_text(encoding='utf-8'))
        if operator.get('confirmed') is not True:
            raise ValueError('Current operator confirmation is required.')
        report['operator_record']=operator
    if args.prepare_only:
        report['scope']='Preparation only: no motor commands.'
    report['baseline_confirmation']={'method':'agreeing_valid_readings_window',
        'min_readings':4,'min_span_seconds':.6,'max_gap_seconds':1.8,'window_seconds':6,'recent_seconds':.6,
        'dropout_note':'Brief LED refresh dropouts are not evidence of a value change.'}
    if residual_unlimited:
        report['baseline_thrust_policy']={'method':'any_valid_stopped_thrust',
            'absolute_limit_g':None,'agreement_tolerance_g':None,'unlimited':True,
            'stable_value_not_fabricated':True,'setup_baseline_uses_residual':True,
            'note':'This invocation is a later stage, retry, resume or explicitly declared follow-up: its setup baseline accepts any valid stopped residual thrust. Electrical fields still require zero.'}
        report['residual_thrust_policy']={'enabled':True,'unlimited':True,
            'max_absolute_g':None,'agreement_tolerance_g':None,
            'setup_baseline_uses_residual':True,
            'first_baseline_strict':False,'first_session_stage_strict':None,
            'electrical_fields_exact':True,
            'note':'The operator explicitly removed the inter-stage residual-thrust limit: any valid stopped thrust value is treated as stable. Both the setup baseline and the stopped baseline before the step use this policy. The parent session records whether its first stage was strict; this policy also covers an explicitly declared follow-up first stage. The residual does not enter the measured target window.'}
    elif residual_limit is not None:
        report['baseline_thrust_policy']={'method':'valid_raw_within_residual_tolerance',
            'absolute_limit_g':max_upper_g,'agreement_tolerance_g':max_upper_g,
            'stable_value_not_fabricated':True,'setup_baseline_uses_residual':True,
            'note':'This invocation is a later stage or retry: its setup baseline allows the same small stopped residual thrust as the baseline before the step. The first stage of the session remains strict.'}
        report['residual_thrust_policy']={'enabled':True,'max_absolute_g':max_upper_g,
            'agreement_tolerance_g':max_upper_g,'setup_baseline_uses_residual':True,
            'first_baseline_strict':False,'first_session_stage_strict':True,
            'electrical_fields_exact':True,
            'note':'This invocation is a later stage or retry; both its setup baseline and the stopped baseline before the step allow a small residual thrust. The first stage of the session remains strict. The residual does not enter the measured target window.'}
    else:
        report['baseline_thrust_policy']={'method':'valid_raw_within_original_tolerance',
            'absolute_limit_g':near_zero_thrust_limit,'agreement_tolerance_g':near_zero_thrust_limit,
            'stable_value_not_fabricated':True,'setup_baseline_uses_residual':False,
            'note':('Operator clarified a stuck 3-4 g display zero is not a real load; extreme range allows 5 g.'
                    if expanded else 'Strict first baseline: thrust must stay within the near-zero agreement limit.')}
    if args.resume_final:
        report.update(levels=[.02],resume_final_only=True,
                      scope='Complete only final 0.02 level; prior two levels are not repeated.')
    if args.sweep_low or args.next_step:
        report.update(scope='One mechanically fixed low-output sweep, not full-range performance.',
            levels=[.01,.015,.02],seconds_per_level=10,stopped_gap_seconds=5,sweep_steps=[],
            process_policy='no_fixed_thresholds',
            process_limits_enforced=False,
            limits_note='No fixed current/power/thrust abort thresholds as of 2026-10-18; hardware protection relies on the ESC, power supply and the operator independent cutoff.',
            operator_conditions='User now reports the base is securely fixed by clamps; no hand restraint.')
    if args.next_step:
        report.update(value=args.step_value,levels=[args.step_value],requested_seconds=args.step_seconds,
                      seconds_per_level=args.step_seconds,
                      scope='Single selected increment only; no fixed abort thresholds, no further automatic increase.')
    if args.gentle_rise:
        profile=(extreme_range_profile(args.step_value,args.step_seconds) if args.extreme_range else
                 high_range_profile(args.step_value,args.step_seconds) if args.high_range else
                 rise_profile(args.step_value,args.step_seconds))
        report.update(requested_seconds=sum(s for _,s in profile),
            levels=[v for v,_ in profile],target_seconds=args.step_seconds,
            stopped_gap_seconds=0,continuous_profile=True,
            rise_profile=[{'value':v,'seconds':s} for v,s in profile],
            scope='One bounded gentle rise with the explicitly recorded trial policy; stop on any violation. The desktop app may retry only a stage whose statistics were insufficient.')
    if args.prepare_only:
        report['scope']='Preparation only: no motor commands.'
    save(folder/'result.json',report)
    bench=None
    camera=None
    cv_stdout=(folder/'cv_stdout.txt').open('w',encoding='utf-8')
    cv_stderr=(folder/'cv_stderr.txt').open('w',encoding='utf-8')
    observed=(folder/'observed_cv.jsonl').open('w',encoding='utf-8')
    stop_file=folder/'STOP_CV'
    snapshot_path=folder/'live_cv.json'
    current={}
    last_observed=-1
    requested_frame=False
    hold_started=None
    phase='baseline'
    current_step=None
    load_guard=None
    program_deadline=time.monotonic()+(100 if args.extreme_range else 80)
    try:
        camera=subprocess.Popen([project_python(ROOT),
            str(ROOT/'cv_mvp/instrument_live.py'),'--headless','--seconds','90',
            '--run-id',run_id,'--stop-file',str(stop_file),
            '--config',str(args.cv_config.resolve()),'--snapshot-path',str(snapshot_path),
            '--preview-path',str(folder/'live_preview.jpg')],
            cwd=ROOT,stdout=cv_stdout,stderr=cv_stderr,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        guard=CVGuard(run_id,time.monotonic)

        def health(require_stable=False):
            nonlocal current,last_observed,requested_frame
            if (folder/'CANCEL').exists():
                raise RuntimeError('Operator cancellation requested.')
            if time.monotonic()>program_deadline:
                raise RuntimeError('Total integration session deadline exceeded.')
            if args.ui_heartbeat:
                age=check_ui_heartbeat(args.ui_heartbeat)
                report['max_ui_heartbeat_age_seconds']=max(report.get('max_ui_heartbeat_age_seconds',0),age)
            if camera.poll() is not None:
                raise RuntimeError('Camera process exited.')
            current=read_snapshot(snapshot_path)
            if args.high_range or args.extreme_range:
                current=derived_power(current)
            # While stopped, a long LED dropout is display refresh, not a lost
            # load reading. Electrical LCD fields stay strict while running; the
            # LED thrust field gets a little more slack because its refreshed
            # segments are intermittently unreadable by design.
            guard.check(current,require_stable=require_stable,
                        allow_near_zero_thrust=expanded or residual_unlimited or residual_limit is not None,
                        near_zero_thrust_limit=near_zero_thrust_limit,max_upper_g=max_upper_g,
                        unreadable_seconds=3 if phase=='baseline' else 1.5,
                        unreadable_seconds_by_field=None if phase=='baseline' else {'upper':2.5})
            report['max_observed_cv_age_seconds']=max(report.get('max_observed_cv_age_seconds',0),
                time.monotonic()-current['captured_monotonic_ns']/1e9)
            if current['samples']!=last_observed:
                observed.write(json.dumps({**current,'sweep_phase':phase,'sweep_step':current_step},ensure_ascii=False)+'\n')
                observed.flush()
                last_observed=current['samples']
            if phase=='running' and load_guard is not None:
                report['load_response_valid_samples']=load_guard.check(current,time.monotonic())
            review_delay=4+sum(s['seconds'] for s in report.get('rise_profile',[])[:-1])
            if hold_started is not None and time.monotonic()-hold_started>=review_delay and not requested_frame:
                (ROOT/'cv_mvp/instrument_logs/request_frame').touch()
                requested_frame=True
            return current

        setup_deadline=time.monotonic()+45
        bench=TimedBench(folder,max_value=1.0 if args.extreme_range else .60 if args.high_range else .10,port=args.port)
        bench.connect()
        report['preflight']=bench.probe(parameters=True)
        params=report['preflight']['output_parameters']
        if report['preflight'].get('firmware_version')!='1.14.3' or any(
            params.get(key)!=value for key,value in
            {'COM_MOT_TEST_EN':1,'PWM_MAIN_FUNC1':101,'PWM_MAIN_TIM0':-4}.items()):
            raise RuntimeError('Firmware or verified Motor 1 configuration changed.')
        bench.ensure_disarmed()
        bench.enable_output_observation()
        bench.watch(.7)
        bench.verify_zero()
        baseline=stopped_baseline_for_run(residual_limit=residual_limit,
                                          residual_unlimited=residual_unlimited,
                                          near_zero_thrust_limit=near_zero_thrust_limit)
        last_problem=None
        while time.monotonic()<setup_deadline:
            bench.receive()
            bench.check()
            if (folder/'CANCEL').exists():
                raise RuntimeError('Operator cancellation requested.')
            try:
                health()
            except (RuntimeError,OSError,ValueError,KeyError) as exc:
                baseline.reset()
                last_problem=str(exc)
            else:
                if baseline.update(current,time.monotonic()):
                    break
        else:
            readable=current.get('readable_fraction',{})
            report['baseline_diagnosis']={
                'problem':last_problem,'readable_fraction':readable,
                'samples':current.get('samples'),
                'note':'Invalid frames are brief LED refresh dropouts, not a changed value.'}
            detail='; '.join(f'{key} readable {value:.0%}' for key,value in readable.items())
            message='Fresh stable baseline not ready: '+str(last_problem or 'no sustained agreement')
            if detail:
                message+=' ('+detail+')'
            raise RuntimeError(message)
        report['cv_log_folder']=current['log_folder']
        report['baseline']=current
        save(folder/'before_hold.json',current)
        if args.prepare_only:
            (ROOT/'cv_mvp/instrument_logs/request_frame').touch()
            monitor_deadline=time.monotonic()+args.monitor_seconds
            while time.monotonic()<monitor_deadline:
                bench.receive()
                bench.verify_zero()
                health()
            report['continuous_monitor_seconds']=args.monitor_seconds
            report['status']='preparation_passed_no_motor_commands'
            print('PREPARATION_PASSED: no motor commands sent.',flush=True)
            return 0
        report['status']='running'
        save(folder/'result.json',report)
        if args.sweep_low or args.next_step:
            def before_step(index,value):
                nonlocal phase,current_step,hold_started,requested_frame,load_guard
                phase='baseline'; current_step=index
                bench.verify_zero()
                wait_stopped_baseline(bench,health,time.monotonic,
                    allow_near_zero_thrust=stopped_allow_upper,timeout_seconds=10,
                    near_zero_thrust_limit=near_zero_thrust_limit,max_upper_g=max_upper_g)
                fields={f['id']:f for f in current['fields']}
                upper_baseline=fields['upper'].get('baseline_value') or fields['upper']['stable_value']
                current_baseline=fields['current'].get('baseline_value') or fields['current']['stable_value']
                report['baseline_upper_g']=float(upper_baseline)
                if residual_unlimited or residual_limit is not None:
                    report['residual_baseline_upper_g']=float(upper_baseline)
                report.setdefault('stopped_baselines',[]).append({
                    'step':index+1,'upper_g':float(upper_baseline),
                    'current_a':float(current_baseline),
                    'absolute_limit_g':None if residual_unlimited else max_upper_g,
                    'unlimited':residual_unlimited,
                    'residual_policy':residual_unlimited or residual_limit is not None})
                if ((not residual_unlimited and abs(float(upper_baseline))>max_upper_g)
                        or abs(float(current_baseline))>.02):
                    raise RuntimeError('Stopped baseline is not near zero; check clamp/tare/power before testing.')
                save(folder/f'step_{index+1}_baseline.json',current)
                phase='running'; hold_started=time.monotonic(); requested_frame=False
                load_guard=LoadResponseGuard(hold_started,current)
                print(f"STEP_START: value={value} total={report['requested_seconds'] if args.gentle_rise else args.step_seconds if args.next_step else 10} seconds",flush=True)
            def after_step(step):
                nonlocal phase,hold_started
                phase='stopping'; hold_started=None
                bench.watch(.8)
                bench.verify_zero()
                step['reported_output_returned_zero']=True
                control=step['control']
                step['raw_output_values']=sorted({s['values'][0] for s in bench.outputs
                    if control['started_monotonic_s']<=s['t']<=control['deadline_monotonic_s'] and s['values'][0]})
                if not step['raw_output_values']:
                    raise RuntimeError('No active target output observed for step.')
                frame_state_path=Path(report['cv_log_folder'])/'review_frame.json'
                if frame_state_path.exists():
                    frame_state=read_snapshot(frame_state_path)
                    frame_time=frame_state.get('captured_monotonic_ns',0)/1e9
                    if frame_state.get('run_id')==run_id and control['started_monotonic_s']<=frame_time<=control['deadline_monotonic_s']:
                        for filename in ('review_frame.png','review_frame.json'):
                            source=Path(report['cv_log_folder'])/filename
                            if source.exists():
                                shutil.copy2(source,folder/f"step_{step['index']+1}_{filename}")
                report['sweep_steps'].append(step)
                save(folder/'result.json',report)
                print(f"STEP_ENDED: value={step['value']} output reports zero",flush=True)
            def rest(seconds):
                nonlocal phase
                phase='rest'
                deadline=time.monotonic()+seconds
                while time.monotonic()<deadline:
                    bench.receive(); bench.verify_zero(); health()
            if args.next_step:
                index=int(round(args.step_value*1000))
                before_step(index,args.step_value)
                control=(finite_profile(bench,health,time.monotonic,profile) if args.high_range or args.extreme_range else
                         finite_rise(bench,health,time.monotonic,target_seconds=args.step_seconds,target=args.step_value) if args.gentle_rise else
                         finite_hold(bench,health,time.monotonic,seconds=args.step_seconds,value=args.step_value))
                after_step({'index':index,'value':args.step_value,'control':control})
            else:
                finite_sweep(bench,health,time.monotonic,before_step,after_step,rest,
                             start_index=2 if args.resume_final else 0)
        else:
            print('HOLD_START: Motor 1, value=0.02, total=10 seconds',flush=True)
            hold_started=time.monotonic()
            report['control']=finite_hold(bench,health,time.monotonic)
        bench.watch(.8)
        bench.verify_zero()
        report['reported_output_returned_zero']=True
        report['active_raw_outputs']=sorted({s['values'][0] for s in bench.outputs if s['values'][0]})
        if not report['active_raw_outputs']:
            raise RuntimeError('No active Motor 1 output report was observed.')
        report['other_channels_zero']=not bench.unexpected_output
        print('HOLD_ENDED: stop acknowledged; output reports zero. Confirm physical stop.',flush=True)
        tail_deadline=time.monotonic()+4
        while time.monotonic()<tail_deadline:
            bench.receive()
            try:
                health()
            except (RuntimeError,OSError,ValueError) as exc:
                report['post_stop_cv_note']=str(exc)
                break
        report['after_hold']=current
        report['status']='software_checks_passed_physical_confirmation_pending'
        return_code=0
    except (Exception,KeyboardInterrupt) as exc:
        report['status']='aborted'
        report['error']=repr(exc)
        report['error_details']={'message':str(exc),'filename':getattr(exc,'filename',None),
                                 'traceback':traceback.format_exc()}
        report['abort_cv_snapshot']=current
        if bench is not None and bench.stop_acknowledged:
            try:
                bench.watch(.6)
                bench.verify_zero()
                report['reported_output_returned_zero']=True
            except Exception as stop_error:
                report['post_abort_output_check_error']=repr(stop_error)
        print('TRIAL_ABORTED: '+repr(exc),file=sys.stderr,flush=True)
        return_code=1
    finally:
        if bench is not None:
            report['stop_commands_acknowledged']=bench.stop_acknowledged
            try:
                bench.close()
            except Exception as exc:
                report['link_close_error']=repr(exc)
        if camera is not None and camera.poll() is None:
            stop_file.touch()
            try:
                camera.wait(timeout=8)
            except subprocess.TimeoutExpired:
                camera.terminate()
                camera.wait(timeout=5)
                report['cv_close_note']='Camera process required termination after motor control ended.'
        report['camera_returncode']=camera.returncode if camera is not None else None
        observed.close()
        cv_stdout.close()
        cv_stderr.close()
        save(folder/'result.json',report)
    print(json.dumps({'status':report['status'],'folder':str(folder),'error':report.get('error')},ensure_ascii=False),flush=True)
    return return_code


if __name__=='__main__':
    raise SystemExit(main())
