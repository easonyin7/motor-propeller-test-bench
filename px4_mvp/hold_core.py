"""Finite lease renewal, independent of serial/camera libraries for fault tests."""
import math
import json
import time


HIGH_RANGE_VALUES=(.065,.07,.08,.09,.10,.12,.15,.18,.22,.27,.33,.40,.50,.60)


def _strict_profile(values,stage):
    """Round command values while keeping an eight-step rise strictly increasing."""
    profile=[];previous=None
    for value in values:
        rounded=round(float(value),4)
        if previous is not None and rounded<=previous:
            rounded=round(previous+.0001,4)
        profile.append((rounded,stage));previous=rounded
    return profile


def high_range_profile(target=.10,target_seconds=2):
    """Gentle geometric rise from 0.060 to a 0.061..0.60 target."""
    if (not math.isfinite(target) or not .06<target<=.60 or target_seconds not in (2,10,15)
            or round(target,3)!=target):
        raise ValueError('High-range target must be 0.061..0.60 with at most three decimals, for 2, 10 or 15 seconds.')
    start=.06
    intervals=8
    # A two-second probe still gets the full 18-second rise; measured holds
    # (10 or 15 seconds) keep a five-second rise so the total is at most 20.
    rise=18 if target_seconds==2 else 5
    values=[start*(target/start)**(i/intervals) for i in range(intervals)]+[target]
    stage=rise/intervals
    profile=tuple(_strict_profile(values[:-1],stage))+((target,target_seconds),)
    if any(b[0]<=a[0] for a,b in zip(profile,profile[1:])):
        raise ValueError('High-range profile must strictly increase.')
    return profile


EXTREME_RANGE_VALUES=(.70,.75,.80,.85,.90,1.00)


def extreme_range_profile(target=1.00,target_seconds=10):
    """Gentle ten-second geometric rise from 0.060 to a 0.601..1.00 target."""
    if (not math.isfinite(target) or not .60<target<=1.0 or target_seconds!=10
            or round(target,3)!=target):
        raise ValueError('Extreme-range target must be 0.601..1.00 with at most three decimals, for 10 seconds.')
    start=.06
    intervals=8
    rise=10
    values=[start*(target/start)**(i/intervals) for i in range(intervals)]+[target]
    stage=rise/intervals
    profile=tuple(_strict_profile(values[:-1],stage))+((target,target_seconds),)
    if any(b[0]<=a[0] for a,b in zip(profile,profile[1:])):
        raise ValueError('Extreme-range profile must strictly increase.')
    if sum(s for _,s in profile)>20+1e-9:
        raise ValueError('Extreme-range profile total must not exceed 20 seconds.')
    return profile


def read_snapshot(path,reader=None,sleep=time.sleep):
    """Retry only brief Windows sharing errors; never return a cached sample."""
    read = reader or (lambda: path.read_text(encoding='utf-8'))
    for attempt in range(6):
        try:
            return json.loads(read())
        except PermissionError:
            if attempt==5:
                raise
            sleep(.01)


def check_ui_heartbeat(path,clock=time.monotonic,max_age=2):
    """An optional GUI watchdog; only the GUI event loop refreshes this file."""
    state=read_snapshot(path)
    try:
        age=clock()-int(state['monotonic_ns'])/1e9
    except (KeyError,TypeError,ValueError):
        raise RuntimeError('Desktop UI heartbeat is invalid.')
    if not 0<=age<=max_age:
        raise RuntimeError('Desktop UI stopped responding; stop this run.')
    return age


def finite_hold(bench, health, clock, seconds=10.0, value=0.02):
    if not math.isfinite(seconds) or not .2 <= seconds <= 20:
        raise ValueError('Total duration must be 0.2..20 seconds.')
    if not math.isfinite(value) or not .01 <= value <= .04:
        raise ValueError('This trial restricts output to 0.01..0.04.')
    started = clock()
    deadline = started+seconds
    last_send = float('-inf')
    renewals = 0
    try:
        while clock() < deadline:
            bench.check()
            health()
            now = clock()
            remaining = deadline-now
            if remaining <= 0:
                break
            if now-last_send >= .35 and remaining >= .2:
                # Never lease output past the total deadline, even if the host
                # stalls or a final stop cannot reach the flight controller.
                last_send = now
                bench.command(value,min(1.0,remaining),min(.3,remaining))
                renewals += 1
            bench.receive()
    finally:
        stop_started = clock()
        bench.stop()
    return {'requested_seconds':seconds,'loop_elapsed_seconds':stop_started-started,
            'stop_ack_elapsed_seconds':clock()-stop_started,
            'renewals':renewals,'stop_commands_acknowledged':True,
            'started_monotonic_s':started,'deadline_monotonic_s':deadline}


