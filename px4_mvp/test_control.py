"""Offline fault checks; never opens a serial port."""
import contextlib
import io
import time
from types import SimpleNamespace
import unittest

from px4_control_mvp import BenchLink, validate_pulse, mavutil


class FakeBench(BenchLink):
    def __init__(self, fail_ack=False, fail_receive=False, armed=False):
        self.last_heartbeat = SimpleNamespace(base_mode=128 if armed else 0)
        self.heartbeat_time = time.monotonic()
        self.commands = []
        self.events = []
        self.fail_ack = fail_ack
        self.fail_receive = fail_receive

    def record(self, event, detail):
        self.events.append((event, detail))

    def send_command(self, command, params):
        self.commands.append((command, params))

    def wait_ack(self, command, timeout=0.7):
        if self.fail_ack:
            self.fail_ack = False
            raise RuntimeError('ACK missing')

    def receive(self):
        if self.fail_receive:
            raise OSError('USB disconnected')
        time.sleep(0.005)


class FaultChecks(unittest.TestCase):
    def test_rejects_nonfinite_or_excess_output(self):
        for value in [float('nan'), float('inf'), -0.01, 0.11]:
            with self.assertRaises(ValueError):
                validate_pulse(value, 1)
        for duration in [float('nan'), 0, 3.01]:
            with self.assertRaises(ValueError):
                validate_pulse(0.05, duration)

    def test_high_range_pulse_requires_explicit_ceiling(self):
        validate_pulse(0.60, 1, 0.60)
        with self.assertRaises(ValueError):
            validate_pulse(0.61, 1, 0.60)
        with self.assertRaises(ValueError):
            validate_pulse(0.11, 1)
    def test_extreme_range_pulse_requires_full_ceiling(self):
        validate_pulse(1.00, 1, 1.0)
        with self.assertRaises(ValueError):
            validate_pulse(1.01, 1, 1.0)
        with self.assertRaises(ValueError):
            validate_pulse(0.71, 1, 0.60)
    def test_armed_and_stale_state_send_no_motor_commands(self):
        for bench in [FakeBench(armed=True), FakeBench()]:
            if not bench.last_heartbeat.base_mode:
                bench.heartbeat_time -= 10
            with self.assertRaises(RuntimeError):
                bench.pulse(1, 0.05, 0.2)
            self.assertEqual(bench.commands, [])

    def test_missing_ack_attempts_stop_without_repeating_output(self):
        bench = FakeBench(fail_ack=True)
        with self.assertRaisesRegex(RuntimeError, 'ACK missing'):
            bench.pulse(1, 0.05, 0.2)
        positive = [p for _, p in bench.commands if p[0] == 0.05]
        self.assertEqual(len(positive), 1)
        self.assertNotEqual(bench.commands[1][1][0], bench.commands[1][1][0])
        self.assertEqual(bench.commands[-1][1][1], 0)

    def test_usb_loss_reports_unconfirmed_stop_and_no_restart(self):
        bench = FakeBench(fail_receive=True)
        with contextlib.redirect_stderr(io.StringIO()) as output:
            with self.assertRaises(OSError):
                bench.pulse(1, 0.05, 0.2)
        self.assertIn('STOP UNCONFIRMED', output.getvalue())
        self.assertEqual(len([p for _, p in bench.commands if p[0] == 0.05]), 1)

    def test_nan_stop_survives_mavlink_wire_encoding(self):
        class Sink:
            def __init__(self):
                self.data = b''
            def write(self, data):
                self.data += data
        sink = Sink()
        sender = mavutil.mavlink.MAVLink(sink, srcSystem=255, srcComponent=190)
        sender.command_long_send(1, 1, mavutil.mavlink.MAV_CMD_ACTUATOR_TEST,
                                 0, float('nan'), 1, 0, 0, 1, 0, 0)
        receiver = mavutil.mavlink.MAVLink(None)
        decoded = receiver.parse_buffer(sink.data)[0]
        self.assertNotEqual(decoded.param1, decoded.param1)
        self.assertEqual(decoded.param5, 1)


if __name__ == '__main__':
    unittest.main()
