"""Finite no-prop bench sequence: low outputs, repeatability, command expiry.

Requires the same secured, powered bench and independent cutoff as the pulse tool.
Does not simulate USB disconnection or prove physical RPM/stop from telemetry.
"""
import argparse
import json
import sys
import time

from px4_control_mvp import BenchLink, HERE, mavutil, validate_pulse


class ObservedBench(BenchLink):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.samples = []

    def receive(self):
        msg = super().receive()
        if msg is not None and msg.get_type() == 'SERVO_OUTPUT_RAW':
            self.samples.append({'t': time.monotonic(), 'port': msg.port,
                                 'outputs': [getattr(msg, f'servo{i}_raw', 0) for i in range(1, 17)]})
        return msg

    def watch(self, seconds):
        start = len(self.samples)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.receive()
            self.ensure_disarmed()
        return self.samples[start:]


def verify_rest(samples):
    relevant = [s for s in samples if s['port'] == 0]
    if len(relevant) < 3:
        raise RuntimeError('Insufficient output telemetry; sequence aborted.')
    if any(any(s['outputs']) for s in relevant[-3:]):
        raise RuntimeError('Latest output samples are not all zero; sequence aborted.')


def verify_step(samples, start):
    relevant = [s for s in samples if s['port'] == 0 and s['t'] >= start]
    if any(any(s['outputs'][1:]) for s in relevant):
        raise RuntimeError('Unexpected output on a channel other than Motor 1; sequence aborted.')
    active = [s for s in relevant if s['outputs'][0] > 0]
    if not active:
        raise RuntimeError('No active Motor 1 output observed; sequence aborted.')
    stopped = [s for s in relevant if s['t'] > active[-1]['t'] and s['outputs'][0] == 0]
    if len(stopped) < 3:
        raise RuntimeError('Insufficient stopped output samples after pulse; sequence aborted.')
    return {'raw_output_values': sorted({s['outputs'][0] for s in active}),
            'first_active_s': round(active[0]['t'] - start, 3),
            'first_zero_after_active_s': round(stopped[0]['t'] - start, 3),
            'other_channels_zero': True, 'reported_output_returned_zero': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', default='COM42')
    parser.add_argument('--bench-ready', action='store_true')
    args = parser.parse_args()
    if not args.bench_ready:
        parser.error('requires --bench-ready after physical bench checks')
    logs = HERE / 'logs'
    logs.mkdir(exist_ok=True)
    name = time.strftime('%Y%m%d_%H%M%S') + '_sequence'
    report = {'port': args.port, 'steps': [], 'status': 'incomplete',
              'physical_observation': 'pending operator feedback',
              'scope': 'No-prop single motor; command-expiry test keeps USB and heartbeats active.'}
    bench = None
    attempted = False
    try:
        bench = ObservedBench(args.port, 115200, logs / f'{name}.csv')
        bench.connect()
        report['preflight'] = bench.probe(parameters=True)
        parameters = report['preflight']['output_parameters']
        if parameters.get('PWM_MAIN_FUNC1') != 101 or parameters.get('COM_MOT_TEST_EN') != 1:
            raise RuntimeError('Motor 1 mapping or test enable changed; sequence aborted.')
        if parameters.get('PWM_MAIN_TIM0') != -4:
            raise RuntimeError('Output protocol differs from the verified DShot300 setup; sequence aborted.')
        bench.ensure_disarmed()
        bench.enable_output_observation()
        verify_rest(bench.watch(0.6))

        for label, value in [('repeat_1', 0.02), ('repeat_2', 0.02),
                             ('level_003', 0.03), ('level_004', 0.04)]:
            validate_pulse(value, 1)
            print(f'{label}: Motor 1 value={value}, duration=1 s', flush=True)
            start = time.monotonic()
            bench.record('step_start', {'label': label, 'value': value, 'duration': 1})
            attempted = True
            bench.pulse(1, value, 1)
            verify_rest(bench.watch(0.7))
            result = {'label': label, 'value': value, 'duration_s': 1,
                      **verify_step(bench.samples, start)}
            report['steps'].append(result)
            bench.record('step_result', result)
            print(json.dumps(result), flush=True)
            verify_rest(bench.watch(1.3))

        # Deliberately do not renew or send stop during the observation window.
        # The command itself has a finite 1 s lifetime. Cleanup follows observation.
        print('command_expiry: Motor 1 value=0.02, timeout=1 s; observe for 2 s', flush=True)
        validate_pulse(0.02, 1)
        bench.ensure_disarmed()
        start = time.monotonic()
        bench.record('step_start', {'label': 'command_expiry', 'value': 0.02,
                                    'timeout': 1, 'observe_s': 2})
        try:
            bench.send_command(mavutil.mavlink.MAV_CMD_ACTUATOR_TEST, [0.02, 1, 0, 0, 1, 0, 0])
            bench.wait_ack(mavutil.mavlink.MAV_CMD_ACTUATOR_TEST)
            verify_rest(bench.watch(2.0))
            result = {'label': 'command_expiry', 'value': 0.02, 'timeout_s': 1,
                      'no_stop_command_before_observed_zero': True,
                      **verify_step(bench.samples, start)}
            report['steps'].append(result)
            bench.record('expiry_observed_before_cleanup', result)
            print(json.dumps(result), flush=True)
        finally:
            bench.stop(1)
        verify_rest(bench.watch(0.5))
        report['status'] = 'telemetry_checks_passed'
        return 0
    except (Exception, KeyboardInterrupt) as error:
        report['status'] = 'aborted'
        report['error'] = str(error)
        print('ABORTED:', error, file=sys.stderr, flush=True)
        if bench and attempted:
            try:
                bench.stop(1)
                bench.watch(0.5)
            except BaseException as stop_error:
                report['stop_unconfirmed'] = str(stop_error)
                print('STOP UNCONFIRMED: use independent power cutoff.', file=sys.stderr, flush=True)
        return 1
    finally:
        if bench:
            bench.close()
        report_path = logs / f'{name}.json'
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('Report:', report_path, flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
