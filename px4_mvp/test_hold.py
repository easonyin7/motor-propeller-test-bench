"""Offline tests only: no MAVLink import, serial access, or camera access."""
import unittest
from hold_core import CVGuard,LoadResponseGuard,finite_hold,finite_rise,finite_profile,rise_profile,finite_sweep,read_snapshot,wait_stopped_baseline,stopped_baseline_for_run,high_range_profile,extreme_range_profile


class Clock:
    def __init__(self): self.t=100.
    def __call__(self): return self.t


class Bench:
    def __init__(self,clock):
        self.clock=clock
        self.sent=[]
        self.stops=0
        self.fail_ack=False
        self.fail_receive=False
        self.fail_stop=False
    def check(self): pass
    def verify_zero(self): pass
    def command(self,value,lease,ack):
        self.sent.append((self.clock(),value,lease))
        if self.fail_ack: raise RuntimeError('ACK missing')
        self.clock.t+=.02
    def receive(self):
        if self.fail_receive: raise OSError('USB lost')
        self.clock.t+=.05
    def stop(self):
        self.stops+=1
        if self.fail_stop: raise RuntimeError('STOP UNCONFIRMED')


def state(clock,seq=1,run='test',stable=True):
    return {'run_id':run,'state':'running','samples':seq,'frame_seq':seq,
            'captured_monotonic_ns':int(clock()*1e9),
            'fields':[{'id':k,'unit':u,'value':'0','valid':True,
                       'stable_value':'0' if stable else None}
                      for k,u in CVGuard.REQUIRED.items()]}


