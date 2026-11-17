"""The SSJ server as a background process, shared by the boot task and the Start button (no GUI).

``SSJ-Controller.exe --autostart`` runs a small supervisor: it waits for
PostgreSQL, starts Waitress, optionally makes a
backup once the server answers, and stops everything when the controller
window (or the installer) leaves a stop request.  It records itself in
``<SSJ_DATA_ROOT>/run/server.json`` so any controller window can find it, no
matter who started it, and closing a window never stops it.
"""

import ctypes
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
STILL_ACTIVE = 259
DB_WAIT_SECONDS = 180
HEALTH_WAIT_SECONDS = 120
STOP_WAIT_SECONDS = 20
MAX_RESTARTS = 3  # Waitress crashing more often than this is a real fault: stop and log it
STARTED_BY_TASK = 'task'
STARTED_BY_CONTROLLER = 'controller'


def _truthy(value):
    return str(value or '').strip().lower() in ('1', 'true', 'yes')


def controller_command(*arguments):
    """Command line that runs this controller again: the packaged exe, or `python -m controller.main`."""
    if getattr(sys, 'frozen', False):
        return [sys.executable, *arguments]
    return [sys.executable, '-m', 'controller.main', *arguments]


def controller_workdir():
    return str(Path(sys.executable).parent if getattr(sys, 'frozen', False) else PROJECT_ROOT)


# ---------- process checks ----------
def pid_alive(pid):
    if not pid:
        return False
    if sys.platform != 'win32':
        try:
            os.kill(pid, 0)
        except PermissionError:
            return True
        except OSError:
            return False
        return True
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5  # access denied: it exists, owned by another account
    try:
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


# ---------- state file ----------
def run_dir(values):
    path = Path(values.get('SSJ_DATA_ROOT') or 'C:/SSJData') / 'run'
    path.mkdir(parents=True, exist_ok=True)
    return path


def _state_path(values):
    return run_dir(values) / 'server.json'


def _stop_path(values):
    return run_dir(values) / 'server.stop'


def server_state(values):
    """The running supervisor's record, or None. A record left by a dead process is removed."""
    path = _state_path(values)
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if pid_alive(state.get('pid')):
        return state
    try:
        path.unlink()
    except OSError:
        pass
    return None


def is_running(values):
    return server_state(values) is not None


def start_background(values):
    """Start the supervisor from the controller window; it outlives the window."""
    if is_running(values):
        return
    subprocess.Popen(
        controller_command('--autostart', '--from-controller'),
        cwd=controller_workdir(),
        creationflags=(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP) if sys.platform == 'win32' else 0,
        close_fds=True,
    )


