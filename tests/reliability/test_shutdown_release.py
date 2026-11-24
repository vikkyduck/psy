"""Offline regressions for the actual safe shutdown tail and process-exit guard."""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import threading
import types
import unittest


ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT / "gateway/hermes_cli"
TARGET = Path(os.environ["PSY_TEST_GATEWAY"]).resolve()
spec = importlib.util.spec_from_file_location("psy_gateway_exit", HERE / "psy_gateway_exit.py")
guard_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard_module)
Guard = guard_module.GatewayProcessExitGuard


def extracted_tail(verdict=True):
    """Execute the deployed function itself without importing the full gateway."""
    tree = ast.parse((TARGET / "gateway/run.py").read_text())
    function = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == "_start_gateway_shutdown_tail")
    prepare = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                   and n.name == "_prepare_gateway_process_exit")
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, prepare, function], type_ignores=[]))
    parent = sys.modules.setdefault("hermes_cli", types.ModuleType("hermes_cli"))
    sys.modules["hermes_cli.psy_gateway_exit"] = guard_module
    keepalive = types.ModuleType("hermes_cli.nous_auth_keepalive")
    keepalive.stop_nous_auth_keepalive = lambda: None
    sys.modules["hermes_cli.nous_auth_keepalive"] = keepalive

    async def no_op(*args, **kwargs):
        return True

    def resolve(*args):
        if isinstance(verdict, BaseException):
            raise verdict
        return verdict

    namespace = dict(
        asyncio=asyncio, threading=threading, logger=logging.getLogger("shutdown-test"),
        _best_effort=lambda fn: fn(), _stop_cron_provider=lambda provider: None,
        _await_thread_exit=no_op, _shutdown_mcp_servers_nonblocking=no_op,
        _resolve_gateway_exit_verdict=resolve,
        _CRON_SHUTDOWN_DRAIN_TIMEOUT=1, _HOUSEKEEPING_SHUTDOWN_DRAIN_TIMEOUT=1,
    )
    exec(compile(module, "actual_gateway_shutdown_tail", "exec"), namespace)
    return namespace[function.name]


async def call_tail(tail, stop_task, control=None):
    runner = types.SimpleNamespace(_stop_task=stop_task, config=None)
    return await tail(runner, control, threading.Event(), None, None, None,
                      threading.Event(), types.SimpleNamespace(join=lambda **kwargs: None), [False])