class HoldTests(unittest.TestCase):
    def test_longer_stopped_wait_keeps_confirmation_and_no_commands(self):
        clock=Clock(); bench=Bench(clock)
        def health():
            s=state(clock)
            # Invalid evidence until five seconds must not count toward confirmation.
            if clock.t<105:
                s['fields'][0].update(valid=False,value=None,stable_value=None)
            return s
        wait_stopped_baseline(bench,health,clock,allow_near_zero_thrust=True,timeout_seconds=10)
        self.assertGreaterEqual(clock.t,105.6)
        self.assertLess(clock.t,110)
        self.assertEqual(bench.sent,[])
        clock=Clock();bench=Bench(clock)
        def unreadable():
            s=state(clock)
            for field in s['fields']:field.update(valid=False,value=None,stable_value=None)
            return s
        with self.assertRaises(RuntimeError):
            wait_stopped_baseline(bench,unreadable,clock,timeout_seconds=10)
        self.assertLess(clock.t,110.1)
        self.assertEqual(bench.sent,[])
    def test_expanded_baseline_accepts_zero_one_jitter_without_faking_stability(self):
        clock=Clock(); bench=Bench(clock)
        def health():
            s=state(clock,int(clock.t*10))
            s['fields'][0].update(value=str(int(clock.t*10)%2),stable_value=None)
            CVGuard('test',clock).check(s,require_stable=True,allow_near_zero_thrust=True)
            return s
        s=wait_stopped_baseline(bench,health,clock,allow_near_zero_thrust=True)
        self.assertGreaterEqual(clock.t,100.6)
        self.assertIsNone(s['fields'][0]['stable_value'])
        self.assertEqual(s['fields'][0]['baseline_value'] in ('0','1'),True)
        self.assertEqual(bench.sent,[])
    def test_baseline_accepts_one_gram_jitter_without_expanded_policy(self):
        clock=Clock(); bench=Bench(clock)
        def health():
            s=state(clock,int(clock.t*20))
            s['fields'][0].update(value=str(int(clock.t*20)%2),stable_value=None)
            return s
        snapshot=wait_stopped_baseline(bench,health,clock)
        self.assertGreaterEqual(clock.t,100.6)
        self.assertIn(snapshot['fields'][0]['baseline_value'],('0','1'))
        self.assertEqual(bench.sent,[])
    def test_expanded_baseline_does_not_accept_out_of_range_raw_force(self):
        clock=Clock(); bench=Bench(clock)
        def health():
            s=state(clock); s['fields'][0].update(value='3',stable_value=None)
            return s
        with self.assertRaises(RuntimeError):
            wait_stopped_baseline(bench,health,clock,allow_near_zero_thrust=True)
        self.assertEqual(bench.sent,[])
        with self.assertRaises(RuntimeError):
            CVGuard('test',clock).check(health(),require_stable=True,allow_near_zero_thrust=True)
    def test_gentle_rise_leases_stay_within_each_stage_and_total(self):
        for seconds,target in ((2,.03),(15,.03),(2,.035),(15,.035),(2,.04),(15,.04),(2,.045),(15,.045),(2,.05),(15,.05),(2,.055),(15,.055),(2,.06),(15,.06),(2,.065),(15,.065),(2,.07),(15,.07)):
            clock=Clock(); bench=Bench(clock)
            result=finite_rise(bench,lambda:None,clock,target_seconds=seconds,target=target)
            self.assertEqual(bench.stops,1)
            self.assertEqual({v for _,v,_ in bench.sent},{s['value'] for s in result['profile_stages']})
            for t,value,lease in bench.sent:
                s=next(s for s in result['profile_stages'] if s['value']==value)
                self.assertGreaterEqual(t,s['started_monotonic_s'])
                self.assertLessEqual(t+lease,s['deadline_monotonic_s']+1e-7)
            self.assertEqual(result['target_window']['seconds'],seconds)
            self.assertLessEqual(result['requested_seconds'],20)
            self.assertLess(result['loop_elapsed_seconds'],result['requested_seconds']+.1)
    def test_new_rise_stops_on_boundary_before_highest_target(self):
        clock=Clock(); bench=Bench(clock)
        def health():
            if clock.t>=104.4: raise RuntimeError('current boundary')
        with self.assertRaisesRegex(RuntimeError,'current boundary'):
            finite_rise(bench,health,clock,target_seconds=15,target=.05)
        self.assertEqual(bench.stops,1)
        self.assertNotIn(.05,[v for _,v,_ in bench.sent])
    def test_unplanned_higher_step_is_rejected_without_commands(self):
        for value in (.07001,.075,float('nan')):
            with self.assertRaises(ValueError): rise_profile(value,15)
    def test_custom_low_target_gets_strict_five_second_rise(self):
        for target in (.026,.037,.059):
            profile=rise_profile(target,15)
            self.assertEqual(profile[-1],(target,15))
            self.assertTrue(all(b[0]>a[0] for a,b in zip(profile,profile[1:])))
            self.assertAlmostEqual(sum(s for _,s in profile[:-1]),5,places=9)
    def test_gentle_rise_boundary_failure_never_reaches_target(self):
        clock=Clock(); bench=Bench(clock)
        def health():
            if clock.t>=102.5: raise RuntimeError('current boundary')
        with self.assertRaisesRegex(RuntimeError,'current boundary'):
            finite_rise(bench,health,clock)
        self.assertEqual(bench.stops,1)
        self.assertNotIn(.03,[v for _,v,_ in bench.sent])
    def test_gentle_rise_stalled_host_never_renews_after_total_deadline(self):
        clock=Clock(); bench=Bench(clock)
        def health(): clock.t+=20
        with self.assertRaisesRegex(RuntimeError,'received no command'):
            finite_rise(bench,health,clock,target_seconds=15)
        self.assertEqual(bench.sent,[])
        self.assertEqual(bench.stops,1)
    def test_gentle_rise_ack_loss_prevents_target_stage(self):
        clock=Clock(); bench=Bench(clock)
        def health():
            if clock.t>=102: bench.fail_ack=True
        with self.assertRaisesRegex(RuntimeError,'ACK missing'):
            finite_rise(bench,health,clock)
        self.assertEqual(bench.stops,1)
        self.assertNotIn(.03,[v for _,v,_ in bench.sent])
    def test_zero_load_stops_long_run_after_three_seconds(self):
        clock=Clock(); bench=Bench(clock); guard=LoadResponseGuard(clock(),state(clock))
        with self.assertRaisesRegex(RuntimeError,'No repeated current/power load response'):
            finite_hold(bench,lambda:guard.check(state(clock,int(clock.t*100)),clock()),clock,seconds=20,value=.025)
        self.assertEqual(bench.stops,1)
        self.assertTrue(all(t<103.05 for t,_,_ in bench.sent))
    def test_repeated_same_positive_frame_is_not_repeated_response(self):
        clock=Clock(); guard=LoadResponseGuard(clock(),state(clock))
        positive=state(clock)
        for f in positive['fields']:
            if f['id']=='current': f['value']='0.22'
            if f['id']=='power': f['value']='5.43'
        guard.check(positive,100.5); guard.check(positive,101)
        with self.assertRaises(RuntimeError): guard.check(positive,103.1)
        positive['samples']=2
        self.assertEqual(guard.check(positive,103.2),2)
    def test_invalid_load_values_do_not_prove_response(self):
        clock=Clock(); guard=LoadResponseGuard(clock(),state(clock))
        bad=state(clock)
        for f in bad['fields']:
            if f['id'] in ('current','power'): f.update(value='nan',valid=True)
        with self.assertRaises(RuntimeError): guard.check(bad,103.1)
    def test_snapshot_transient_sharing_error_retries_fresh_read(self):
        calls=[]; waits=[]
        def read():
            calls.append(1)
            if len(calls)<3: raise PermissionError('temporary sharing conflict')
            return '{"samples":23}'
        self.assertEqual(read_snapshot(None,read,waits.append),{'samples':23})
        self.assertEqual(len(calls),3)
        self.assertEqual(waits,[.01,.01])
    def test_snapshot_persistent_sharing_error_is_bounded(self):
        waits=[]
        def read(): raise PermissionError('still locked')
        with self.assertRaises(PermissionError): read_snapshot(None,read,waits.append)
        self.assertEqual(len(waits),5)
    def test_leases_never_outlive_deadline_and_always_stop(self):
        clock=Clock(); bench=Bench(clock)
        result=finite_hold(bench,lambda:None,clock)
        self.assertGreater(len(bench.sent),20)
        self.assertTrue(all(t+d<=110.0000001 and .2<=d<=1 for t,v,d in bench.sent))
        self.assertEqual(bench.stops,1)
        self.assertLess(result['loop_elapsed_seconds'],10.1)
    def test_host_stall_does_not_send_again_after_deadline(self):
        clock=Clock(); bench=Bench(clock)
        def stalled_health(): clock.t+=11
        finite_hold(bench,stalled_health,clock)
        self.assertEqual(bench.sent,[])
        self.assertEqual(bench.stops,1)
    def test_missing_ack_does_not_retry_running_command(self):
        clock=Clock(); bench=Bench(clock); bench.fail_ack=True
        with self.assertRaisesRegex(RuntimeError,'ACK missing'):
            finite_hold(bench,lambda:None,clock)
        self.assertEqual(len(bench.sent),1)
        self.assertEqual(bench.stops,1)
    def test_usb_loss_stops_without_renewal(self):
        clock=Clock(); bench=Bench(clock); bench.fail_receive=True
        with self.assertRaises(OSError): finite_hold(bench,lambda:None,clock)
        self.assertEqual(len(bench.sent),1)
        self.assertEqual(bench.stops,1)
    def test_stop_failure_is_not_success(self):
        clock=Clock(); bench=Bench(clock); bench.fail_stop=True
        with self.assertRaisesRegex(RuntimeError,'STOP UNCONFIRMED'):
            finite_hold(bench,lambda:None,clock,seconds=.2)
    def test_cv_failure_cancels_before_any_output(self):
        clock=Clock(); bench=Bench(clock)
        def bad_health(): raise RuntimeError('CV stale')
        with self.assertRaisesRegex(RuntimeError,'CV stale'):
            finite_hold(bench,bad_health,clock)
        self.assertEqual(bench.sent,[])
        self.assertEqual(bench.stops,1)
    def test_cv_failure_mid_run_prevents_further_renewal(self):
        clock=Clock(); bench=Bench(clock)
        def health():
            if clock.t>=100.8: raise RuntimeError('camera exited')
        with self.assertRaisesRegex(RuntimeError,'camera exited'):
            finite_hold(bench,health,clock)
        self.assertTrue(bench.sent)
        self.assertTrue(all(t<100.8 for t,_,_ in bench.sent))
        self.assertEqual(bench.stops,1)
    def test_duration_and_output_limits(self):
        for duration in (0,20.01,float('nan')):
            with self.assertRaises(ValueError): finite_hold(None,None,None,seconds=duration)
        for value in (.009,.04001,float('nan')):
            with self.assertRaises(ValueError): finite_hold(None,None,None,value=value)
    def test_next_increment_probe_is_bounded_and_stopped(self):
        clock=Clock(); bench=Bench(clock)
        result=finite_hold(bench,lambda:None,clock,seconds=2,value=.025)
        self.assertTrue(bench.sent)
        self.assertTrue(all(value==.025 and t+lease<=102.0000001 for t,value,lease in bench.sent))
        self.assertEqual(bench.stops,1)
        self.assertLess(result['loop_elapsed_seconds'],2.1)
    def test_twenty_second_ceiling_keeps_every_lease_bounded(self):
        clock=Clock(); bench=Bench(clock)
        result=finite_hold(bench,lambda:None,clock,seconds=20,value=.04)
        self.assertTrue(all(t+lease<=120.0000001 and lease<=1 for t,_,lease in bench.sent))
        self.assertGreater(len(bench.sent),45)
        self.assertLess(result['loop_elapsed_seconds'],20.1)
        self.assertEqual(bench.stops,1)
    def test_fifteen_second_run_stops_on_late_data_failure(self):
        clock=Clock(); bench=Bench(clock)
        def health():
            if clock.t>=112: raise RuntimeError('late CV failure')
        with self.assertRaisesRegex(RuntimeError,'late CV failure'):
            finite_hold(bench,health,clock,seconds=15,value=.03)
        self.assertTrue(all(t<112 for t,_,_ in bench.sent))
        self.assertEqual(bench.stops,1)
    def test_sweep_has_three_stopped_levels_and_two_gaps(self):
        clock=Clock(); bench=Bench(clock); starts=[]; ends=[]; gaps=[]
        def rest(seconds): gaps.append(seconds); clock.t+=seconds
        results=finite_sweep(bench,lambda:None,clock,
            lambda i,v:starts.append(v),lambda step:ends.append(step['value']),rest)
        self.assertEqual(starts,[.01,.015,.02])
        self.assertEqual(ends,starts)
        self.assertEqual(gaps,[5,5])
        self.assertEqual(bench.stops,3)
        self.assertEqual(len(results),3)
    def test_sweep_failure_never_starts_later_level(self):
        clock=Clock(); bench=Bench(clock); starts=[]
        def before(index,value):
            starts.append(value)
            if index==1: bench.fail_ack=True
        def rest(seconds): clock.t+=seconds
        with self.assertRaisesRegex(RuntimeError,'ACK missing'):
            finite_sweep(bench,lambda:None,clock,before,lambda step:None,rest)
        self.assertEqual(starts,[.01,.015])
        self.assertEqual(bench.stops,2)
        self.assertNotIn(.02,[value for _,value,_ in bench.sent])
    def test_explicit_final_resume_does_not_repeat_previous_levels(self):
        clock=Clock(); bench=Bench(clock); starts=[]; gaps=[]
        result=finite_sweep(bench,lambda:None,clock,lambda i,v:starts.append((i,v)),
                            lambda step:None,gaps.append,start_index=2)
        self.assertEqual(starts,[(2,.02)])
        self.assertEqual(gaps,[])
        self.assertEqual(bench.stops,1)
        self.assertEqual(len(result),1)
    def test_baseline_waits_through_short_unstable_period(self):
        clock=Clock(); bench=Bench(clock)
        def health():
            s=state(clock)
            # Unreadable evidence until one second must not count.
            if clock.t<101:
                for field in s['fields']:field.update(valid=False,value=None,stable_value=None)
            return s
        snapshot=wait_stopped_baseline(bench,health,clock)
        self.assertGreaterEqual(clock.t,101.6)
        self.assertLess(clock.t,102)
        self.assertTrue(snapshot['fields'][0]['stable_value'] is not None)
        self.assertEqual(snapshot['fields'][0]['baseline_value'],'0')
        self.assertEqual(bench.sent,[])
    def test_baseline_tolerates_led_dropouts_but_not_a_changing_value(self):
        clock=Clock(); bench=Bench(clock)
        def health():
            s=state(clock,int(clock.t*20))
            # Reject every third frame like a partially refreshed LED digit.
            if int(clock.t*20)%3==0:
                for field in s['fields']:field.update(valid=False,value=None,stable_value=None)
            return s
        wait_stopped_baseline(bench,health,clock)
        self.assertLess(clock.t,101)
        clock=Clock(); bench=Bench(clock)
        def conflicting():
            s=state(clock,int(clock.t*20))
            if int(clock.t*20)%2:
                s['fields'][1].update(value='23.9')
            return s
        with self.assertRaises(RuntimeError):
            wait_stopped_baseline(bench,conflicting,clock)
        self.assertEqual(bench.sent,[])
    def test_baseline_timeout_does_not_send_output(self):
        clock=Clock(); bench=Bench(clock)
        def unreadable():
            s=state(clock)
            for field in s['fields']:field.update(valid=False,value=None,stable_value=None)
            return s
        with self.assertRaisesRegex(RuntimeError,'four seconds'):
            wait_stopped_baseline(bench,unreadable,clock)
        self.assertLess(clock.t,104.1)
        self.assertEqual(bench.sent,[])
    def test_guard_accepts_readable_changing_values(self):
        clock=Clock(); guard=CVGuard('test',clock)
        guard.check(state(clock),require_stable=True)
        clock.t+=.3
        guard.check(state(clock,seq=2,stable=False))
    def test_guard_rejects_old_run_stale_frame_and_stalled_frames(self):
        clock=Clock()
        for snapshot in (state(clock,run='old'),{**state(clock),'captured_monotonic_ns':1}):
            with self.assertRaises(RuntimeError): CVGuard('test',clock).check(snapshot)
        guard=CVGuard('test',clock); guard.check(state(clock))
        clock.t+=1.01
        with self.assertRaises(RuntimeError): guard.check(state(clock))
    def test_guard_times_out_unreadable_field_and_checks_baseline(self):
        clock=Clock(); guard=CVGuard('test',clock); guard.check(state(clock))
        bad=state(clock,seq=2); bad['fields'][0].update(valid=False,value=None,stable_value=None)
        with self.assertRaises(RuntimeError): guard.check(bad,require_stable=True)
        for i in range(3,10):
            clock.t+=.2
            bad=state(clock,seq=i); bad['fields'][0].update(valid=False,value=None)
            guard.check(bad)
        clock.t+=.2
        bad=state(clock,seq=10); bad['fields'][0].update(valid=False,value=None)
        with self.assertRaises(RuntimeError): guard.check(bad)
    def test_high_range_profile_is_bounded_and_monotonic(self):
        for target in (.065,.073,.10,.33,.60):
            for seconds in (2,15):
                profile=high_range_profile(target,seconds)
                self.assertEqual(profile[-1],(target,seconds))
                self.assertLessEqual(sum(s for _,s in profile),20+1e-9)
                self.assertTrue(all(b[0]>a[0] for a,b in zip(profile,profile[1:])))
                self.assertTrue(all(0<v<=.60 for v,_ in profile))
        ten=high_range_profile(.07,10)
        self.assertEqual(ten[-1],(.07,10))
        self.assertLessEqual(sum(seconds for _,seconds in ten),20)
        with self.assertRaises(ValueError): high_range_profile(.07,20)
        with self.assertRaises(ValueError): high_range_profile(.61,2)
    def test_finite_profile_rejects_unsafe_profiles(self):
        for profile in (((.1,1),(.1,1)),((.1,21),),((0,1),),((.1,0),)):
            with self.assertRaises(ValueError):
                finite_profile(None,None,None,profile)
    def test_extreme_range_profile_is_bounded_and_monotonic(self):
        for target in (.601,.735,.80,.90,1.00):
            profile=extreme_range_profile(target,10)
            self.assertEqual(profile[-1],(target,10))
            self.assertLessEqual(sum(s for _,s in profile),20+1e-9)
            self.assertTrue(all(b[0]>a[0] for a,b in zip(profile,profile[1:])))
            self.assertTrue(all(0<v<=1.0 for v,_ in profile))
            self.assertEqual(profile[0][0],.06)
        with self.assertRaises(ValueError): extreme_range_profile(.70,15)
        with self.assertRaises(ValueError): extreme_range_profile(.60,10)
        with self.assertRaises(ValueError): extreme_range_profile(.6501,10)
        with self.assertRaises(ValueError): extreme_range_profile(1.01,10)
    def test_residual_thrust_baseline_accepts_four_grams_and_rejects_seven(self):
        clock=Clock(); bench=Bench(clock)
        def health():
            s=state(clock,int(clock.t*20))
            s['fields'][0].update(value='0004',stable_value=None)
            return s
        snapshot=wait_stopped_baseline(bench,health,clock,allow_near_zero_thrust=True,
                                       near_zero_thrust_limit=2,max_upper_g=6)
        self.assertEqual(snapshot['fields'][0]['baseline_value'],'0004')
        self.assertEqual(snapshot['fields'][1]['baseline_value'],'0')
        self.assertEqual(bench.sent,[])
        clock=Clock(); bench=Bench(clock)
        def varying():
            s=state(clock,int(clock.t*20))
            s['fields'][0].update(value='1' if int(clock.t*20)%2 else '6',stable_value=None)
            return s
        wait_stopped_baseline(bench,varying,clock,allow_near_zero_thrust=True,
                              near_zero_thrust_limit=2,max_upper_g=6)
        self.assertEqual(bench.sent,[])
        clock=Clock(); bench=Bench(clock)
        def too_much():
            s=state(clock,int(clock.t*20))
            s['fields'][0].update(value='0007',stable_value=None)
            return s
        with self.assertRaises(RuntimeError):
            wait_stopped_baseline(bench,too_much,clock,allow_near_zero_thrust=True,
                                  near_zero_thrust_limit=2,max_upper_g=6)
        self.assertEqual(bench.sent,[])
    def test_setup_baseline_for_later_stage_accepts_residual_thrust(self):
        clock=Clock(); baseline=stopped_baseline_for_run(residual_limit=6)
        for i in range(80):
            snapshot=state(clock,i)
            snapshot['fields'][0].update(value='0003',stable_value=None)
            if baseline.update(snapshot,clock.t):break
            clock.t+=.1
        else:self.fail('residual setup baseline never confirmed a steady 3 g reading')
        self.assertGreaterEqual(clock.t,100.6)
        clock=Clock(); strict=stopped_baseline_for_run()
        for i in range(80):
            snapshot=state(clock,i)
            snapshot['fields'][0].update(value='0003',stable_value=None)
            self.assertFalse(strict.update(snapshot,clock.t))
            clock.t+=.1
    def test_setup_baseline_unlimited_accepts_any_stopped_thrust(self):
        # The operator removed the inter-stage residual limit: even a large,
        # fluctuating stopped thrust value must not block the setup baseline.
        clock=Clock(); baseline=stopped_baseline_for_run(residual_unlimited=True)
        for i in range(80):
            snapshot=state(clock,i)
            snapshot['fields'][0].update(value=('0050' if i%2 else '0250'),stable_value=None)
            if baseline.update(snapshot,clock.t):break
            clock.t+=.1
        else:self.fail('unlimited setup baseline never confirmed a large residual reading')
        self.assertGreaterEqual(clock.t,100.6)
        with self.assertRaises(ValueError):
            stopped_baseline_for_run(residual_limit=6,residual_unlimited=True)
    def test_guard_per_field_timeout_gives_only_the_led_thrust_field_slack(self):
        clock=Clock(); guard=CVGuard('test',clock); guard.check(state(clock))
        for i in range(2,14):
            clock.t+=.2
            snapshot=state(clock,seq=i)
            snapshot['fields'][0].update(valid=False,value=None)
            guard.check(snapshot,unreadable_seconds_by_field={'upper':2.5})
        clock.t+=.2
        snapshot=state(clock,seq=14)
        snapshot['fields'][0].update(valid=False,value=None)
        with self.assertRaisesRegex(RuntimeError,'unreadable for too long: upper'):
            guard.check(snapshot,unreadable_seconds_by_field={'upper':2.5})
        clock=Clock(); guard=CVGuard('test',clock); guard.check(state(clock))
        for i in range(2,9):
            clock.t+=.2
            snapshot=state(clock,seq=i)
            snapshot['fields'][1].update(valid=False,value=None)
            guard.check(snapshot,unreadable_seconds_by_field={'upper':2.5})
        clock.t+=.2
        snapshot=state(clock,seq=9)
        snapshot['fields'][1].update(valid=False,value=None)
        with self.assertRaisesRegex(RuntimeError,'unreadable for too long: voltage'):
            guard.check(snapshot,unreadable_seconds_by_field={'upper':2.5})
    def test_extreme_range_baseline_tolerates_stuck_three_gram_zero(self):
        clock=Clock(); snapshot=state(clock)
        for field in snapshot['fields']:
            if field['id']=='upper':
                field['value']='0003'; field['stable_value']=None
        with self.assertRaisesRegex(RuntimeError,'Baseline is not stable: upper'):
            CVGuard('test',clock).check(snapshot,require_stable=True,
                                        allow_near_zero_thrust=True)
        CVGuard('test',clock).check(snapshot,require_stable=True,
                                    allow_near_zero_thrust=True,near_zero_thrust_limit=5)
        for field in snapshot['fields']:
            if field['id']=='upper': field['value']='0006'
        with self.assertRaisesRegex(RuntimeError,'Baseline is not stable: upper'):
            CVGuard('test',clock).check(snapshot,require_stable=True,
                                        allow_near_zero_thrust=True,near_zero_thrust_limit=5)


if __name__=='__main__': unittest.main()
