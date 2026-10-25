"""Automatic main/supplement orchestration with simulated telemetry, no hardware."""
from contextlib import contextmanager
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bench_app.controller import Controller,StageFailure
from bench_app.data import read_json,write_json
from bench_app.model import MAIN_LEVELS,MAIN_TEMPLATE,TEMPLATES,Plan


@contextmanager
def rig(force=None,fail_stage=None,short_stage=None,cancel_at_transition=False):
    with tempfile.TemporaryDirectory() as directory:
        base=Path(directory);reference=base/'reference.png';reference.write_bytes(b'fixture')
        calls=[];waits=[];c=Controller();original_emit=c.emit
        def emit(kind,**values):
            original_emit(kind,**values)
            if kind=='schedule' and cancel_at_transition:c.cancelled.set()
        c.emit=emit
        c.cancelled.wait=lambda seconds=None:waits.append(seconds) or False
        def child(command,folder):
            folder.mkdir(parents=True)
            value=float(command[command.index('--step-value')+1])
            stage=int(folder.name.split('_')[1]);calls.append((stage,value,command,folder))
            if '--prepare-only' in command:return {'status':'preparation_passed_no_motor_commands'}
            if stage==fail_stage and '_resume' not in folder.name:
                raise StageFailure('simulated camera failure')
            run='test_'+folder.name
            write_json(folder/'result.json',dict(run_id=run,value=value,
                status='software_checks_passed_physical_confirmation_pending',
                stop_commands_acknowledged=True,reported_output_returned_zero=True,other_channels_zero=True,
                sweep_steps=[dict(control=dict(target_window=dict(started_monotonic_s=100,seconds=10)))]))
            count=3 if stage==short_stage and '_resume' not in folder.name else 20
            thrust=force(value) if force else value*7000/9
            rows=[]
            for i in range(count):
                fields=[dict(id=k,unit=u,value=str(v),valid=True) for k,u,v in (
                    ('upper','g',thrust),('voltage','V',24),('current','A',1),('power','W',24))]
                rows.append(dict(run_id=run,frame_seq=i,captured_monotonic_ns=int((102.05+i*.35)*1e9),fields=fields))
            (folder/'observed_cv.jsonl').write_text('\n'.join(json.dumps(row) for row in rows),encoding='utf-8')
            return read_json(folder/'result.json')
        c._run_child=child
        with patch('bench_app.controller.DATA',base/'sessions'),patch('bench_app.controller.WorkspaceLock'), \
             patch('bench_app.controller.validate_profile',return_value={'reference_image':str(reference)}):
            yield c,calls,waits
        if c.thread and c.thread.is_alive():
            c.cancelled.set();c.thread.join(timeout=5)


def start(c,prepare=False):
    plan=Plan(MAIN_LEVELS,auto_supplement=True)
    c.start(plan,'real',operator=dict(confirmed=True,plan_signature=plan.signature),prepare_only=prepare)
    c.thread.join(timeout=8)
    assert not c.active,'simulated orchestration did not finish'
    return plan


