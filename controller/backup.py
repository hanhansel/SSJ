"""Automatic backups: schedule settings, retention and the `--backup` run (no GUI).

An automatic backup is an ordinary snapshot labelled ``auto``.  Retention only
ever deletes those files: manual and pre-import snapshots in the same folder
are kept forever.  Automatic backups older than three backup intervals are
deleted (daily: 3 days, monthly: 3 months, ...), and only after a new backup
succeeded, so the newest one always survives.
"""

import json
import logging
import os
import re
from calendar import monthrange
from contextlib import contextmanager
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from controller.server_process import pid_alive, run_dir
from controller.snapshot import SNAPSHOT_SUFFIX, SnapshotError, default_snapshot_dir, export_snapshot

AUTO_LABEL = 'auto'
# Backup choices in the Automation tab, in their display order.
BACKUP_MODES = {
    'off': 'Off',
    'on_start': 'Every server start',
    'daily': 'Daily',
    'weekly': 'Weekly',
    'monthly': 'Monthly',
    'yearly': 'Yearly',
}
SCHEDULED_MODES = ('daily', 'weekly', 'monthly', 'yearly')
WEEKDAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
MONTHS = ('January', 'February', 'March', 'April', 'May', 'June', 'July',
          'August', 'September', 'October', 'November', 'December')
# Days 29-31 are missing from some months, so a monthly backup picks 1-28.
MAX_MONTH_DAY = 28
RETENTION_LABELS = {
    'on_start': '3 days', 'daily': '3 days', 'weekly': '3 weeks', 'monthly': '3 months', 'yearly': '3 years',
}
AUTO_FILE = re.compile(rf'^ssj_{AUTO_LABEL}_(\d{{4}}-\d{{2}}-\d{{2}}_\d{{6}}){re.escape(SNAPSHOT_SUFFIX)}(\.tmp)?$')


def _truthy(value):
    return str(value or '').strip().lower() in ('1', 'true', 'yes')


def normalize_backup_settings(mode, time_text, weekday, day, month, run_missed, folder):
    """Validate the Automation backup fields; returns the config values to save."""
    mode = (mode or 'off').strip()
    if mode not in BACKUP_MODES:
        raise ValueError('Unknown backup schedule.')
    match = re.fullmatch(r'(\d{1,2}):(\d{2})', (time_text or '').strip())
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise ValueError('The backup time must use the HH:MM format, e.g. 12:00.')
    try:
        weekday, day, month = int(weekday), int(day), int(month)
    except (TypeError, ValueError) as error:
        raise ValueError('The backup weekday, day and month must be numbers.') from error
    if not 1 <= weekday <= 7:
        raise ValueError('The backup weekday must be Monday to Sunday.')
    if not 1 <= day <= MAX_MONTH_DAY:
        raise ValueError(f'The backup day must be 1 to {MAX_MONTH_DAY}.')
    if not 1 <= month <= 12:
        raise ValueError('The backup month must be January to December.')
    folder = (folder or '').strip()
    if mode != 'off' and folder.startswith('\\\\'):
        # The tasks run without a stored Windows password, so they cannot open network shares.
        raise ValueError('The backup folder must be on this PC\'s disk (not a network folder \\\\server\\...).')
    return {
        'BACKUP_MODE': mode,
        'BACKUP_TIME': f'{int(match.group(1)):02d}:{match.group(2)}',
        'BACKUP_WEEKDAY': str(weekday),
        'BACKUP_DAY': str(day),
        'BACKUP_MONTH': str(month),
        'BACKUP_RUN_MISSED': 'true' if run_missed else 'false',
        'SNAPSHOT_FOLDER': folder,
    }


def snapshot_folder(values):
    configured = (values.get('SNAPSHOT_FOLDER') or '').strip()
    return Path(configured) if configured else default_snapshot_dir()


def _shift_months(moment, months):
    index = moment.year * 12 + moment.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    return moment.replace(year=year, month=month, day=min(moment.day, monthrange(year, month)[1]))


