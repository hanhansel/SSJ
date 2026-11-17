"""Centralized filesystem and environment layout for SSJ.

Application resources (templates, static files, migrations) are versioned and
read-only. Runtime data (uploads, logs, configuration, Caddy state) lives under
``SSJ_DATA_ROOT`` so an application update cannot overwrite business data.
"""

from dataclasses import dataclass
import os
from pathlib import Path
import sys

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WINDOWS_DATA_ROOT = Path(r"C:\SSJData")


def resource_root() -> Path:
    """Return the root containing bundled templates and static resources."""
    bundled_root = getattr(sys, "_MEIPASS", None)
    return Path(bundled_root).resolve() if bundled_root else PROJECT_ROOT


def _default_data_root(app_env: str) -> Path:
    if app_env == "production":
        return DEFAULT_WINDOWS_DATA_ROOT if os.name == "nt" else Path("/var/lib/ssj")
    return PROJECT_ROOT


def _absolute_path(value: str | None, default: Path, base: Path) -> Path:
    candidate = Path(value).expanduser() if value else default
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def deployment_env_path(data_root: Path | None = None) -> Path:
    root = data_root or DEFAULT_WINDOWS_DATA_ROOT
    return root / "config" / "ssj.env"


def load_runtime_environment() -> Path | None:
    """Load one explicit environment file without overriding process values.

    Windows services should set ``SSJ_ENV_FILE``. Source-based development
    continues to use the repository's ignored ``.env`` file. A packaged build
    can fall back to ``C:\\SSJData\\config\\ssj.env``.
    """
    explicit = os.environ.get("SSJ_ENV_FILE", "").strip()
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.append(deployment_env_path())
    candidates.append(PROJECT_ROOT / ".env")

    for candidate in candidates:
        if candidate.is_file():
            resolved = candidate.resolve()
            # override=False: values already in the process environment (e.g. a
            # test or service setting DATABASE_URL) must win over the file.
            load_dotenv(resolved, override=False)
            return resolved
    return None


@dataclass(frozen=True)
class RuntimePaths:
    resource_root: Path
    data_root: Path
    config_dir: Path
    upload_dir: Path
    document_upload_dir: Path
    print_template_dir: Path
    log_root: Path
    app_log_dir: Path
    caddy_log_dir: Path
    controller_log_dir: Path
    backup_log_dir: Path
    backup_dir: Path
    instance_dir: Path
    caddy_root: Path
    caddy_data_dir: Path
    caddy_config_dir: Path
    certificate_dir: Path
    staging_dir: Path

    @property
    def template_dir(self) -> Path:
        return self.resource_root / "templates"

    @property
    def static_dir(self) -> Path:
        return self.resource_root / "static"

    @property
    def migrations_dir(self) -> Path:
        return self.resource_root / "migrations"


def get_runtime_paths(app_env: str | None = None) -> RuntimePaths:
    app_env = (app_env or os.environ.get("APP_ENV", "development")).lower()
    default_root = _default_data_root(app_env)
    data_root = _absolute_path(os.environ.get("SSJ_DATA_ROOT"), default_root, PROJECT_ROOT)

    upload_dir = _absolute_path(os.environ.get("UPLOAD_FOLDER"), data_root / "uploads", data_root)
    log_root = _absolute_path(os.environ.get("LOG_ROOT"), data_root / "logs", data_root)
    app_log_dir = _absolute_path(os.environ.get("LOG_DIR"), log_root / "app", data_root)
    print_template_dir = _absolute_path(
        os.environ.get("PRINT_TEMPLATE_FOLDER"),
        upload_dir / "print-templates",
        data_root,
    )

    caddy_root = data_root / "caddy"
    return RuntimePaths(
        resource_root=resource_root(),
        data_root=data_root,
        config_dir=data_root / "config",
        upload_dir=upload_dir,
        document_upload_dir=upload_dir / "documents",
        print_template_dir=print_template_dir,
        log_root=log_root,
        app_log_dir=app_log_dir,
        caddy_log_dir=log_root / "caddy",
        controller_log_dir=log_root / "controller",
        backup_log_dir=log_root / "backup",
        backup_dir=data_root / "backups",
        instance_dir=data_root / "instance",
        caddy_root=caddy_root,
        caddy_data_dir=caddy_root / "data",
        caddy_config_dir=caddy_root / "config",
        certificate_dir=data_root / "certificates",
        staging_dir=data_root / "staging",
    )


def ensure_application_directories(paths: RuntimePaths, include_instance: bool = False) -> None:
    """Create only directories the Flask process writes during normal use."""
    directories = [
        paths.upload_dir,
        paths.document_upload_dir,
        paths.print_template_dir,
        paths.app_log_dir,
    ]
    if include_instance:
        directories.append(paths.instance_dir)
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
