"""Offline tests: fake child processes and fixture telemetry, no device access."""
import io
import json
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import patch

from bench_app.controller import Controller,StageFailure,WorkspaceLock
from bench_app.data import analyze_run,load_campaign,number,read_json,supplement_levels,write_json
from bench_app.model import Plan,Step,LEVELS,build_command,validate_profile,CAMERA_CONFIG

def fixture(folder,status='software_checks_passed_physical_confirmation_pending'):
    write_json(folder/'result.json',dict(run_id='fixture',value=.06,status=status,
        stop_commands_acknowledged=True,reported_output_returned_zero=True,other_channels_zero=True,
        sweep_steps=[dict(control=dict(target_window=dict(started_monotonic_s=100,seconds=15)))]))
    samples=[]
    for i,(t,force,current) in enumerate([(100.5,999,9),(102,10,1),(103,20,2),(114.9,30,3),(115,999,9)]):
        values=[('upper','g',force),('voltage','V',24),('current','A',current),('power','W',1)]
        samples.append(dict(run_id='fixture',frame_seq=i,captured_monotonic_ns=int(t*1e9),fields=[
            dict(id=k,unit=u,value=str(v),valid=True) for k,u,v in values]))
    with (folder/'observed_cv.jsonl').open('w',encoding='utf-8') as stream:
        for item in samples+[samples[2]]:stream.write(json.dumps(item)+'\n')