def retention_cutoff(mode, now):
    """Automatic backups created before this moment are deleted; None keeps everything."""
    if mode in ('on_start', 'daily'):
        return now - timedelta(days=3)
    if mode == 'weekly':
        return now - timedelta(weeks=3)
    if mode == 'monthly':
        return _shift_months(now, -3)
    if mode == 'yearly':
        return _shift_months(now, -36)
    return None


def prune_auto_backups(folder, mode, now):
    """Delete automatic backups (and their leftover .tmp files) older than the cutoff."""
    cutoff = retention_cutoff(mode, now)
    folder = Path(folder)
    if cutoff is None or not folder.is_dir():
        return []
    removed = []
    for path in folder.iterdir():
        match = AUTO_FILE.match(path.name)
        if not match or datetime.strptime(match.group(1), '%Y-%m-%d_%H%M%S') >= cutoff:
            continue
        try:
            path.unlink()
            removed.append(path.name)
        except OSError:
            pass
    return removed


def next_backup_time(values, now):
    """When the scheduled backup task fires next, or None when it has no schedule."""
    mode = values.get('BACKUP_MODE', 'off')
    if mode not in SCHEDULED_MODES:
        return None
    hour, minute = (int(part) for part in values.get('BACKUP_TIME', '12:00').split(':'))
    today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if mode == 'daily':
        return today if today > now else today + timedelta(days=1)
    if mode == 'weekly':
        days_ahead = (int(values.get('BACKUP_WEEKDAY', 1)) - 1 - now.weekday()) % 7
        candidate = today + timedelta(days=days_ahead)
        return candidate if candidate > now else candidate + timedelta(weeks=1)
    day = int(values.get('BACKUP_DAY', 1))
    if mode == 'monthly':
        candidate = today.replace(day=day)
        return candidate if candidate > now else _shift_months(candidate, 1)
    candidate = today.replace(month=int(values.get('BACKUP_MONTH', 1)), day=day)
    return candidate if candidate > now else candidate.replace(year=candidate.year + 1)


# ---------- run state shared with the controller window ----------
def status_path(values):
    return run_dir(values) / 'backup-status.json'


def read_status(values):
    try:
        return json.loads(status_path(values).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _write_status(values, **status):
    status['finished_at'] = datetime.now().isoformat(timespec='seconds')
    status_path(values).write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding='utf-8')


@contextmanager
def snapshot_lock(values):
    """Only one backup or import at a time; a lock left by a dead process is taken over."""
    path = run_dir(values) / 'snapshot.lock'
    try:
        held_by = int(path.read_text(encoding='utf-8').strip() or 0)
    except (OSError, ValueError):
        held_by = 0
    if held_by and held_by != os.getpid() and pid_alive(held_by):
        raise SnapshotError('Another backup or snapshot import is running. Try again once it has finished.')
    path.write_text(str(os.getpid()), encoding='utf-8')
    try:
        yield
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def _logger(values):
    logger = logging.getLogger('ssj.backup')
    if not logger.handlers:
        log_dir = Path(values.get('LOG_ROOT') or Path(values.get('SSJ_DATA_ROOT', 'C:/SSJData')) / 'logs') / 'backup'
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(log_dir / 'backup.log', maxBytes=1024 * 1024, backupCount=3, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


def run_backup(values, now=None):
    """One automatic backup: export, then delete expired automatic ones. Returns an exit code."""
    logger = _logger(values)
    mode = values.get('BACKUP_MODE', 'off')
    folder = snapshot_folder(values)
    try:
        with snapshot_lock(values):
            path = export_snapshot(values.get('DATABASE_URL'), values.get('UPLOAD_FOLDER'), folder, label=AUTO_LABEL)
            removed = prune_auto_backups(folder, mode, now or datetime.now())
    except SnapshotError as error:
        logger.error('Backup failed: %s', error)
        _write_status(values, ok=False, message=str(error))
        return 1
    logger.info('Backup saved: %s (%d old backups deleted)', path, len(removed))
    _write_status(values, ok=True, file=str(path), removed=removed,
                  message=f'Saved: {path.name}' + (f', {len(removed)} old backup(s) deleted' if removed else ''))
    return 0


def backup_enabled_on_start(values):
    return values.get('BACKUP_MODE') == 'on_start'


def run_missed(values):
    return _truthy(values.get('BACKUP_RUN_MISSED', 'true'))
