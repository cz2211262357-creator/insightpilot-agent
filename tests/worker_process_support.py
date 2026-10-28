"Launch and suspend only the specific child handles created by this test."

import ctypes
import json
import os
import signal
import subprocess
import sys
import sysconfig
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


def wait_file(path, process=None, seconds=15):
    deadline = time.monotonic() + seconds
    while not path.exists():
        if process and process.poll() is not None:
            raise AssertionError(
                "Owned Worker exited before expected fault/event; inspect its private fixture log"
            )
        if time.monotonic() > deadline:
            raise AssertionError("Owned Worker fault/event was not hit")
        time.sleep(0.02)


@contextmanager
def child(settings, url, run_id, scope, fault=False):
    folder = (
        settings.workspace_root / ".cache/insightpilot-verification" / ("worker-" + uuid4().hex)
    )
    folder.mkdir()
    paths = {
        name: folder / name for name in ("ready", "start", "result", "fault", "release", "log")
    }
    env = os.environ.copy()
    env.update(
        INSIGHTPILOT_MODEL_BASE_URL=url,
        INSIGHTPILOT_MODEL_API_KEY="synthetic-worker-key",
        PYTHONNOUSERSITE="1",
    )
    project = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = os.pathsep.join(
        (sysconfig.get_path("purelib"), str(project / "src"), str(project))
    )
    bootstrap = (
        "import site,runpy;site.addsitedir("
        + repr(sysconfig.get_path("purelib"))
        + ");runpy.run_module('tests.worker_process',run_name='__main__')"
    )
    command = [
        sys._base_executable,
        "-c",
        bootstrap,
        "--root",
        str(settings.workspace_root),
        "--run-id",
        str(run_id),
        "--scope",
        scope,
    ]
    for name in ("ready", "start", "result"):
        command.extend(["--" + name, str(paths[name])])
    if fault:
        command.extend(["--fault", str(paths["fault"]), "--release", str(paths["release"])])
    with paths["log"].open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        suspended = False

        def pause():
            nonlocal suspended
            assert process.poll() is None and process.pid != os.getpid()
            if os.name == "nt":
                call = ctypes.windll.ntdll.NtSuspendProcess
                call.argtypes = [ctypes.c_void_p]
                call.restype = ctypes.c_long
                assert call(int(process._handle)) == 0
            else:
                os.kill(process.pid, signal.SIGSTOP)
            suspended = True

        def resume():
            nonlocal suspended
            if suspended and process.poll() is None:
                if os.name == "nt":
                    call = ctypes.windll.ntdll.NtResumeProcess
                    call.argtypes = [ctypes.c_void_p]
                    call.restype = ctypes.c_long
                    assert call(int(process._handle)) == 0
                else:
                    os.kill(process.pid, signal.SIGCONT)
            suspended = False

        try:
            wait_file(paths["ready"], process)
            assert int(paths["ready"].read_text(encoding="utf-8")) == process.pid
            yield process, paths, pause, resume
        finally:
            resume()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def result(process, paths):
    wait_file(paths["result"], process, seconds=25)
    process.wait(timeout=8)
    assert process.returncode == 0
    return json.loads(paths["result"].read_text(encoding="utf-8"))