def finite_sweep(bench,health,clock,before_step,after_step,rest,start_index=0):
    """Exactly three bounded runs; any error prevents all following steps."""
    if start_index not in (0,2):
        raise ValueError('Only a full sweep or explicit final-level completion is supported.')
    results=[]
    for index,value in enumerate((.01,.015,.02)):
        if index<start_index:
            continue
        before_step(index,value)
        control=finite_hold(bench,health,clock,seconds=10,value=value)
        step={'index':index,'value':value,'control':control}
        after_step(step)
        results.append(step)
        if index<2:
            rest(5)
    return results


def rise_profile(target=.03,target_seconds=2):
    if (not math.isfinite(target) or not .025<target<=.07 or target_seconds not in (2,10,15)
            or round(target,3)!=target):
        raise ValueError('Gentle rise target must be 0.026..0.070 with at most three decimals, for 2, 10 or 15 seconds.')
    prefixes={.03:((.025,2),(.0275,2)),
              .035:((.025,2),(.03,2)),
              .04:((.025,1.5),(.03,1.5),(.035,1.5)),
              .045:((.025,1),(.03,1),(.035,1),(.04,1)),
              .05:((.025,1),(.03,1),(.035,1),(.04,1),(.045,1)),
              .055:tuple((v,5/6) for v in (.025,.03,.035,.04,.045,.05)),
              .06:tuple((v,5/7) for v in (.025,.03,.035,.04,.045,.05,.055)),
              .065:tuple((v,5/8) for v in (.025,.03,.035,.04,.045,.05,.055,.06)),
              .07:tuple((v,5/9) for v in (.025,.03,.035,.04,.045,.05,.055,.06,.065))}
    if target in prefixes:
        return (*prefixes[target],(target,target_seconds))
    # Custom targets keep the same five-second rise budget. Rounding at four
    # decimals can collide for targets just above 0.025, so widen such steps.
    start=.025;intervals=8;rise=5
    values=[start*(target/start)**(i/intervals) for i in range(intervals)]
    return (*_strict_profile(values,rise/intervals),(target,target_seconds))


def finite_profile(bench,health,clock,profile):
    """One predeclared profile; all limits and health checks stay active."""
    if not profile or any(not math.isfinite(v) or not 0<v<=1 for v,_ in profile):
        raise ValueError('Profile values must be finite and within 0..1.')
    if any(not math.isfinite(s) or s<=0 for _,s in profile):
        raise ValueError('Profile stage durations must be positive.')
    if any(b[0]<=a[0] for a,b in zip(profile,profile[1:])):
        raise ValueError('Profile values must strictly increase.')
    if sum(s for _,s in profile)>20+1e-9:
        raise ValueError('Profile total duration must not exceed 20 seconds.')
    started=clock()
    stages=[]
    cursor=started
    for value,seconds in profile:
        stages.append({'value':value,'seconds':seconds,'started_monotonic_s':cursor,
                       'deadline_monotonic_s':cursor+seconds,'renewals':0})
        cursor+=seconds
    deadline=min(cursor,started+20)
    stages[-1]['deadline_monotonic_s']=deadline
    last_send=float('-inf'); last_index=None
    try:
        while clock()<deadline:
            bench.check(); health()
            now=clock()
            if now>=deadline: break
            index=next(i for i,s in enumerate(stages) if now<s['deadline_monotonic_s'])
            stage=stages[index]
            remaining=stage['deadline_monotonic_s']-now
            if (index!=last_index or now-last_send>=.35) and remaining>=.2:
                bench.command(stage['value'],min(1,remaining),min(.3,remaining))
                stage['renewals']+=1
                last_send=now; last_index=index
            bench.receive()
    finally:
        stop_started=clock()
        bench.stop()
    if any(s['renewals']==0 for s in stages):
        raise RuntimeError('A planned rise stage received no command; do not accept as complete.')
    return {'requested_seconds':deadline-started,'loop_elapsed_seconds':stop_started-started,
            'stop_ack_elapsed_seconds':clock()-stop_started,'stop_commands_acknowledged':True,
            'started_monotonic_s':started,'deadline_monotonic_s':deadline,
            'renewals':sum(s['renewals'] for s in stages),'profile_stages':stages,
            'target_window':{k:stages[-1][k] for k in ('value','seconds','started_monotonic_s','deadline_monotonic_s')}}


def finite_rise(bench,health,clock,target_seconds=2,target=.03):
    """One predeclared gentle rise; all limits and health checks stay active."""
    return finite_profile(bench,health,clock,rise_profile(target,target_seconds))


