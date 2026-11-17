"""Windows Task Scheduler tasks for auto-start and automatic backup (no GUI).

The controller window (normal rights) writes the task definitions, then starts
``--register-tasks`` once with administrator rights (one UAC prompt) to load
them and open the server port in the Windows firewall.

Both tasks run as the Windows account that set them up, whether or not anyone
is logged on, without a stored password ("S4U").  That keeps the server under
the same account as the controller window, so the window can stop it and
``Documents`` points at that account's folder.  Such tasks cannot open network
shares, which is why the backup folder must be on this PC.
"""

import os
import subprocess
from datetime import datetime
from xml.sax.saxutils import escape

from controller.config import env_path
from controller.server_process import CREATE_NO_WINDOW, controller_command, controller_workdir

TASK_FOLDER = '\\SSJ\\'
SERVER_TASK = TASK_FOLDER + 'SSJ Server'
BACKUP_TASK = TASK_FOLDER + 'SSJ Backup'
FIREWALL_RULE = 'SSJ Server'
WEEKDAY_TAGS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
MONTH_TAGS = ('January', 'February', 'March', 'April', 'May', 'June', 'July',
              'August', 'September', 'October', 'November', 'December')


def tasks_dir():
    return env_path().parent / 'tasks'


def current_user():
    domain = os.environ.get('USERDOMAIN', '')
    user = os.environ.get('USERNAME', '')
    return f'{domain}\\{user}' if domain else user


def _settings(time_limit, start_when_available, restart):
    # Defaults Windows would otherwise apply: stop after 3 days, never start on
    # battery (a UPS on USB counts as one), below-normal priority.
    restart_xml = (
        '    <RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>\n' if restart else ''
    )
    return (
        '  <Settings>\n'
        '    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n'
        '    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n'
        '    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n'
        '    <AllowHardTerminate>true</AllowHardTerminate>\n'
        f'    <StartWhenAvailable>{"true" if start_when_available else "false"}</StartWhenAvailable>\n'
        '    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n'
        '    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>\n'
        '    <AllowStartOnDemand>true</AllowStartOnDemand>\n'
        '    <Enabled>true</Enabled>\n'
        '    <Hidden>false</Hidden>\n'
        '    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n'
        '    <WakeToRun>false</WakeToRun>\n'
        f'    <ExecutionTimeLimit>{time_limit}</ExecutionTimeLimit>\n'
        '    <Priority>5</Priority>\n'
        f'{restart_xml}'
        '  </Settings>\n'
    )


def _task_xml(description, trigger, settings, argument, user):
    command, *arguments = controller_command(argument)
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        f'  <RegistrationInfo><Description>{escape(description)}</Description></RegistrationInfo>\n'
        f'  <Triggers>\n{trigger}  </Triggers>\n'
        '  <Principals>\n'
        '    <Principal id="Author">\n'
        f'      <UserId>{escape(user)}</UserId>\n'
        '      <LogonType>S4U</LogonType>\n'
        '      <RunLevel>LeastPrivilege</RunLevel>\n'
        '    </Principal>\n'
        '  </Principals>\n'
        f'{settings}'
        '  <Actions Context="Author">\n'
        '    <Exec>\n'
        f'      <Command>{escape(command)}</Command>\n'
        f'      <Arguments>{escape(subprocess.list2cmdline(arguments))}</Arguments>\n'
        f'      <WorkingDirectory>{escape(controller_workdir())}</WorkingDirectory>\n'
        '    </Exec>\n'
        '  </Actions>\n'
        '</Task>\n'
    )


def server_task_xml(user):
    # One minute after boot: PostgreSQL and the network come up first.
    trigger = '    <BootTrigger><Delay>PT1M</Delay></BootTrigger>\n'
    return _task_xml('Starts the SSJ server when the PC boots.', trigger,
                     _settings('PT0S', start_when_available=False, restart=True), '--autostart', user)


def _backup_trigger(values, today):
    hour, minute = values.get('BACKUP_TIME', '12:00').split(':')
    start = f'{today.isoformat()}T{int(hour):02d}:{minute}:00'
    mode = values.get('BACKUP_MODE')
    if mode == 'daily':
        schedule = '<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>'
    elif mode == 'weekly':
        day = WEEKDAY_TAGS[int(values.get('BACKUP_WEEKDAY', 1)) - 1]
        schedule = f'<ScheduleByWeek><WeeksInterval>1</WeeksInterval><DaysOfWeek><{day} /></DaysOfWeek></ScheduleByWeek>'
    else:
        months = MONTH_TAGS if mode == 'monthly' else (MONTH_TAGS[int(values.get('BACKUP_MONTH', 1)) - 1],)
        schedule = (
            f'<ScheduleByMonth><DaysOfMonth><Day>{int(values.get("BACKUP_DAY", 1))}</Day></DaysOfMonth>'
            f'<Months>{"".join(f"<{month} />" for month in months)}</Months></ScheduleByMonth>'
        )
    return f'    <CalendarTrigger><StartBoundary>{start}</StartBoundary>{schedule}</CalendarTrigger>\n'


