import importlib.util
import os
import signal
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_with_heartbeat.py"


class HeartbeatTests(unittest.TestCase):
    def run_child(self, code, interval="0.01"):
        return subprocess.run([sys.executable, str(SCRIPT), "--interval", interval,
                               "--", sys.executable, "-u", "-c", code],
                              capture_output=True, text=True, timeout=10)

    def load_wrapper(self):
        spec = importlib.util.spec_from_file_location("heartbeat_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_success_and_both_child_output_streams_are_preserved(self):
        result = self.run_child("import sys; print('child-out'); print('child-err', file=sys.stderr)")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("child-out", result.stdout)
        self.assertIn("child-err", result.stderr)

    def test_child_exit_seven_is_propagated(self):
        result = self.run_child("import sys; sys.exit(7)")
        self.assertEqual(result.returncode, 7, result.stderr)

    def test_sleeping_child_emits_alive_elapsed_heartbeat(self):
        result = self.run_child("import time; time.sleep(0.12)")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"heartbeat: child pid=\d+ alive elapsed=\d+\.\d+s")
        self.assertNotIn("complete", result.stdout)

    def test_invalid_intervals_rejected_before_launch(self):
        for interval in ("0", "-1", "nan", "inf"):
            with self.subTest(interval=interval):
                result = self.run_child("print('SHOULD_NOT_LAUNCH')", interval)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("SHOULD_NOT_LAUNCH", result.stdout)

    def test_interrupt_terminates_and_reaps_child(self):
        module = self.load_wrapper()
        child = mock.Mock()
        child.wait.side_effect = [KeyboardInterrupt(), 0]
        child.poll.return_value = None
        with mock.patch.object(module.subprocess, "Popen", return_value=child):
            self.assertEqual(module.run_command(["child"], 0.01), 130)
        child.terminate.assert_called_once()
        self.assertEqual(child.wait.call_count, 2)

    def test_cleanup_kills_child_that_ignores_termination(self):
        module = self.load_wrapper()
        child = mock.Mock()
        child.wait.side_effect = [KeyboardInterrupt(), subprocess.TimeoutExpired("child", 5), 0]
        child.poll.return_value = None
        with mock.patch.object(module.subprocess, "Popen", return_value=child):
            self.assertEqual(module.run_command(["child"], 0.01), 130)
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertEqual(child.wait.call_count, 3)

    def test_sigterm_handler_restored_after_child_cleanup(self):
        module = self.load_wrapper()
        previous = signal.getsignal(signal.SIGTERM)
        child = mock.Mock()
        def interrupted_wait(timeout):
            handler = signal.getsignal(signal.SIGTERM)
            handler(signal.SIGTERM, None)
        calls = 0
        def wait(timeout=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                interrupted_wait(timeout)
            return 0
        child.wait.side_effect = wait
        child.poll.return_value = None
        with mock.patch.object(module.subprocess, "Popen", return_value=child):
            self.assertEqual(module.run_command(["child"], 0.01), 128 + signal.SIGTERM)
        child.terminate.assert_called_once()
        self.assertEqual(calls, 2)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_unexpected_wait_error_is_not_swallowed_and_child_is_reaped(self):
        module = self.load_wrapper()
        child = mock.Mock()
        child.wait.side_effect = [RuntimeError("wait failed"), 0]
        child.poll.return_value = None
        with mock.patch.object(module.subprocess, "Popen", return_value=child):
            with self.assertRaisesRegex(RuntimeError, "wait failed"):
                module.run_command(["child"], 0.01)
        child.terminate.assert_called_once()
        self.assertEqual(child.wait.call_count, 2)

    @unittest.skipIf(os.name == "nt", "POSIX signals")
    def test_child_signal_exit_maps_to_shell_signal_status(self):
        result = self.run_child("import os, signal; os.kill(os.getpid(), signal.SIGTERM)")
        self.assertEqual(result.returncode, 128 + signal.SIGTERM)

    @unittest.skipIf(os.name == "nt", "POSIX signals")
    def test_sigterm_on_wrapper_terminates_and_reaps_child(self):
        wrapper = subprocess.Popen([sys.executable, "-u", str(SCRIPT), "--interval", "0.01", "--",
                                    sys.executable, "-u", "-c", "import time; time.sleep(30)"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            heartbeat = wrapper.stdout.readline()
            self.assertIn("alive", heartbeat)
            pid = int(heartbeat.split("pid=")[1].split()[0])
            wrapper.send_signal(signal.SIGTERM)
            wrapper.communicate(timeout=10)
            self.assertEqual(wrapper.returncode, 128 + signal.SIGTERM)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            if wrapper.poll() is None:
                wrapper.kill()
                wrapper.communicate()


if __name__ == "__main__":
    unittest.main()