class StoppedBaseline:
    """Accept sustained, agreeing stopped readings despite brief CV dropouts.

    The LED panels refresh faster than the camera samples them, so a share of
    frames lose a segment and the reader correctly rejects them. Requiring one
    unbroken second of valid frames then never completes even while the meter
    sits steadily at 0000. Require several agreeing readings spanning at least
    half a second, with no long gap and a recent reading, instead of treating
    every rejected frame as proof that the value changed. A conflicting valid
    reading restarts confirmation; invalid frames are never evidence.
    """
    KEYS=('upper','voltage','current','power')

    def __init__(self,keys=KEYS,min_samples=4,span_seconds=.6,window_seconds=6.0,
                 max_gap_seconds=1.8,recent_seconds=.6,allow_near_zero_thrust=False,
                 near_zero_thrust_limit=2,max_upper_g=None):
        if min_samples<2:
            raise ValueError('Stopped baseline needs at least two agreeing readings.')
        if not all(math.isfinite(s) and s>0 for s in (span_seconds,window_seconds,
                                                      max_gap_seconds,recent_seconds)):
            raise ValueError('Stopped baseline windows must be positive.')
        absolute_limit = near_zero_thrust_limit if max_upper_g is None else max_upper_g
        # math.inf is the explicit "any residual thrust" sentinel used between
        # stages when the operator has removed the residual limit.
        if absolute_limit!=math.inf and (not math.isfinite(absolute_limit)
                or absolute_limit<near_zero_thrust_limit or absolute_limit>20):
            raise ValueError('Stopped upper limit must be between the agreement tolerance and 20 g, or unlimited.')
        self.keys=tuple(keys)
        self.min_samples=min_samples
        self.span_seconds=span_seconds
        self.window_seconds=window_seconds
        self.max_gap_seconds=max_gap_seconds
        self.recent_seconds=recent_seconds
        self.allow_near_zero_thrust=allow_near_zero_thrust
        self.near_zero_thrust_limit=near_zero_thrust_limit
        self.max_upper_g=absolute_limit
        self.samples={key:[] for key in self.keys}

    def reset(self):
        for history in self.samples.values():
            history.clear()

    def tolerance(self,key):
        # The meter flickers at rest; a small jump is not a new load. In residual
        # mode the whole allowed 1..6 g residual band is acceptable. Every other
        # field must agree exactly.
        return self.max_upper_g if key=='upper' else 0

    def reading(self,field,key):
        if not field.get('valid'):
            return None
        try:
            value=float(field.get('value'))
        except (TypeError,ValueError):
            return None
        if not math.isfinite(value):
            return None
        if key=='upper' and self.allow_near_zero_thrust and abs(value)>self.max_upper_g:
            return None
        return value

    def update(self,snapshot,now):
        fields={f['id']:f for f in snapshot.get('fields',[])}
        confirmed=True
        for key,history in self.samples.items():
            history[:]=[(time,value,text) for time,value,text in history
                        if now-time<=self.window_seconds]
            field=fields.get(key,{})
            value=self.reading(field,key)
            if value is not None:
                if history and abs(value-history[-1][1])>self.tolerance(key)+1e-9:
                    history.clear()
                history.append((now,value,str(field.get('value'))))
            if len(history)<self.min_samples:
                confirmed=False
                continue
            if now-history[-1][0]>self.recent_seconds:
                confirmed=False
                continue
            if history[-1][0]-history[0][0]<self.span_seconds:
                confirmed=False
                continue
            if any(later[0]-earlier[0]>self.max_gap_seconds
                   for earlier,later in zip(history,history[1:])):
                confirmed=False
        return bool(confirmed)

    @property
    def values(self):
        """Newest accepted raw text per field; never a fabricated stable value."""
        return {key:history[-1][2] for key,history in self.samples.items() if history}


def stopped_baseline_for_run(*,residual_limit=None,residual_unlimited=False,
                             near_zero_thrust_limit=2):
    """Build the setup baseline policy for one child invocation.

    The first stage of a session stays strict. Later stages and retries follow a
    motor run: they either allow a bounded residual (``residual_limit``) or, when
    the operator has removed the limit, accept any valid stopped thrust value
    (``residual_unlimited``). Electrical fields remain exact.

    The setup baseline always enforces its upper policy (unlike a pre-step wait,
    which is followed by an explicit check). Otherwise a low first stage could
    confirm against a steady 3 g display offset.
    """
    if residual_unlimited and residual_limit is not None:
        raise ValueError('Residual thrust cannot be both bounded and unlimited.')
    max_upper_g=(math.inf if residual_unlimited else
                 near_zero_thrust_limit if residual_limit is None else residual_limit)
    return StoppedBaseline(allow_near_zero_thrust=True,
                           near_zero_thrust_limit=near_zero_thrust_limit,
                           max_upper_g=max_upper_g)


