"""Run helper processes inside a Windows Job Object so the whole tree ends with them.

Quota reads start command-line tools that start more processes: the npm shim
``codex.CMD`` runs cmd.exe -> node -> ``codex app-server``, which in turn
starts its MCP servers; Grok starts its own MCP server under a hidden PTY.
Killing the launcher (or a psutil snapshot of its children) misses every
process whose parent has already exited, and those accumulated by the hundreds.

A job created with KILL_ON_JOB_CLOSE holds every descendant, however deep and
whoever its parent is. The process starts suspended, joins the job, then
resumes, so not even its first child can be created outside the job. Closing
the job (when the read finishes, times out, or the tray itself dies and Windows
closes its handles) terminates everything left in it.
"""
from __future__ import annotations

import ctypes
import logging
import os
import signal
import subprocess
from ctypes import wintypes

log = logging.getLogger(__name__)

CREATE_SUSPENDED = 0x00000004
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


try:
    _kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")
    _kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.restype = ctypes.c_long
except (AttributeError, OSError):      # not Windows: plain processes
    _kernel = _ntdll = None


class Job:
    """One kill-on-close job; `close()` ends every process still in it."""

    def __init__(self):
        self.handle = None
        if _kernel is None:
            return
        handle = _kernel.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel.SetInformationJobObject(handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                                               ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            _kernel.CloseHandle(handle)
            raise ctypes.WinError(error)
        self.handle = handle

    def assign(self, process_handle: int) -> None:
        if self.handle and not _kernel.AssignProcessToJobObject(self.handle, int(process_handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        handle, self.handle = self.handle, None
        if handle:
            _kernel.TerminateJobObject(handle, 1)
            _kernel.CloseHandle(handle)

    def __del__(self):
        self.close()


class _PosixGroup:
    """A private process group for bounded CLI reads on macOS/Linux."""

    def __init__(self, pid: int):
        self.pid = pid

    def close(self) -> None:
        pid, self.pid = self.pid, 0
        if pid:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def popen(args, **kwargs) -> subprocess.Popen:
    """subprocess.Popen whose whole process tree lives in its own job
    (``process.job``); end it with `stop()`. Without job support the process
    simply runs normally."""
    if _kernel is None:
        kwargs["start_new_session"] = True
        process = subprocess.Popen(args, **kwargs)
        process.job = _PosixGroup(process.pid)
        return process
    try:
        job = Job()
    except OSError:
        log.warning("job object unavailable; %s runs uncontained", args[0], exc_info=True)
        process = subprocess.Popen(args, **kwargs)
        process.job = None
        return process
    kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_SUSPENDED
    try:
        process = subprocess.Popen(args, **kwargs)
    except BaseException:
        job.close()
        raise
    handle = getattr(process, "_handle", None)
    if not isinstance(handle, int):         # a test double, not a real process
        job.close()
        process.job = None
        return process
    try:
        job.assign(handle)
    except OSError:
        log.warning("could not contain %s; it runs uncontained", args[0], exc_info=True)
        job.close()
        job = None
    status = _ntdll.NtResumeProcess(int(handle))
    process.job = job
    if status:
        # A process left suspended would itself be a leak.
        log.error("could not resume %s (NTSTATUS %#x); ending it", args[0], status & 0xFFFFFFFF)
        stop(process)
        raise OSError(f"无法启动 {args[0]}")
    return process


def stop(process: subprocess.Popen, timeout: float = 5.0) -> None:
    """End the process and every descendant still running in its job."""
    job = getattr(process, "job", None)
    if job is not None:
        job.close()
    elif process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        log.warning("process %s did not exit after stop", process.pid)


def run(args, *, timeout: float | None = None, capture_output: bool = False, input=None,
        **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run, contained: whatever the command left behind is ended
    when it returns, and a timeout ends the whole tree, not just the launcher.
    Output is collected until the command itself exits; a leftover helper that
    inherited the pipes cannot hold the call open (it is ended with the job)."""
    import threading
    if capture_output:
        kwargs["stdout"] = kwargs["stderr"] = subprocess.PIPE
    if input is not None:
        kwargs["stdin"] = subprocess.PIPE
    process = popen(args, **kwargs)
    chunks: dict[str, list] = {"stdout": [], "stderr": []}

    def drain(name):
        stream = getattr(process, name)
        try:
            for piece in iter(lambda: stream.read(65536), "" if kwargs.get("text") or kwargs.get("encoding") else b""):
                chunks[name].append(piece)
        except (OSError, ValueError):
            pass

    readers = [threading.Thread(target=drain, args=(name,), daemon=True) for name in chunks if getattr(process, name)]
    for reader in readers:
        reader.start()
    try:
        if input is not None:
            process.stdin.write(input)
            process.stdin.close()
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop(process)
        raise subprocess.TimeoutExpired(args, timeout) from None
    finally:
        stop(process)
        for reader in readers:
            reader.join(timeout=5)
        for name in chunks:
            stream = getattr(process, name)
            if stream:
                stream.close()
    joiner = "" if kwargs.get("text") or kwargs.get("encoding") else b""
    output = {name: joiner.join(parts) if getattr(process, name) else None for name, parts in chunks.items()}
    return subprocess.CompletedProcess(args, process.returncode, output["stdout"], output["stderr"])