class ShutdownRegression(unittest.TestCase):
    def test_exit_guard_is_not_armed_by_elapsed_lecture_time(self):
        calls = []
        with Guard(calls.append, timeout=.02, emergency_grace=.02, hard_exit=calls.append):
            threading.Event().wait(.08)
        self.assertEqual(calls, [])

    def test_embedded_gateway_tail_never_installs_a_process_deadline(self):
        async def example():
            done = asyncio.create_task(asyncio.sleep(0))
            return await call_tail(extracted_tail(), done)
        self.assertTrue(asyncio.run(example()))

    def test_context_closure_cancels_the_finalization_timer(self):
        calls = []
        with Guard(calls.append, timeout=.08, emergency_grace=.03, hard_exit=calls.append):
            guard_module.arm_after_durable_shutdown(17)
        threading.Event().wait(.12)
        self.assertEqual(calls, [])

    def test_actual_tail_waits_for_real_stop_task_before_arming(self):
        calls = []
        async def example():
            release = asyncio.Event()
            stop_task = asyncio.create_task(release.wait())
            with Guard(calls.append, timeout=.04, emergency_grace=.02, hard_exit=calls.append):
                task = asyncio.create_task(call_tail(extracted_tail(), stop_task))
                await asyncio.sleep(.1)
                self.assertEqual(calls, [])
                self.assertFalse(task.done())
                release.set()
                self.assertTrue(await task)
        asyncio.run(example())
        self.assertEqual(calls, [])

    def test_outer_cancellation_does_not_cancel_durable_stop_task(self):
        async def example():
            release = asyncio.Event()
            stop_task = asyncio.create_task(release.wait())
            task = asyncio.create_task(call_tail(extracted_tail(), stop_task))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(stop_task.cancelled())
            release.set()
            await stop_task
        asyncio.run(example())

    def test_system_exit_is_deferred_until_cooperative_tail_finishes(self):
        events = []
        class Control:
            async def stop(self):
                events.append("control-closed")
        async def example():
            stop_task = asyncio.create_task(asyncio.sleep(0))
            with self.assertRaises(SystemExit) as raised:
                await call_tail(extracted_tail(SystemExit(75)), stop_task, Control())
            self.assertEqual(raised.exception.code, 75)
        asyncio.run(example())
        self.assertEqual(events, ["control-closed"])

    def _subprocess_example(self, mode, verdict=True, expected_code=0):
        with tempfile.TemporaryDirectory() as folder:
            script = textwrap.dedent("""
                import asyncio, os, sys, threading
                from pathlib import Path
                sys.path.insert(0, STAGING)
                from test_shutdown_release import Guard, extracted_tail, call_tail
                root = Path(OUTPUT)
                async def persist():
                    # More than the finalization budget: active durable work must be unlimited.
                    for n in range(3):
                        await asyncio.sleep(.15)
                        with (root / ('chapter-' + str(n))).open('wb') as f:
                            f.write(('saved-chapter-' + str(n)).encode())
                            f.flush()
                            os.fsync(f.fileno())
                    with (root / 'database-closed').open('wb') as f:
                        f.write(b'closed')
                        f.flush()
                        os.fsync(f.fileno())
                async def cancellation_resistant():
                    while True:
                        try:
                            await asyncio.sleep(10)
                        except asyncio.CancelledError:
                            pass
                class Control:
                    async def stop(self):
                        if MODE == 'tail-hang':
                            await asyncio.Event().wait()
                async def main():
                    if MODE == 'cancellation-resistant':
                        asyncio.create_task(cancellation_resistant())
                    if MODE == 'executor-hang':
                        asyncio.get_running_loop().run_in_executor(None, threading.Event().wait)
                    done = asyncio.create_task(persist())
                    result = await call_tail(extracted_tail(VERDICT), done, Control())
                    if MODE == 'post-barrier-exception':
                        raise RuntimeError('post-durable-shutdown error')
                    return result
                def exit_callback(code):
                    if MODE == 'exit-callback-hang':
                        threading.Event().wait()
                    os._exit(code)
                with Guard(exit_callback, timeout=.15, emergency_grace=.1):
                    asyncio.run(main())
                    if MODE == 'exit-callback-hang':
                        threading.Event().wait()
            """)
            script = ("STAGING=" + repr(str(Path(__file__).resolve().parent)) + "\nOUTPUT=" + repr(folder)
                      + "\nMODE=" + repr(mode) + "\nVERDICT=" + repr(verdict) + "\n" + script)
            result = subprocess.run([sys.executable, "-c", script], timeout=4,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, expected_code, result.stderr)
            for n in range(3):
                self.assertEqual((Path(folder) / ("chapter-" + str(n))).read_bytes(),
                                 ("saved-chapter-" + str(n)).encode())
            self.assertEqual((Path(folder) / "database-closed").read_bytes(), b"closed")

    def test_cancellation_resistant_task_cannot_delay_process_after_durable_barrier(self):
        self._subprocess_example("cancellation-resistant")

    def test_stuck_default_executor_cannot_delay_process_after_durable_barrier(self):
        self._subprocess_example("executor-hang")

    def test_stuck_control_tail_cannot_delay_process_after_durable_barrier(self):
        self._subprocess_example("tail-hang")

    def test_stuck_exit_callback_has_independent_emergency_deadline(self):
        self._subprocess_example("exit-callback-hang")

    def test_clean_exit_stays_clean_and_keeps_all_chapters(self):
        self._subprocess_example("clean")

    def test_failed_exit_code_survives_a_stuck_finalizer(self):
        self._subprocess_example("cancellation-resistant", verdict=False, expected_code=1)

    def test_service_restart_exit_code_survives_a_stuck_tail(self):
        self._subprocess_example("tail-hang", verdict=SystemExit(75), expected_code=75)

    def test_post_barrier_exception_keeps_deadline_for_non_daemon_threads(self):
        # The executor thread blocks interpreter finalization after the CLI exception.
        with tempfile.TemporaryDirectory() as folder:
            script = ("import sys,threading\nsys.path.insert(0," + repr(str(HERE)) + ")\n"
                      "from psy_gateway_exit import GatewayProcessExitGuard,arm_after_durable_shutdown\n"
                      "import os\nthreading.Thread(target=threading.Event().wait).start()\n"
                      "with GatewayProcessExitGuard(os._exit,timeout=.1,emergency_grace=.05):\n"
                      "    arm_after_durable_shutdown(0)\n"
                      "    raise RuntimeError('after durable barrier')\n")
            result = subprocess.run([sys.executable, "-c", script], timeout=3,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, result.stderr)

    def test_failed_durable_stop_never_arms_the_finalization_guard(self):
        calls = []
        async def failed_stop():
            raise RuntimeError("durable-stop-failed")
        async def example():
            done = asyncio.create_task(failed_stop())
            with Guard(calls.append, timeout=.02, emergency_grace=.02, hard_exit=calls.append):
                with self.assertRaisesRegex(RuntimeError, "durable-stop-failed"):
                    await call_tail(extracted_tail(), done)
                await asyncio.sleep(.08)
        asyncio.run(example())
        self.assertEqual(calls, [])

    def test_both_actual_cli_entrypoints_keep_guard_through_asyncio_and_exit(self):
        for filename, function_name in [("hermes_cli/gateway.py", "run_gateway"),
                                        ("gateway/run.py", "main")]:
            tree = ast.parse((TARGET / filename).read_text())
            function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                            and n.name == function_name)
            guarded = [n for n in ast.walk(function) if isinstance(n, ast.With)
                       and any(isinstance(item.context_expr, ast.Call)
                               and isinstance(item.context_expr.func, ast.Name)
                               and item.context_expr.func.id == "GatewayProcessExitGuard"
                               for item in n.items)]
            self.assertEqual(len(guarded), 1, filename)
            calls = [n for n in ast.walk(guarded[0]) if isinstance(n, ast.Call)]
            self.assertTrue(any(isinstance(n.func, ast.Attribute) and n.func.attr == "run"
                                and isinstance(n.func.value, ast.Name)
                                and n.func.value.id == "asyncio" for n in calls), filename)
            self.assertTrue(any(isinstance(n.func, ast.Name)
                                and n.func.id in {"_hard_exit_after_gateway_teardown",
                                                  "_exit_after_graceful_shutdown"}
                                for n in calls), filename)


if __name__ == "__main__":
    unittest.main(verbosity=2)
