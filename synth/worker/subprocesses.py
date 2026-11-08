"""Bounded native jobs: timeouts terminate wrappers and their descendants."""
import os
import signal
import subprocess
import time


def run_process(args, *, input=None, timeout=None, capture_output=False, check=False, **options):
    if capture_output:
        options.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if input is not None:
        options['stdin'] = subprocess.PIPE
    with subprocess.Popen(args, start_new_session=True, **options) as process:
        try:
            stdout, stderr = process.communicate(input=input, timeout=timeout)
        except BaseException:
            # rtk and other launchers may wait on another process. Killing just
            # the launcher can leave inference and its inherited lease lock alive.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            # Reaping the launcher can beat the kernel closing a descendant's
            # inherited lease. Wait for this owned process group to disappear.
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    break
                except PermissionError:
                    # macOS can briefly expose an adopted, already-killed
                    # descendant while launchd finishes reaping the group.
                    pass
                time.sleep(.01)
            raise
        result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result