class AutomaticTests(unittest.TestCase):
    def test_complete_main_then_eight_supplements_saved_and_distinct(self):
        with rig() as (c,calls,waits):
            start(c)
            self.assertEqual(TEMPLATES[MAIN_TEMPLATE],MAIN_LEVELS)
            self.assertEqual([value for _,value,_,_ in calls[:10]],list(MAIN_LEVELS))
            self.assertEqual(len(calls),18)
            self.assertIn('--extreme-range',calls[9][2]);self.assertEqual(calls[9][1],1.0)
            self.assertEqual(c.session['status'],'software_complete_waiting_operator')
            self.assertEqual(c.session['supplement']['status'],'completed')
            self.assertEqual(len(c.session['points']),18)
            self.assertEqual([p['phase'] for p in c.session['points']],['main']*10+['supplement']*8)
            self.assertEqual([p['stage_number'] for p in c.session['points']],list(range(1,19)))
            self.assertEqual(len([p for p in c.session['points'] if p['level']==.5]),2)
            self.assertEqual(waits.count(5),17)  # Includes the main/supplement boundary.
            self.assertTrue((c.folder/'supplement_plan.json').exists())
            self.assertIsNone(c.session['physical_confirmation'])
            for _,_,command,_ in calls[1:]:self.assertIn('--residual-thrust-unlimited',command)
            with (c.folder/'results.csv').open(encoding='utf-8-sig',newline='') as stream:
                rows=list(csv.DictReader(stream))
            self.assertEqual([row['phase'] for row in rows],['main']*10+['supplement']*8)
            self.assertIn('自动补点计划',(c.folder/'report.md').read_text(encoding='utf-8'))
            events=[]
            while not c.events.empty():events.append(c.events.get())
            self.assertTrue(all(event['total']==18 for event in events if event['kind']=='step'))

    def test_main_failure_stops_before_planning_supplement(self):
        with rig(fail_stage=6) as (c,calls,_):
            start(c)
            self.assertEqual(len(calls),6)
            self.assertEqual(c.session['supplement']['status'],'pending')
            self.assertEqual(c.session['status'],'aborted_waiting_operator')
            self.assertFalse((c.folder/'supplement_plan.json').exists())

    def test_inadequate_coverage_and_nonmonotonic_main_preserve_ten_points(self):
        for force in [lambda value:value*100,
                      lambda value:200 if value==.7 else value*7000/9]:
            with self.subTest(force=force),rig(force=force) as (c,calls,_):
                start(c)
                self.assertEqual(len(calls),10)
                self.assertEqual(len(c.session['points']),10)
                self.assertEqual(c.session['status'],'aborted_waiting_operator')
                self.assertEqual(c.session['supplement']['status'],'planning_failed')
                self.assertIn('自动补点无法生成',c.session['error'])

    def test_cancel_at_transition_and_resume_uses_saved_supplement(self):
        with rig(cancel_at_transition=True) as (c,calls,_):
            original=start(c)
            self.assertEqual(len(calls),10)
            saved=list(c.session['supplement']['levels'])
            self.assertEqual(c.session['status'],'cancelled_waiting_operator')
            c.confirm_physical_stop()
            remaining=c.resume_remaining_levels()[1]
            self.assertEqual(list(remaining),saved)
            plan=Plan(remaining,auto_supplement=True)
            with patch('bench_app.controller.supplement_levels',side_effect=AssertionError('Must not regenerate')):
                c.resume(plan,operator=dict(confirmed=True,plan_signature=plan.signature))
                c.thread.join(timeout=8)
            self.assertEqual(len(calls),18)
            self.assertEqual(c.session['supplement']['levels'],saved)
            self.assertEqual(c.session['status'],'software_complete_waiting_operator')

    def test_failed_duplicate_supplement_is_not_satisfied_by_completed_main(self):
        with rig(short_stage=14) as (c,calls,_):
            start(c)
            self.assertEqual(c.session['points'][4]['level'],.5)
            self.assertEqual(c.session['failed_stage']['stage_number'],14)
            self.assertEqual(c.session['failed_stage']['level'],.5)
            c.confirm_physical_stop()
            index,levels=c.resume_remaining_levels()
            self.assertEqual(index,13);self.assertEqual(levels[0],.5)
            plan=Plan(levels,auto_supplement=True)
            c.resume(plan,operator=dict(confirmed=True,plan_signature=plan.signature))
            c.thread.join(timeout=8)
            self.assertEqual(c.session['status'],'software_complete_waiting_operator')
            self.assertEqual(len(c.session['points']),18)
            self.assertEqual(len({p['stage_number'] for p in c.session['points']}),18)
            self.assertEqual(c.session['points'][4]['phase'],'main')
            self.assertEqual([stage for stage,_,_,_ in calls].count(14),3)

    def test_resume_failed_main_then_generates_supplement(self):
        with rig(fail_stage=6) as (c,calls,_):
            start(c);c.confirm_physical_stop()
            plan=Plan(c.resume_remaining_levels()[1],auto_supplement=True)
            c.resume(plan,operator=dict(confirmed=True,plan_signature=plan.signature))
            c.thread.join(timeout=8)
            self.assertEqual(c.session['status'],'software_complete_waiting_operator')
            self.assertEqual(len(c.session['points']),18)
            self.assertEqual([stage for stage,_,_,_ in calls].count(1),1)

    def test_precheck_does_not_run_main_or_generate_supplement(self):
        with rig() as (c,calls,_):
            start(c,prepare=True)
            self.assertEqual(len(calls),1)
            self.assertIn('--prepare-only',calls[0][2]);self.assertNotIn('--execute',calls[0][2])
            self.assertNotIn('supplement',c.session)
            self.assertEqual(c.session['status'],'preparation_passed_no_motor_commands')

    def test_failed_plan_persistence_never_starts_supplement(self):
        with rig() as (c,calls,_):
            def write(path,value):
                if Path(path).name=='supplement_plan.json':raise OSError('simulated storage failure')
                return write_json(path,value)
            with patch('bench_app.controller.write_json',side_effect=write):start(c)
            self.assertEqual(len(calls),10)
            self.assertEqual(c.session['status'],'aborted_waiting_operator')
            self.assertEqual(len(c.session['points']),10)
            self.assertIn('storage failure',c.session['error'])

    def test_generated_levels_cannot_exceed_confirmed_main_range(self):
        with rig() as (c,calls,_),patch('bench_app.controller.supplement_levels',
                                      return_value=(.1,.2,.3,.4,.5,.6,.7,1.001)):
            start(c)
            self.assertEqual(len(calls),10)
            self.assertEqual(c.session['supplement']['status'],'planning_failed')
            self.assertIn('超出已确认',c.session['error'])

    def test_policy_is_in_consent_and_cannot_be_silently_changed_on_resume(self):
        regular=Plan(MAIN_LEVELS);automatic=Plan(MAIN_LEVELS,auto_supplement=True)
        self.assertNotEqual(regular.signature,automatic.signature)
        with rig(fail_stage=2) as (c,calls,_):
            with self.assertRaises(ValueError):
                c.start(automatic,'real',operator=dict(confirmed=True,plan_signature=regular.signature))
            self.assertEqual(calls,[])
            start(c);c.confirm_physical_stop()
            plan=Plan(c.resume_remaining_levels()[1])
            with self.assertRaises(ValueError):
                c.resume(plan,operator=dict(confirmed=True,plan_signature=plan.signature))

    def test_demo_and_custom_auto_plans_fail_before_opening_devices(self):
        c=Controller()
        with self.assertRaises(ValueError):c.start(Plan(MAIN_LEVELS,auto_supplement=True),'demo')
        with self.assertRaises(ValueError):c.start(Plan((.1,.2),auto_supplement=True),'real')
        self.assertIsNone(c.folder)


if __name__=='__main__':unittest.main()