class AppTests(unittest.TestCase):
    def test_multistep_real_orchestration_never_runs_next_step_after_fault(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);calls=[];reference=base/'reference.png';reference.write_bytes(b'fixture')
            class FailedChild:
                def __init__(self,command,**kwargs):
                    calls.append(command)
                    folder=Path(command[command.index('--result-dir')+1]);folder.mkdir(parents=True)
                    write_json(folder/'result.json',dict(run_id='fixture',status='aborted',error='current boundary'))
                    self.stdout=io.StringIO('TRIAL_ABORTED: current boundary\n')
                def poll(self):return 1
            c=Controller(process_factory=FailedChild);plan=Plan((.03,.04))
            with patch('bench_app.controller.DATA',base/'sessions'),patch('bench_app.controller.validate_profile',return_value={'reference_image':str(reference)}):
                c.start(plan,'real',operator={'confirmed':True,'plan_signature':plan.signature})
                c.thread.join(timeout=5)
            self.assertFalse(c.active);self.assertEqual(len(calls),1)
            self.assertEqual(c.session['status'],'aborted_waiting_operator')
            self.assertEqual(c.session['points'],[])
            self.assertIsNone(c.session['physical_confirmation'])

    def test_statistical_shortfall_retries_only_current_stage_then_continues(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);calls=[];commands=[];reference=base/'reference.png';reference.write_bytes(b'fixture')
            def write_stage(folder,good):
                level=float(folder.name.split('_')[2])
                run_id='fixture_'+folder.name
                write_json(folder/'result.json',dict(run_id=run_id,value=level,
                    status='software_checks_passed_physical_confirmation_pending',
                    stop_commands_acknowledged=True,reported_output_returned_zero=True,
                    other_channels_zero=True,
                    sweep_steps=[dict(control=dict(target_window=dict(started_monotonic_s=100,seconds=15)))]))
                times=[102.2+i*.5 for i in range(24)] if good else [102.2,103.0,114.0]
                rows=[]
                for i,when in enumerate(times):
                    fields=[dict(id='upper',unit='g',value=str(20+i%3),valid=True),
                            dict(id='voltage',unit='V',value='24.0',valid=True),
                            dict(id='current',unit='A',value='1.0',valid=True),
                            dict(id='power',unit='W',value='24.0',valid=True)]
                    rows.append(dict(run_id=run_id,frame_seq=i,captured_monotonic_ns=int(when*1e9),fields=fields))
                (folder/'observed_cv.jsonl').write_text('\n'.join(json.dumps(r) for r in rows),encoding='utf-8')
            class ScriptedChild:
                def __init__(self,command,**kwargs):
                    folder=Path(command[command.index('--result-dir')+1]);folder.mkdir(parents=True)
                    calls.append(folder.name);commands.append(command)
                    good='_retry' in folder.name or 'stage_02' in folder.name
                    write_stage(folder,good)
                    self.stdout=io.StringIO('STEP_START: value=fixture\nSTEP_ENDED: fixture\n')
                def poll(self):return 0
            c=Controller(process_factory=ScriptedChild)
            c.cancelled.wait=lambda timeout=None:False
            plan=Plan((.03,.04))
            with patch('bench_app.controller.DATA',base/'sessions'),patch('bench_app.controller.validate_profile',return_value={'reference_image':str(reference)}):
                c.start(plan,'real',operator=dict(confirmed=True,plan_signature=plan.signature))
                c.thread.join(timeout=5)
            self.assertFalse(c.active)
            self.assertEqual(calls,['stage_01_0.03','stage_01_0.03_retry1','stage_02_0.04'])
            self.assertEqual([p['level'] for p in c.session['points']],[.03,.04])
            self.assertEqual(len(c.session['retry_log']),1)
            self.assertEqual(c.session['retry_log'][0]['level'],.03)
            self.assertEqual(c.session['retry_log'][0]['samples'],3)
            self.assertEqual(c.session['status'],'software_complete_waiting_operator')
            self.assertNotIn('--residual-thrust-unlimited',commands[0])
            for command in commands[1:]:
                self.assertIn('--residual-thrust-unlimited',command)
            self.assertTrue(c.session['residual_thrust_policy']['unlimited'])
            self.assertIsNone(c.session['residual_thrust_policy']['max_absolute_g'])
    def test_confirmed_abort_resumes_from_failed_stage_without_repeating_points(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);calls=[];commands=[];reference=base/'reference.png';reference.write_bytes(b'fixture')
            def write_stage(folder,good):
                level=float(folder.name.split('_')[2])
                if good:
                    run_id='fixture_'+folder.name
                    write_json(folder/'result.json',dict(run_id=run_id,value=level,
                        status='software_checks_passed_physical_confirmation_pending',
                        stop_commands_acknowledged=True,reported_output_returned_zero=True,
                        other_channels_zero=True,
                        sweep_steps=[dict(control=dict(target_window=dict(started_monotonic_s=100,seconds=15)))]))
                    rows=[]
                    for i in range(24):
                        when=102.2+i*.5
                        fields=[dict(id='upper',unit='g',value=str(20+i%3),valid=True),
                                dict(id='voltage',unit='V',value='24.0',valid=True),
                                dict(id='current',unit='A',value='1.0',valid=True),
                                dict(id='power',unit='W',value='24.0',valid=True)]
                        rows.append(dict(run_id=run_id,frame_seq=i,captured_monotonic_ns=int(when*1e9),fields=fields))
                    (folder/'observed_cv.jsonl').write_text('\n'.join(json.dumps(r) for r in rows),encoding='utf-8')
                else:
                    write_json(folder/'result.json',dict(run_id='fixture_'+folder.name,status='aborted',
                        error='Fresh stable baseline not ready: residual 3 g'))
            class ScriptedChild:
                def __init__(self,command,**kwargs):
                    folder=Path(command[command.index('--result-dir')+1]);folder.mkdir(parents=True)
                    calls.append(folder.name);commands.append(command)
                    # Stage 1 succeeds, the original stage 2 aborts, and its resume succeeds.
                    good=('stage_01' in folder.name) or ('resume1' in folder.name)
                    write_stage(folder,good);self.code=0 if good else 1
                    self.stdout=io.StringIO('STEP_START: value=fixture\nSTEP_ENDED: fixture\n')
                def poll(self):return self.code
            c=Controller(process_factory=ScriptedChild)
            c.cancelled.wait=lambda timeout=None:False
            plan=Plan((.025,.03))
            with patch('bench_app.controller.DATA',base/'sessions'),patch('bench_app.controller.validate_profile',return_value={'reference_image':str(reference)}):
                c.start(plan,'real',operator=dict(confirmed=True,plan_signature=plan.signature))
                c.thread.join(timeout=5)
                self.assertEqual(c.session['status'],'aborted_waiting_operator')
                self.assertEqual(c.session['failed_stage']['index'],1)
                self.assertEqual([p['level'] for p in c.session['points']],[.025])
                self.assertEqual(calls,['stage_01_0.025','stage_02_0.03'])
                with self.assertRaises(ValueError):c.resume(Plan((.03,)),operator=dict(
                    confirmed=True,plan_signature=Plan((.03,)).signature))
                c.confirm_physical_stop()
                remaining=Plan((.03,),plan.cooldown_seconds,plan.settle_seconds)
                c.resume(remaining,operator=dict(confirmed=True,plan_signature=remaining.signature))
                c.thread.join(timeout=5)
            self.assertFalse(c.active)
            self.assertEqual(calls,['stage_01_0.025','stage_02_0.03','stage_02_0.03_resume1'])
            self.assertEqual([p['level'] for p in c.session['points']],[.025,.03])
            self.assertEqual(c.session['status'],'software_complete_waiting_operator')
            self.assertEqual(len(c.session['resume_log']),1)
            self.assertEqual(c.session['resume_log'][0]['start_index'],1)
            self.assertEqual(c.session['resume_log'][0]['remaining_levels'],[.03])
            self.assertIsNone(c.session['failed_stage'])
            self.assertTrue((c.folder/'operator_resume1.json').is_file())
            resume_command=commands[-1]
            self.assertIn('--residual-thrust-unlimited',resume_command)
            self.assertNotIn('--residual-thrust-unlimited',commands[0])
            self.assertIn('--residual-thrust-unlimited',commands[1])

    def test_resume_keeps_cooldown_between_its_remaining_stages(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);calls=[];waits=[];reference=base/'reference.png';reference.write_bytes(b'fixture')
            def write_stage(folder,good):
                level=float(folder.name.split('_')[2])
                if good:
                    run_id='fixture_'+folder.name
                    write_json(folder/'result.json',dict(run_id=run_id,value=level,
                        status='software_checks_passed_physical_confirmation_pending',
                        stop_commands_acknowledged=True,reported_output_returned_zero=True,
                        other_channels_zero=True,
                        sweep_steps=[dict(control=dict(target_window=dict(started_monotonic_s=100,seconds=15)))]))
                    rows=[]
                    for i in range(24):
                        when=102.2+i*.5
                        fields=[dict(id='upper',unit='g',value=str(20+i%3),valid=True),
                                dict(id='voltage',unit='V',value='24.0',valid=True),
                                dict(id='current',unit='A',value='1.0',valid=True),
                                dict(id='power',unit='W',value='24.0',valid=True)]
                        rows.append(dict(run_id=run_id,frame_seq=i,captured_monotonic_ns=int(when*1e9),fields=fields))
                    (folder/'observed_cv.jsonl').write_text('\n'.join(json.dumps(r) for r in rows),encoding='utf-8')
                else:
                    write_json(folder/'result.json',dict(run_id='fixture_'+folder.name,status='aborted',
                        error='Fresh stable baseline not ready: residual 3 g'))
            class ScriptedChild:
                def __init__(self,command,**kwargs):
                    folder=Path(command[command.index('--result-dir')+1]);folder.mkdir(parents=True)
                    calls.append(folder.name)
                    good=('stage_01' in folder.name) or ('resume1' in folder.name)
                    write_stage(folder,good);self.code=0 if good else 1
                    self.stdout=io.StringIO('STEP_START: value=fixture\nSTEP_ENDED: fixture\n')
                def poll(self):return self.code
            c=Controller(process_factory=ScriptedChild)
            def wait(timeout=None):
                waits.append(timeout);return False
            c.cancelled.wait=wait
            plan=Plan((.025,.03,.04))
            with patch('bench_app.controller.DATA',base/'sessions'),patch('bench_app.controller.validate_profile',return_value={'reference_image':str(reference)}):
                c.start(plan,'real',operator=dict(confirmed=True,plan_signature=plan.signature))
                c.thread.join(timeout=5)
                self.assertEqual(c.session['status'],'aborted_waiting_operator')
                c.confirm_physical_stop()
                remaining=Plan((.03,.04),plan.cooldown_seconds,plan.settle_seconds)
                c.resume(remaining,operator=dict(confirmed=True,plan_signature=remaining.signature))
                c.thread.join(timeout=5)
            self.assertFalse(c.active)
            self.assertEqual(c.session['status'],'software_complete_waiting_operator')
            self.assertEqual(calls,['stage_01_0.025','stage_02_0.03',
                                    'stage_02_0.03_resume1','stage_03_0.04_resume1'])
            # One cooldown after stage 1, and one between the two resumed stages.
            self.assertEqual(waits.count(plan.cooldown_seconds),2)

    def test_resume_rejects_wrong_remaining_plan_and_unconfirmed_stop(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);calls=[];reference=base/'reference.png';reference.write_bytes(b'fixture')
            class FailedChild:
                def __init__(self,command,**kwargs):
                    calls.append(command)
                    folder=Path(command[command.index('--result-dir')+1]);folder.mkdir(parents=True)
                    write_json(folder/'result.json',dict(run_id='fixture',status='aborted',error='stop unconfirmed'))
                    self.stdout=io.StringIO('TRIAL_ABORTED: fixture\n')
                def poll(self):return 1
            c=Controller(process_factory=FailedChild);plan=Plan((.03,.04))
            with patch('bench_app.controller.DATA',base/'sessions'),patch('bench_app.controller.validate_profile',return_value={'reference_image':str(reference)}):
                c.start(plan,'real',operator=dict(confirmed=True,plan_signature=plan.signature))
                c.thread.join(timeout=5)
                full=Plan((.03,.04))
                with self.assertRaises(ValueError):
                    c.resume(full,operator=dict(confirmed=True,plan_signature=full.signature))
                c.confirm_physical_stop()
                tail=Plan((.04,))
                with self.assertRaises(ValueError):
                    c.resume(tail,operator=dict(confirmed=True,plan_signature=tail.signature))
                with self.assertRaises(ValueError):
                    c.resume(full,operator=dict(confirmed=True,plan_signature='wrong'))
            self.assertEqual(len(calls),1)
            self.assertEqual(c.session['status'],'aborted_operator_confirmed')

    def test_latest_resumable_session_loads_from_disk(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);session_dir=base/'old_session';session_dir.mkdir()
            plan=Plan((.03,.04))
            write_json(session_dir/'camera_config.json',dict(camera='fixture'))
            write_json(session_dir/'session.json',dict(mode='实机测试',status='aborted_operator_confirmed',
                plan=plan.as_dict(),points=[dict(level=.03,quality=dict(passed=True),run_dir='fixture')],
                physical_confirmation=dict(stopped=True)))
            c=Controller()
            with patch('bench_app.controller.DATA',base):
                latest=c.latest_resumable()
                self.assertEqual(latest[1],session_dir)
                c.load_latest_resumable()
            self.assertEqual(c.folder,session_dir)
            self.assertTrue(c.can_resume())
            info=c.resume_remaining_levels()
            self.assertEqual(info,(1,(.04,)))
            self.assertFalse(c.active)
            self.assertEqual(c.session['status'],'aborted_operator_confirmed')

    def test_residual_thrust_flags_are_rejected_for_precheck_and_out_of_range(self):
        step=Step(.03)
        command=build_command(step,'COM42','a','b',operator_record='operator.json',residual_unlimited=True)
        self.assertIn('--residual-thrust-unlimited',command)
        precheck=build_command(step,'COM42','a','b',prepare_only=True,residual_unlimited=True)
        self.assertNotIn('--residual-thrust-unlimited',precheck)
        bounded=build_command(step,'COM42','a','b',operator_record='operator.json',residual_thrust_limit=6)
        self.assertIn('--residual-thrust-limit',bounded)
        with self.assertRaises(ValueError):
            build_command(step,'COM42','a','b',operator_record='operator.json',
                          residual_thrust_limit=6,residual_unlimited=True)
        for limit in (0,7):
            with self.assertRaises(ValueError):
                build_command(step,'COM42','a','b',operator_record='operator.json',
                              residual_thrust_limit=limit)

    def test_followup_plan_allows_residual_on_its_first_stage(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t);commands=[];reference=base/'reference.png';reference.write_bytes(b'fixture')
            class FailedChild:
                def __init__(self,command,**kwargs):
                    commands.append(command)
                    folder=Path(command[command.index('--result-dir')+1]);folder.mkdir(parents=True)
                    write_json(folder/'result.json',dict(run_id='fixture',status='aborted',error='fixture stop'))
                    self.stdout=io.StringIO('TRIAL_ABORTED: fixture\n')
                def poll(self):return 1
            c=Controller(process_factory=FailedChild)
            c.cancelled.wait=lambda timeout=None:False
            plan=Plan((.45,),followup_first=True)
            with patch('bench_app.controller.DATA',base/'sessions'),patch('bench_app.controller.validate_profile',return_value={'reference_image':str(reference)}):
                c.start(plan,'real',operator=dict(confirmed=True,plan_signature=plan.signature))
                c.thread.join(timeout=5)
            self.assertIn('--residual-thrust-unlimited',commands[0])
            self.assertFalse(c.session['residual_thrust_policy']['first_stage_strict'])

    def test_frozen_ui_heartbeat_causes_finite_control_to_stop(self):
        from px4_mvp.hold_core import finite_hold,check_ui_heartbeat
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'heartbeat.json';write_json(p,dict(monotonic_ns=int(100e9)))
            class Clock:
                value=100
                def __call__(self):return self.value
            clock=Clock()
            class Bench:
                stops=0
                sent=[]
                def check(self):pass
                def command(self,value,lease,ack):self.sent.append((clock(),lease))
                def receive(self):clock.value+=.05
                def stop(self):self.stops+=1
            bench=Bench()
            with self.assertRaisesRegex(RuntimeError,'stopped responding'):
                finite_hold(bench,lambda:check_ui_heartbeat(p,clock),clock,seconds=10)
            self.assertEqual(bench.stops,1)
            self.assertTrue(bench.sent)
            self.assertLess(clock(),102.1)
            self.assertLess(max(t+lease for t,lease in bench.sent),103.1)
    def test_ui_watchdog_rejects_stale_future_and_invalid_heartbeats(self):
        from px4_mvp.hold_core import check_ui_heartbeat
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'heartbeat.json'
            write_json(p,dict(monotonic_ns=int(99.5e9)))
            self.assertEqual(check_ui_heartbeat(p,lambda:100),.5)
            for n in [97e9,101e9,'nan',None]:
                write_json(p,dict(monotonic_ns=n))
                with self.assertRaises(RuntimeError):check_ui_heartbeat(p,lambda:100)
    def test_pending_physical_stop_blocks_new_session_even_when_process_ended(self):
        controller=Controller();controller.session={'status':'aborted_waiting_operator'}
        with self.assertRaisesRegex(ValueError,'停转'):controller.start(Plan((.06,)))
    def test_atomic_save_retries_transient_windows_sharing_error(self):
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/'state.json';original=Path.replace;attempts=[]
            def sharing_once(source,target):
                attempts.append(1)
                if len(attempts)==1:raise PermissionError('File temporarily open')
                return original(source,target)
            with patch.object(Path,'replace',sharing_once):write_json(path,dict(status='saved'))
            self.assertEqual(read_json(path)['status'],'saved')
            self.assertEqual(len(attempts),2)
    def test_plan_rejects_out_of_range_nan_duplicate_and_descending_targets(self):
        for values in [(0.009,),(1.001,),(.0601,),(float('nan'),),(.06,.06),(.10,.03),()]:
            with self.assertRaises(ValueError):Plan(values)
        with self.assertRaises(ValueError):Plan((.06,),cooldown_seconds=0)
        with self.assertRaises(ValueError):Plan((.06,),settle_seconds=9)

    def test_plan_accepts_custom_three_decimal_targets_not_in_fixed_list(self):
        levels=(.021,.037,.061,.605,.735,1.0)
        plan=Plan(levels)
        for level in levels:
            self.assertNotIn(level,LEVELS)
            step=Step(level)
            self.assertEqual(step.level,level)
            self.assertLessEqual(step.rise_seconds+step.hold_seconds,20)
        self.assertEqual([s['stage'] for s in plan.as_dict()['steps']],
                         ['低段','低段','中段','高段','高段','高段'])
        self.assertTrue(all(step.hold_seconds==10 for step in map(Step,levels)))
        self.assertEqual(Plan(levels).cooldown_seconds,5)
    def test_plan_carries_no_fixed_hardware_thresholds(self):
        plan=Plan((.025,.06,.10,.90))
        steps=plan.as_dict()['steps']
        for step in steps:
            self.assertNotIn('policy',step)
            self.assertFalse({'current','power','thrust','hard_current','hard_power'}&set(step))
        self.assertEqual([s['stage'] for s in steps],['低段','低段','中段','高段'])

    def test_supplement_levels_interpolates_eight_points_inside_300_500_g(self):
        def point(level,thrust):
            return dict(level=level,metrics=dict(thrust_g=dict(mean=thrust)))
        levels=supplement_levels([point(.10,100),point(.30,300),point(.50,500),point(.70,700)])
        self.assertEqual(len(levels),8)
        self.assertEqual(levels,tuple(sorted(set(levels))))
        self.assertTrue(all(.30<level<.50 for level in levels))
        self.assertAlmostEqual(levels[0],.322,places=3)
        self.assertAlmostEqual(levels[-1],.478,places=3)
        with self.assertRaises(ValueError):
            supplement_levels([point(.10,100),point(.30,400)])
        with self.assertRaises(ValueError):
            supplement_levels([point(.10,200),point(.30,150),point(.50,500)])
        with self.assertRaises(ValueError):
            supplement_levels([point(.10,350)])

    def test_ten_second_hold_passes_child_validation_for_all_stages(self):
        import subprocess,sys
        from bench_app.model import ROOT
        base=[sys.executable,'-X','utf8',str(ROOT/'px4_mvp/timed_hold.py'),
              '--next-step','--step-seconds','10']
        cases=[('0.025',()),('0.03',('--gentle-rise','--expanded-trial')),
               ('0.10',('--gentle-rise','--high-range')),
               ('0.60',('--gentle-rise','--high-range')),
               ('0.90',('--gentle-rise','--extreme-range'))]
        for value,flags in cases:
            result=subprocess.run(base+['--step-value',value,*flags],
                                  capture_output=True,text=True,encoding='utf-8')
            self.assertEqual(result.returncode,2,value)
            # Validation must reach the missing execute/guard flag, proving the
            # ten-second hold was accepted for this stage.
            self.assertIn('Explicit --execute and --guarded-prop-bench required.',
                          result.stderr,value)
        result=subprocess.run(base+['--step-value','0.03','--prepare-only',
                                    '--residual-thrust-unlimited'],
                              capture_output=True,text=True,encoding='utf-8')
        self.assertIn('Residual-thrust policy is not allowed for preparation-only checks.',
                      result.stderr)

    def test_command_routes_every_supported_level_to_existing_guarded_backend(self):
        for level in LEVELS+(.021,.037,.061,.605,.735,1.0):
            step=Step(level)
            cmd=build_command(step,'COM42','cal.json','result',operator_record='operator.json')
            self.assertIn('--execute',cmd);self.assertIn('--guarded-prop-bench',cmd)
            self.assertIn('--operator-record',cmd)
            self.assertLessEqual(step.rise_seconds+step.hold_seconds,20)
            if level>.6:self.assertIn('--extreme-range',cmd)
            elif level>.06:self.assertIn('--high-range',cmd)
            elif level>.025:self.assertIn('--expanded-trial',cmd)
            self.assertNotIn('--thrust-limit',cmd)
            self.assertNotIn('--sustained-boundaries',cmd)
        with self.assertRaises(ValueError):build_command(Step(.06),'COM42','a','b')
        with self.assertRaises(ValueError):build_command(Step(.06),'COM42 & command','a','b',prepare_only=True)

    def test_prepare_cannot_send_execute_flag(self):
        cmd=build_command(Step(.90),'COM42','a','b',prepare_only=True)
        self.assertIn('--prepare-only',cmd);self.assertNotIn('--execute',cmd)
        self.assertNotIn('--guarded-prop-bench',cmd)

    def test_analysis_excludes_settling_end_boundary_and_duplicate_frames(self):
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t);fixture(folder);p=analyze_run(folder,2)
            self.assertEqual(p['samples'],3)
            self.assertEqual(p['metrics']['thrust_g']['mean'],20)
            self.assertEqual(p['metrics']['power_w']['mean'],48)
            self.assertEqual(p['metrics']['display_power_w']['mean'],1)
            self.assertEqual(p['paired_samples'],3)
            self.assertFalse(p['zero_correction_applied'])

    def test_invalid_and_wrong_unit_are_not_zero_filled(self):
        self.assertIsNone(number(dict(valid=True,unit='A',value='nan'),'A'))
        self.assertIsNone(number(dict(valid=True,unit='V',value='1'),'A'))
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t);fixture(folder)
            lines=[json.loads(x) for x in (folder/'observed_cv.jsonl').read_text().splitlines()]
            for row in lines:row['fields'][0].update(valid=False,value=None)
            (folder/'observed_cv.jsonl').write_text('\n'.join(json.dumps(x) for x in lines))
            point=analyze_run(folder,2)
            self.assertIsNone(point['metrics']['thrust_g']['mean'])
            self.assertEqual(point['metrics']['thrust_g']['n'],0)
            self.assertIsNone(point['efficiency_g_per_w'])

    def test_low_led_readability_keeps_point_until_valid_sample_count_is_short(self):
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t)
            def make(every):
                write_json(folder/'result.json',dict(run_id='low',value=.85,
                    status='software_checks_passed_physical_confirmation_pending',
                    stop_commands_acknowledged=True,reported_output_returned_zero=True,
                    other_channels_zero=True,
                    sweep_steps=[dict(control=dict(target_window=dict(started_monotonic_s=100,seconds=15)))]))
                rows=[]
                for i in range(30):
                    fields=[dict(id='upper',unit='g',value='800',valid=i%every==0),
                            dict(id='voltage',unit='V',value='23.1',valid=True),
                            dict(id='current',unit='A',value='18.2',valid=True),
                            dict(id='power',unit='W',value='420.4',valid=True)]
                    rows.append(dict(run_id='low',frame_seq=i,
                                     captured_monotonic_ns=int((102.2+i*.4)*1e9),fields=fields))
                (folder/'observed_cv.jsonl').write_text('\n'.join(json.dumps(r) for r in rows),encoding='utf-8')
            make(2)
            point=analyze_run(folder)
            self.assertTrue(point['quality']['passed'])
            self.assertEqual(point['metrics']['thrust_g']['n'],15)
            self.assertTrue(point['quality']['warnings'])
            self.assertIn('可读率',point['note'])
            make(4)
            point=analyze_run(folder)
            self.assertFalse(point['quality']['passed'])
            self.assertIn('拉力',point['quality']['issues'][0])

    def test_precheck_failure_explains_led_readability_without_motor_command(self):
        report=dict(run_id='x',status='aborted',
            error="RuntimeError('Fresh stable baseline not ready: Baseline is not stable: upper')",
            baseline_diagnosis=dict(problem='Baseline is not stable: upper',
                readable_fraction={'upper':.34,'voltage':.95,'current':.95,'power':.84},samples=83))
        text=Controller.explain_failure(report,'raw')
        self.assertIn('拉力 34%',text)
        self.assertIn('没有发送电机指令',text)
        self.assertIn('LED',text)
        self.assertEqual(Controller.explain_failure({},'raw'),'raw')

    def test_aborted_or_unconfirmed_stop_never_becomes_curve_point(self):
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t);fixture(folder,'aborted')
            with self.assertRaises(ValueError):analyze_run(folder)
            fixture(folder);report=read_json(folder/'result.json');report['stop_commands_acknowledged']=False
            write_json(folder/'result.json',report)
            with self.assertRaises(ValueError):analyze_run(folder)

    def test_history_preserves_both_0060_records(self):
        rows=load_campaign()
        self.assertEqual(len(rows),28);self.assertEqual(len([p for p in rows if p['level']==.06]),2)

    def test_current_camera_profile_has_expected_units_and_existing_reference(self):
        profile=validate_profile(CAMERA_CONFIG)
        self.assertTrue(Path(profile['reference_image']).is_file())

    def test_real_session_requires_current_plan_confirmation_before_child_creation(self):
        controller=Controller(process_factory=lambda *a,**k:self.fail('Child must not start'))
        for operator in [None,dict(confirmed=True,plan_signature='wrong')]:
            with self.assertRaises(ValueError):controller.start(Plan((.06,)),mode='real',operator=operator)
        self.assertFalse(controller.active)

    def test_cancellation_marker_reaches_current_child(self):
        with tempfile.TemporaryDirectory() as t:
            controller=Controller();controller.current_run=Path(t);controller.stop()
            self.assertTrue((Path(t)/'CANCEL').exists())
            self.assertTrue(controller.cancelled.is_set())

    def test_failed_child_prevents_auto_continuation(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)
            class FakeProcess:
                def __init__(self,*args,**kwargs):
                    self.stdout=io.StringIO('TRIAL_ABORTED: fixture\n')
                    write_json(root/'result.json',dict(run_id='fixture',status='aborted',error='current boundary'))
                def poll(self):return 1
            controller=Controller(process_factory=FakeProcess)
            with self.assertRaisesRegex(StageFailure,'current boundary'):
                controller._run_child(['fake'],root)

    def test_lock_does_not_release_another_owner(self):
        with tempfile.TemporaryDirectory() as t:
            path=Path(t)/'lock';a=WorkspaceLock(path);b=WorkspaceLock(path)
            a.acquire();b.release();self.assertTrue(path.exists())
            with self.assertRaises(RuntimeError):b.acquire()
            a.release();self.assertFalse(path.exists())

if __name__=='__main__':unittest.main()