def wait_stopped_baseline(bench,health,clock,allow_near_zero_thrust=False,timeout_seconds=4,
                          near_zero_thrust_limit=2,max_upper_g=None):
    """Bounded stopped wait; contested values restart, brief dropouts do not."""
    if not math.isfinite(timeout_seconds) or not 4 <= timeout_seconds <= 10:
        raise ValueError('Stopped baseline wait must be 4..10 seconds.')
    deadline=clock()+timeout_seconds
    baseline=StoppedBaseline(allow_near_zero_thrust=allow_near_zero_thrust,
                             near_zero_thrust_limit=near_zero_thrust_limit,
                             max_upper_g=max_upper_g)
    while clock()<deadline:
        bench.receive()
        bench.verify_zero()
        snapshot=health()
        if baseline.update(snapshot,clock()):
            values=baseline.values
            # Annotate in place so the caller's current snapshot carries the
            # confirmed readings; never overwrite the reader's stable_value.
            for field in snapshot.get('fields',[]):
                field['baseline_value']=values.get(field['id'])
            return snapshot
    duration='four' if timeout_seconds==4 else f'{timeout_seconds:g}'
    raise RuntimeError(f'Stopped CV baseline did not become stable within {duration} seconds.')


def baseline_field_ready(field,key,allow_near_zero_thrust=False,near_zero_thrust_limit=2,
                         max_upper_g=None):
    if not field.get('valid'):
        return False
    if key=='upper' and allow_near_zero_thrust:
        absolute_limit = near_zero_thrust_limit if max_upper_g is None else max_upper_g
        try:
            value=float(field.get('value'))
            return math.isfinite(value) and abs(value)<=absolute_limit
        except (ValueError,TypeError):
            return False
    return field.get('stable_value') is not None


class LoadResponseGuard:
    """Do not renew a long run indefinitely when output has no measured load."""
    def __init__(self,started,baseline):
        self.started=started
        self.baseline={f['id']:f for f in baseline['fields']}
        self.samples=set()

    def check(self,state,now):
        fields={f['id']:f for f in state['fields']}
        responded=True
        for key,delta in [('current',.02),('power',.5)]:
            field=fields.get(key,{})
            try:
                value=float(field.get('value'))
                base=float(self.baseline[key]['value'])
                responded &= bool(field.get('valid')) and math.isfinite(value) and value>base+delta
            except (ValueError,TypeError,KeyError):
                responded=False
        if responded:
            self.samples.add(state['samples'])
        if now-self.started>3 and len(self.samples)<2:
            raise RuntimeError('No repeated current/power load response within three seconds; check motor/ESC connection. No automatic retry.')
        return len(self.samples)


class CVGuard:
    """Require this run's advancing, fresh frames; permit readable changing values."""
    REQUIRED = {'upper':'g','voltage':'V','current':'A','power':'W'}

    def __init__(self,run_id,clock):
        self.run_id = run_id
        self.clock = clock
        self.last_valid = {}
        self.last_seq = None
        self.last_frame_seq = None
        self.last_advance = None

    def check(self,state,require_stable=False,allow_near_zero_thrust=False,
              near_zero_thrust_limit=2,unreadable_seconds=1.5,max_upper_g=None,
              unreadable_seconds_by_field=None):
        now = self.clock()
        if state.get('state')!='running' or state.get('run_id')!=self.run_id:
            raise RuntimeError('CV session is not this running trial.')
        age = now-state.get('captured_monotonic_ns',0)/1e9
        if not 0 <= age <= 1.0:
            raise RuntimeError(f'CV frame age {age:.3f}s is outside 0..1s.')
        seq,frame_seq = state.get('samples'),state.get('frame_seq')
        if not isinstance(seq,int) or not isinstance(frame_seq,int):
            raise RuntimeError('Missing CV sample/frame sequence.')
        advanced = seq!=self.last_seq
        if advanced:
            if self.last_seq is not None and (seq<=self.last_seq or frame_seq<=self.last_frame_seq):
                raise RuntimeError('CV sample/frame sequence did not advance.')
            self.last_seq,self.last_frame_seq = seq,frame_seq
            self.last_advance = now
        if self.last_advance is None or now-self.last_advance>1:
            raise RuntimeError('CV publishing has stalled.')
        fields = {f['id']:f for f in state.get('fields',[])}
        for key,unit in self.REQUIRED.items():
            field = fields.get(key,{})
            if field.get('unit')!=unit:
                raise RuntimeError('CV field/unit missing or changed: '+key)
            if field.get('valid') and field.get('value') is not None:
                if advanced:
                    self.last_valid[key] = now
            if require_stable and not baseline_field_ready(field,key,allow_near_zero_thrust,
                                                           near_zero_thrust_limit,max_upper_g):
                raise RuntimeError('Baseline is not stable: '+key)
            field_timeout=(unreadable_seconds_by_field or {}).get(key,unreadable_seconds)
            if key not in self.last_valid or now-self.last_valid[key]>field_timeout:
                raise RuntimeError('CV field unreadable for too long: '+key)
        return state
