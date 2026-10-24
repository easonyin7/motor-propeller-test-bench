"""USB/PX4 bench MVP: probe by default; explicit, bounded motor pulses.

No parameter writes, arming, firmware updates, or automatic restart.
"""
import argparse
import csv
import json
import math
import os
from pathlib import Path
import sys
import struct
import time

HERE = Path(__file__).resolve().parent
if (HERE / '.deps').is_dir():
    sys.path.insert(0, str(HERE / '.deps'))
os.environ['MAVLINK20'] = '1'
from pymavlink import mavutil
from serial.tools import list_ports


class BenchLink:
    def __init__(self, port, baud, log_path):
        self.link = mavutil.mavlink_connection(
            port, baud=baud, dialect='common', source_system=255,
            source_component=190)
        self.target = None
        self.last_heartbeat = None
        self.heartbeat_time = 0.0
        self.next_gcs_heartbeat = 0.0
        self.parameters = {}
        self.log_file = open(log_path, 'w', newline='', encoding='utf-8-sig')
        self.log = csv.writer(self.log_file)
        self.log.writerow(['utc_time', 'monotonic_s', 'event', 'detail'])

    def record(self, event, detail):
        self.log.writerow([time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                           f'{time.monotonic():.6f}', event,
                           json.dumps(detail, ensure_ascii=False)])
        self.log_file.flush()

    def receive(self):
        now = time.monotonic()
        if now >= self.next_gcs_heartbeat:
            self.link.mav.heartbeat_send(mavutil.mavlink.MAV_TYPE_GCS,
                                        mavutil.mavlink.MAV_AUTOPILOT_INVALID,
                                        0, 0, mavutil.mavlink.MAV_STATE_ACTIVE)
            self.next_gcs_heartbeat = now + 1.0
        msg = self.link.recv_match(blocking=True, timeout=0.05)
        if msg is None or msg.get_type() == 'BAD_DATA':
            return None
        source = (msg.get_srcSystem(), msg.get_srcComponent())
        if msg.get_type() == 'HEARTBEAT' and msg.autopilot == mavutil.mavlink.MAV_AUTOPILOT_PX4:
            if self.target is None:
                self.target = source
            if source == self.target:
                self.last_heartbeat = msg
                self.heartbeat_time = time.monotonic()
        if source == self.target:
            if msg.get_type() == 'PARAM_VALUE':
                key = msg.param_id
                if isinstance(key, bytes):
                    key = key.decode('ascii', errors='replace')
                self.parameters[key.rstrip('\x00')] = (msg.param_value, msg.param_type)
            if msg.get_type() in ('HEARTBEAT', 'COMMAND_ACK', 'AUTOPILOT_VERSION', 'STATUSTEXT', 'SERVO_OUTPUT_RAW'):
                self.record('rx', msg.to_dict())
            return msg
        return None

    def connect(self, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.receive()
            if self.target is not None:
                return
        raise RuntimeError('COM port opened, but no PX4 heartbeat within 10 s. '
                           'Check firmware/boot state, USB MAVLink and port ownership.')

    def ensure_disarmed(self):
        if self.last_heartbeat is None or time.monotonic() - self.heartbeat_time > 2.5:
            raise RuntimeError('PX4 heartbeat is stale; control refused.')
        if self.last_heartbeat.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED:
            raise RuntimeError('PX4 is armed; bench actuator test refused.')

    def send_command(self, command, params):
        self.link.mav.command_long_send(*self.target, command, 0, *params)
        self.record('tx_command', {'command': command,
                                 'params': ['NaN' if math.isnan(float(p)) else p for p in params]})

    def wait_ack(self, command, timeout=0.7):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = self.receive()
            if msg is not None and msg.get_type() == 'COMMAND_ACK' and msg.command == command:
                if msg.result != mavutil.mavlink.MAV_RESULT_ACCEPTED:
                    enum = mavutil.mavlink.enums['MAV_RESULT'].get(msg.result)
                    raise RuntimeError('Command rejected: ' + (enum.name if enum else str(msg.result)))
                return
        raise RuntimeError('No command ACK. Execution state is unknown; do not retry a running pulse.')

    def read_parameters(self, names, bytewise):
        for name in names:
            self.link.mav.param_request_read_send(*self.target, name.encode('ascii'), -1)
            self.receive()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and any(n not in self.parameters for n in names):
            self.receive()
        result = {}
        formats = {1: '<B', 2: '<b', 3: '<H', 4: '<h', 5: '<I', 6: '<i'}
        for name in names:
            if name not in self.parameters:
                result[name] = None
                continue
            value, kind = self.parameters[name]
            if bytewise and kind in formats:
                raw = struct.pack('<f', value)
                fmt = formats[kind]
                value = struct.unpack(fmt, raw[:struct.calcsize(fmt)])[0]
            result[name] = value
        self.record('parameters_read', result)
        return result

    def probe(self, seconds=3, parameters=False):
        self.send_command(mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE,
                          [mavutil.mavlink.MAVLINK_MSG_ID_AUTOPILOT_VERSION, 0, 0, 0, 0, 0, 0])
        version = None
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            msg = self.receive()
            if msg is not None and msg.get_type() == 'AUTOPILOT_VERSION':
                version = msg.to_dict()
        result = {'port': self.link.address, 'system_id': self.target[0],
                  'component_id': self.target[1],
                  'heartbeat_age_s': round(time.monotonic() - self.heartbeat_time, 3),
                  'armed': bool(self.last_heartbeat.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED),
                  'heartbeat': self.last_heartbeat.to_dict(), 'autopilot_version': version}
        if version:
            n = version['flight_sw_version']
            result['firmware_version'] = f'{n >> 24}.{(n >> 16) & 255}.{(n >> 8) & 255}'
        if parameters:
            if not version:
                raise RuntimeError('No version/capability response; parameter encoding cannot be determined.')
            bytewise = bool(version['capabilities'] & mavutil.mavlink.MAV_PROTOCOL_CAPABILITY_PARAM_ENCODE_BYTEWISE)
            names = ['COM_MOT_TEST_EN', 'DSHOT_MIN'] + [f'PWM_MAIN_TIM{i}' for i in range(4)] + [f'PWM_{bus}_FUNC{i}' for bus in ('MAIN', 'AUX') for i in range(1, 9)]
            result['output_parameters'] = self.read_parameters(names, bytewise)
            limits = []
            for name, function in result['output_parameters'].items():
                if '_FUNC' in name and function is not None and 101 <= function <= 112:
                    prefix, index = name.split('_FUNC')
                    limits += [f'{prefix}_{kind}{index}' for kind in ('DIS', 'MIN', 'MAX')]
            if limits:
                result['output_limits'] = self.read_parameters(limits, bytewise)
        return result

    def stop(self, motor):
        command = mavutil.mavlink.MAV_CMD_ACTUATOR_TEST
        # NaN requests the disarmed output; zero is NOT a stop value.
        self.send_command(command, [float('nan'), 1.0, 0, 0, motor, 0, 0])
        self.wait_ack(command)
        # Allow the NaN request to be applied before releasing test control.
        deadline = time.monotonic() + 0.15
        while time.monotonic() < deadline:
            self.receive()
        self.send_command(command, [float('nan'), 0, 0, 0, motor, 0, 0])
        self.wait_ack(command)

    def observe_output(self, seconds):
        outputs = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            msg = self.receive()
            if msg is not None and msg.get_type() == 'SERVO_OUTPUT_RAW':
                outputs.append(msg.to_dict())
        return outputs

    def enable_output_observation(self):
        command = mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL
        self.send_command(command, [mavutil.mavlink.MAVLINK_MSG_ID_SERVO_OUTPUT_RAW,
                                    100000, 0, 0, 0, 0, 0])
        self.wait_ack(command)

    def pulse(self, motor, value, seconds):
        self.ensure_disarmed()
        command = mavutil.mavlink.MAV_CMD_ACTUATOR_TEST
        started = time.monotonic()
        try:
            # One finite command: never renew a pulse in the background.
            self.send_command(command, [value, seconds, 0, 0, motor, 0, 0])
            self.wait_ack(command)
            while time.monotonic() - started < seconds:
                self.receive()
                self.ensure_disarmed()
        finally:
            try:
                self.stop(motor)
            except Exception as error:
                self.record('stop_unconfirmed', str(error))
                print('STOP UNCONFIRMED:', error, file=sys.stderr)
                print('Stop request could not be confirmed. Use independent power cutoff; '
                      'verify timeout behavior on the bench.', file=sys.stderr)
                raise

    def close(self):
        try:
            self.link.close()
        finally:
            self.log_file.close()


def validate_pulse(value, seconds, max_value=0.10):
    if not math.isfinite(value) or not 0 <= value <= max_value:
        raise ValueError(f'Normalized output is restricted to 0..{max_value:g}; this is not RPM or measured thrust.')
    if not math.isfinite(seconds) or not 0.2 <= seconds <= 3.0:
        raise ValueError('Pulse duration must be 0.2..3.0 seconds.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', default='probe', choices=['ports', 'probe', 'pulse', 'stop'])
    parser.add_argument('--port', default='COM42')
    parser.add_argument('--baud', type=int, default=115200)
    parser.add_argument('--motor', type=int, default=1, choices=range(1, 13),
                        help='MAVLink logical Motor N, not connector pin or PX4 parameter value')
    parser.add_argument('--value', type=float, default=0.0)
    parser.add_argument('--seconds', type=float, default=1.0)
    parser.add_argument('--bench-ready', action='store_true',
                        help='Operator has removed props, secured motor, verified output mapping and power cutoff')
    parser.add_argument('--parameters', action='store_true', help='Probe also reads output mapping and limits')
    parser.add_argument('--observe-output', action='store_true', help='Pulse requests 10 Hz reported raw output and logs it; values are protocol-dependent')
    parser.add_argument('--no-motor-connected', action='store_true', help='Operator confirms motor is physically disconnected for output-only checks')
    args = parser.parse_args()
    if args.action == 'ports':
        print(json.dumps([{'port': p.device, 'description': p.description,
                           'vid': p.vid, 'pid': p.pid} for p in list_ports.comports()], ensure_ascii=False, indent=2))
        return 0
    if args.action == 'pulse':
        if not (args.bench_ready or args.no_motor_connected):
            parser.error('pulse requires --bench-ready, or --no-motor-connected for output-only checks')
        try:
            validate_pulse(args.value, args.seconds)
        except ValueError as error:
            parser.error(str(error))
    logs = HERE / 'logs'
    logs.mkdir(exist_ok=True)
    name = time.strftime('%Y%m%d_%H%M%S') + f'_{time.time_ns() % 1000000:06d}'
    link = None
    try:
        link = BenchLink(args.port, args.baud, logs / f'{name}.csv')
        link.connect()
        if args.action == 'probe':
            result = link.probe(parameters=args.parameters)
            (logs / f'{name}.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.action == 'pulse':
            before = None
            if args.observe_output:
                link.enable_output_observation()
                before = link.observe_output(0.5)
            link.pulse(args.motor, args.value, args.seconds)
            if args.observe_output:
                after = link.observe_output(0.5)
                print(json.dumps({'reported_output_before': before,
                                  'reported_output_after': after}, indent=2))
            print('Pulse finished; stop commands acknowledged. Verify actual motor stop independently.')
        else:
            link.ensure_disarmed()
            link.stop(args.motor)
            print('Stop commands acknowledged. Verify actual motor stop independently.')
        return 0
    except (Exception, KeyboardInterrupt) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        if link:
            link.record('error', str(error))
        return 1
    finally:
        if link:
            link.close()
        print('Log:', logs / f'{name}.csv')


if __name__ == '__main__':
    raise SystemExit(main())