def backup_task_xml(values, user, today=None):
    from controller.backup import run_missed
    trigger = _backup_trigger(values, today or datetime.now().date())
    return _task_xml('Automatic SSJ data backup (snapshot).', trigger,
                     _settings('PT2H', start_when_available=run_missed(values), restart=False), '--backup', user)


def write_task_files(values, user=None):
    """Write the definitions the elevated helper loads; a missing file means: remove that task."""
    from controller.backup import SCHEDULED_MODES
    user = user or current_user()
    folder = tasks_dir()
    folder.mkdir(parents=True, exist_ok=True)
    wanted = {
        'server.xml': server_task_xml(user) if values.get('AUTOSTART_ENABLED') == 'true' else None,
        'backup.xml': backup_task_xml(values, user) if values.get('BACKUP_MODE') in SCHEDULED_MODES else None,
    }
    for name, content in wanted.items():
        path = folder / name
        if content is None:
            if path.exists():
                path.unlink()
        else:
            path.write_text(content, encoding='utf-16')
    (folder / 'port.txt').write_text(str(values.get('SSJ_INTERNAL_PORT', '32425')), encoding='utf-8')


def _run(command):
    result = subprocess.run(command, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
    return result.returncode, (result.stdout + result.stderr).strip()


def register_from_files():
    """Elevated helper (`--register-tasks`): load or remove both tasks and open the port. Returns an exit code."""
    folder = tasks_dir()
    messages, ok = [], True
    for name, task in (('server.xml', SERVER_TASK), ('backup.xml', BACKUP_TASK)):
        path = folder / name
        if path.exists():
            code, output = _run(['schtasks', '/Create', '/TN', task, '/XML', str(path), '/F'])
            ok = ok and code == 0
            messages.append(f'{task}: {"registered" if code == 0 else "FAILED - " + output}')
        else:
            _run(['schtasks', '/Delete', '/TN', task, '/F'])
            messages.append(f'{task}: not used')
    try:
        port = int((folder / 'port.txt').read_text(encoding='utf-8').strip())
    except (OSError, ValueError):
        port = None
    if port:
        _run(['netsh', 'advfirewall', 'firewall', 'delete', 'rule', f'name={FIREWALL_RULE}'])
        code, output = _run(['netsh', 'advfirewall', 'firewall', 'add', 'rule', f'name={FIREWALL_RULE}',
                             'dir=in', 'action=allow', 'protocol=TCP', f'localport={port}', 'profile=any'])
        ok = ok and code == 0
        messages.append(f'Firewall port {port}: {"opened" if code == 0 else "FAILED - " + output}')
    (folder / 'last-apply.txt').write_text('\n'.join(messages), encoding='utf-8')
    return 0 if ok else 1


def unregister_all():
    """Elevated helper (`--unregister-tasks`, used by the uninstaller): remove tasks and the firewall rule."""
    for task in (SERVER_TASK, BACKUP_TASK):
        _run(['schtasks', '/Delete', '/TN', task, '/F'])
    _run(['netsh', 'advfirewall', 'firewall', 'delete', 'rule', f'name={FIREWALL_RULE}'])
    return 0


def apply_elevated():
    """Run `--register-tasks` with administrator rights and wait. Returns (ok, message)."""
    command, *arguments = controller_command('--register-tasks')

    def quoted(text):
        return "'" + text.replace("'", "''") + "'"

    script = (
        f'$p = Start-Process -FilePath {quoted(command)} -ArgumentList {quoted(subprocess.list2cmdline(arguments))} '
        f'-WorkingDirectory {quoted(controller_workdir())} -Verb RunAs -WindowStyle Hidden -Wait -PassThru; '
        'exit $p.ExitCode'
    )
    report = tasks_dir() / 'last-apply.txt'
    if report.exists():
        report.unlink()
    result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
                            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
    details = report.read_text(encoding='utf-8') if report.exists() else ''
    if not details:
        return False, 'Administrator permission was cancelled or refused. The settings are saved, but the tasks are not registered yet.'
    return result.returncode == 0, details
