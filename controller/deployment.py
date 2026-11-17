"""Non-GUI deployment preparation helpers used by the SSJ Controller."""

from pathlib import Path
import secrets
import socket
from urllib.parse import quote

from controller.config import defaults_for_root, save_values


def validate_internal_port(value):
    try:
        port = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError('The internal port must be a number.') from error
    if not 1024 <= port <= 49151:
        raise ValueError('The internal port must be between 1024 and 49151.')
    return port


def build_postgresql_url(host, port, database, username, password):
    """Build a safe SQLAlchemy URL from user-facing PostgreSQL fields."""
    host = (host or '').strip()
    database = (database or '').strip()
    username = (username or '').strip()
    password = password or ''
    if not all((host, database, username, password)):
        raise ValueError('The PostgreSQL host, database, username, and password are required.')
    try:
        port = int(port)
    except (TypeError, ValueError) as error:
        raise ValueError('The PostgreSQL port must be a number.') from error
    if not 1 <= port <= 65535:
        raise ValueError('The PostgreSQL port must be between 1 and 65535.')
    # quote(safe='') percent-encodes every reserved char, including '+' and
    # spaces (quote_plus turned spaces into '+', which URLs read back literally).
    return (
        f"postgresql+psycopg2://{quote(username, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{quote(database, safe='')}"
    )


def normalize_smtp_values(
    host, port, username, password, from_email, from_name='SSJ',
    use_tls=True, use_ssl=False,
):
    """Validate optional password-reset mail settings for production."""
    host = (host or '').strip()
    username = (username or '').strip()
    password = password or ''
    from_email = (from_email or '').strip()
    from_name = (from_name or '').strip() or 'SSJ'

    # If no SMTP configured at all, allow empty configuration
    if not any((host, username, password, from_email)):
        return {
            'SMTP_HOST': '',
            'SMTP_PORT': str(port or '587'),
            'SMTP_USERNAME': '',
            'SMTP_PASSWORD': '',
            'SMTP_FROM_EMAIL': '',
            'SMTP_FROM_NAME': from_name,
            'SMTP_USE_TLS': 'true' if use_tls else 'false',
            'SMTP_USE_SSL': 'true' if use_ssl else 'false',
        }

    if not all((host, username, password, from_email)):
        raise ValueError('The SMTP host, username, password, and sender email are required when SMTP is used.')
    try:
        port = int(port)
    except (TypeError, ValueError) as error:
        raise ValueError('The SMTP port must be a number.') from error
    if not 1 <= port <= 65535:
        raise ValueError('The SMTP port must be between 1 and 65535.')
    if bool(use_tls) == bool(use_ssl):
        raise ValueError('Choose exactly one SMTP security option: STARTTLS or direct SSL.')
    return {
        'SMTP_HOST': host,
        'SMTP_PORT': str(port),
        'SMTP_USERNAME': username,
        'SMTP_PASSWORD': password,
        'SMTP_FROM_EMAIL': from_email,
        'SMTP_FROM_NAME': from_name,
        'SMTP_USE_TLS': 'true' if use_tls else 'false',
        'SMTP_USE_SSL': 'true' if use_ssl else 'false',
    }


def port_is_available(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def normalize_setup_values(data_root, internal_port, database_url=''):
    root = Path(data_root).expanduser().resolve()
    port = validate_internal_port(internal_port)
    values = defaults_for_root(root)
    values.update({
        'SSJ_DATA_ROOT': str(root),
        'SSJ_INTERNAL_PORT': str(port),
        'CONTROLLER_STATUS_URL': f'http://127.0.0.1:{port}/cstatus',
        'DATABASE_URL': (database_url or '').strip(),
        'SECRET_KEY': secrets.token_urlsafe(48),
    })
    return values


def layout_directories(values):
    root = Path(values['SSJ_DATA_ROOT'])
    directories = [
        root / 'config',
        root / 'uploads' / 'documents',
        Path(values['PRINT_TEMPLATE_FOLDER']),
        root / 'logs' / 'app',
        root / 'logs' / 'controller',
        root / 'logs' / 'backup',
        root / 'staging',
    ]
    directories.append(Path(values['BACKUP_FOLDER']))
    return directories


def ensure_runtime_layout(values):
    for directory in layout_directories(values):
        directory.mkdir(parents=True, exist_ok=True)


def prepare_initial_setup(values, config_path):
    ensure_runtime_layout(values)
    save_values(values, config_path)
