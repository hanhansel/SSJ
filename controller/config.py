"""Configuration reader/writer shared by the local Controller and SSJ service."""

import os
from pathlib import Path


DEFAULT_DATA_ROOT = Path(r"C:\SSJData")


def defaults_for_root(data_root=DEFAULT_DATA_ROOT):
    root = Path(data_root).expanduser()
    internal_port = "32425"
    return {
        'SSJ_DATA_ROOT': str(root),
        'APP_ENV': 'production',
        'SSJ_INTERNAL_HOST': '0.0.0.0',
        'SSJ_INTERNAL_PORT': internal_port,
        # Devices reach Waitress directly at http://<server-ip>:<port>; with no proxy in
        # front, X-Forwarded-For must not be trusted (it would fake the login IP).
        'TRUST_REVERSE_PROXY': 'false',
        'SESSION_COOKIE_SECURE': 'false',
        'SECRET_KEY': '',
        'DATABASE_URL': '',
        'UPLOAD_FOLDER': str(root / 'uploads'),
        'PRINT_TEMPLATE_FOLDER': str(root / 'uploads' / 'print-templates'),
        'LOG_ROOT': str(root / 'logs'),
        'LOG_DIR': str(root / 'logs' / 'app'),
        'LOG_MAX_BYTES': str(10 * 1024 * 1024),
        'LOG_BACKUP_COUNT': '10',
        'BACKUP_FOLDER': str(root / 'backups'),
        'SMTP_HOST': '',
        'SMTP_PORT': '587',
        'SMTP_USERNAME': '',
        'SMTP_PASSWORD': '',
        'SMTP_FROM_EMAIL': '',
        'SMTP_FROM_NAME': 'SSJ',
        'SMTP_USE_TLS': 'true',
        'SMTP_USE_SSL': 'false',
        'PASSWORD_RESET_OTP_TTL_SECONDS': '600',
        'PASSWORD_RESET_MAX_ATTEMPTS': '5',
        'PASSWORD_RESET_RESEND_COOLDOWN_SECONDS': '60',
        'LOGIN_MAX_ATTEMPTS': '5',
        'LOGIN_WINDOW_SECONDS': '900',
        'PASSWORD_RESET_MAX_REQUESTS': '3',
        'PASSWORD_RESET_WINDOW_SECONDS': '3600',
        'CONTROLLER_SERVICE_NAME': 'SSJApp',
        'CONTROLLER_STATUS_URL': f'http://127.0.0.1:{internal_port}/cstatus',
        # Automation tab (controller/backup.py, controller/tasks.py).
        'AUTOSTART_ENABLED': 'false',
        'BACKUP_MODE': 'off',
        'BACKUP_TIME': '12:00',
        'BACKUP_WEEKDAY': '1',
        'BACKUP_DAY': '1',
        'BACKUP_MONTH': '1',
        'BACKUP_RUN_MISSED': 'true',
        'SNAPSHOT_FOLDER': '',  # empty: Documents\SSJ Snapshots of the controller's Windows account
    }


DEFAULTS = defaults_for_root()


def env_path():
    """Return the explicit production configuration file location."""
    configured = os.environ.get('SSJ_ENV_FILE')
    if configured:
        return Path(configured).expanduser().resolve()
    return DEFAULT_DATA_ROOT / 'config' / 'ssj.env'


def _read_raw_values(path):
    values = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        values[key.strip()] = value.strip()
    return values


def load_values(path=None):
    path = Path(path or env_path())
    raw_values = _read_raw_values(path)
    values = defaults_for_root(raw_values.get('SSJ_DATA_ROOT', DEFAULT_DATA_ROOT))
    values.update(raw_values)
    return values


def save_values(values, path=None):
    """Update known keys atomically while preserving comments and future keys."""
    path = Path(path or env_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_text(encoding='utf-8').splitlines() if path.exists() else []
    remaining = {key: str(value) for key, value in values.items()}
    output = []
    for raw_line in original:
        stripped = raw_line.strip()
        if stripped and not stripped.startswith('#') and '=' in raw_line:
            key = raw_line.split('=', 1)[0].strip()
            if key in remaining:
                output.append(f'{key}={remaining.pop(key)}')
                continue
        output.append(raw_line)
    if remaining:
        if output and output[-1]:
            output.append('')
        output.append('# Managed by SSJ Server Controller')
        output.extend(f'{key}={value}' for key, value in remaining.items())
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text('\n'.join(output) + '\n', encoding='utf-8')
    os.replace(temporary, path)