def stop(values, wait_seconds=STOP_WAIT_SECONDS):
    """Ask the supervisor to stop and wait for it; force-kill only if it does not react.

    Returns (stopped, message).
    """
    state = server_state(values)
    if not state:
        return True, 'The server is not running.'
    _stop_path(values).write_text(datetime.now().isoformat(timespec='seconds'), encoding='utf-8')
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if not pid_alive(state['pid']):
            return True, 'Server stopped.'
        time.sleep(0.25)
    if sys.platform == 'win32':
        subprocess.run(['taskkill', '/PID', str(state['pid']), '/T', '/F'],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
    if pid_alive(state['pid']):
        return False, 'The server is not responding and cannot be stopped from this Windows account.'
    for path in (_state_path(values), _stop_path(values)):
        try:
            path.unlink()
        except OSError:
            pass
    return True, 'Server force-stopped.'


# ---------- supervisor ----------
class _KillOnCloseJob:
    """Windows job object: if the supervisor dies, Windows also ends Waitress."""

    def __init__(self):
        self.handle = None
        if sys.platform != 'win32':
            return
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel32 = kernel32
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return

        class BasicLimits(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                        ('LimitFlags', ctypes.c_uint32), ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', ctypes.c_uint32),
                        ('Affinity', ctypes.c_size_t), ('PriorityClass', ctypes.c_uint32),
                        ('SchedulingClass', ctypes.c_uint32)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', BasicLimits), ('IoInfo', IoCounters),
                        ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if kernel32.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.handle = handle

    def add(self, process):
        if self.handle:
            self.kernel32.AssignProcessToJobObject(self.handle, int(process._handle))


def _logger(values):
    logger = logging.getLogger('ssj.server')
    if not logger.handlers:
        log_dir = Path(values.get('LOG_ROOT') or Path(values.get('SSJ_DATA_ROOT', 'C:/SSJData')) / 'logs') / 'controller'
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(log_dir / 'server.log', maxBytes=1024 * 1024, backupCount=3, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


def _wait_for_database(database_url, stop_path, logger):
    """At boot PostgreSQL may still be starting; Waitress would then skip its table check."""
    if not (database_url or '').startswith(('postgresql', 'postgres')):
        return
    from sqlalchemy import create_engine, text
    engine = create_engine(database_url, connect_args={'connect_timeout': 5})
    deadline = time.monotonic() + DB_WAIT_SECONDS
    try:
        while not stop_path.exists():
            try:
                with engine.connect() as conn:
                    conn.execute(text('SELECT 1'))
                return
            except Exception as error:
                if time.monotonic() >= deadline:
                    logger.warning('PostgreSQL is not ready after %d seconds; starting the server anyway: %s',
                                   DB_WAIT_SECONDS, error)
                    return
                time.sleep(5)
    finally:
        engine.dispose()


def _wait_until_healthy(values, stop_path):
    import requests
    url = values.get('CONTROLLER_STATUS_URL')
    deadline = time.monotonic() + HEALTH_WAIT_SECONDS
    while time.monotonic() < deadline and not stop_path.exists():
        try:
            if requests.get(url, timeout=2).json().get('status') == 'healthy':
                return True
        except (requests.RequestException, ValueError):
            pass
        time.sleep(2)
    return False


def _start_app(job, flags):
    app = subprocess.Popen(controller_command('--serve'), cwd=controller_workdir(), creationflags=flags)
    job.add(app)
    return app


def supervise(values, started_by):
    """Run the server until a stop request; returns the process exit code."""
    logger = _logger(values)
    if is_running(values):
        logger.info('The server is already running; start request ignored.')
        return 0
    stop_path = _stop_path(values)
    try:
        stop_path.unlink()
    except OSError:
        pass
    _state_path(values).write_text(json.dumps({
        'pid': os.getpid(),
        'started_by': started_by,
        'started_at': datetime.now().isoformat(timespec='seconds'),
    }), encoding='utf-8')

    job = _KillOnCloseJob()
    children = []
    flags = CREATE_NO_WINDOW if sys.platform == 'win32' else 0
    exit_code = 0
    try:
        _wait_for_database(values.get('DATABASE_URL'), stop_path, logger)
        if stop_path.exists():
            return 0
        app = _start_app(job, flags)
        children.append(app)
        logger.info('SSJ server started (%s), port %s.', started_by, values.get('SSJ_INTERNAL_PORT'))

        from controller.backup import backup_enabled_on_start
        if backup_enabled_on_start(values):
            if _wait_until_healthy(values, stop_path):
                # A separate process, so a stop request is never held up by a running backup.
                subprocess.Popen(controller_command('--backup'), cwd=controller_workdir(), creationflags=flags)
            elif not stop_path.exists():
                logger.warning('The server is not healthy after %d seconds; the start-up backup is skipped.', HEALTH_WAIT_SECONDS)

        restarts = 0
        while not stop_path.exists():
            if app.poll() is not None:
                logger.error('The SSJ server stopped on its own (code %s).', app.returncode)
                if restarts >= MAX_RESTARTS:
                    exit_code = 1
                    break
                restarts += 1
                time.sleep(5)
                children.remove(app)
                app = _start_app(job, flags)
                children.append(app)
                logger.info('SSJ server restarted (%d/%d).', restarts, MAX_RESTARTS)
            time.sleep(1)
        else:
            logger.info('Server stopped on request.')
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
        for path in (_state_path(values), stop_path):
            try:
                path.unlink()
            except OSError:
                pass
    return exit_code
