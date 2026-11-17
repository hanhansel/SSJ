import os
import secrets
import hmac
import logging
from logging.handlers import RotatingFileHandler
from pythonjsonlogger import jsonlogger
import click
from flask import Flask, abort, request, session, redirect, url_for, jsonify
from flask_migrate import Migrate
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from werkzeug.middleware.proxy_fix import ProxyFix
from apps.models import (
    db, PasswordResetOTP, Role, User, SystemSetting, Document, HO, wib_now, DEFAULT_SYSTEM_SETTINGS,
    MENUS, ROLE_OFFICE, ROLE_TECHNICIAN,
)
from apps.core.registry import DOC_REGISTRY, format_document_number, format_ho_number
from apps.core.dates import register_template_filters
from apps.core.paths import ensure_application_directories, get_runtime_paths, load_runtime_environment

load_runtime_environment()

from apps.core.config import settings

migrate = Migrate()


@event.listens_for(Engine, 'connect')
def configure_database_connection(dbapi_connection, _connection_record):
    """Set DB-specific configurations (SQLite FKs, PostgreSQL Timezone)."""
    module_name = dbapi_connection.__class__.__module__
    if module_name.startswith('sqlite3'):
        cursor = dbapi_connection.cursor()
        cursor.execute('PRAGMA foreign_keys=ON')
        cursor.close()
    elif module_name.startswith('psycopg2'):
        cursor = dbapi_connection.cursor()
        cursor.execute("SET TIME ZONE 'Asia/Jakarta'")
        # Every pooled connection must use the same schema.  The role's default
        # search_path ("$user", public) would otherwise send some connections to
        # a schema named after the DB user while others use public.
        cursor.execute("SET search_path TO public")
        cursor.close()


def seed_default_settings():
    for key, val, cat, desc in DEFAULT_SYSTEM_SETTINGS:
        existing = SystemSetting.query.filter_by(key=key).first()
        if not existing:
            setting = SystemSetting(key=key, value=val, category=cat, description=desc)
            db.session.add(setting)
    db.session.commit()


def seed_default_roles():
    """The two account types. What an office account may open is set per account (its menus)."""
    defaults = [
        (ROLE_OFFICE, 'Office', 'Office account; the menus it may open are set per account'),
        (ROLE_TECHNICIAN, 'Technician', 'Works on its own tasks and sends reports from a phone'),
    ]
    for code, name, description in defaults:
        if not Role.query.filter_by(code=code).first():
            db.session.add(Role(code=code, name=name, description=description))
    db.session.commit()



LOOPBACK_ADDRESSES = {'127.0.0.1', '::1'}


class LoopbackProxyFix:
    """Trust X-Forwarded-* only when the request really comes from local Caddy.

    Direct LAN clients (e.g. a phone testing http://<laptop-ip>:32425) are
    served normally, but any forwarded headers they send are ignored.
    """

    def __init__(self, wsgi_app):
        self.direct_app = wsgi_app
        self.proxied_app = ProxyFix(wsgi_app, x_for=1, x_proto=1, x_host=1)

    def __call__(self, environ, start_response):
        if environ.get('REMOTE_ADDR') in LOOPBACK_ADDRESSES:
            return self.proxied_app(environ, start_response)
        return self.direct_app(environ, start_response)


def backfill_document_numbers():
    """Freeze the printable number of older rows so later setting changes do not rewrite history."""
    for doc in Document.query.filter(Document.doc_number_text.is_(None)).all():
        doc.doc_number_text = format_document_number(doc.document_type, doc.doc_no, doc.date)
    for ho in HO.query.filter(HO.ho_number_text.is_(None)).all():
        ho.ho_number_text = format_ho_number(ho.prj.doc_no, ho.prj.date)
    db.session.commit()


def configure_logging(app, log_dir):
    """Write safe operational history for the controller and support staff."""
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, 'ssj-app.log')
    if not any(getattr(handler, 'baseFilename', None) == log_path for handler in app.logger.handlers):
        handler = RotatingFileHandler(
            log_path,
            maxBytes=app.config['LOG_MAX_BYTES'],
            backupCount=app.config['LOG_BACKUP_COUNT'],
            encoding='utf-8',
        )
        formatter = jsonlogger.JsonFormatter('%(asctime)s %(levelname)s %(name)s %(message)s')
        handler.setFormatter(formatter)
        app.logger.addHandler(handler)
    app.logger.setLevel(logging.INFO)


def create_app():
    paths = get_runtime_paths(settings.app_env)
    app = Flask(
        __name__,
        template_folder=str(paths.template_dir),
        static_folder=str(paths.static_dir),
        instance_path=str(paths.instance_dir),
    )
    app.config['APP_ENV'] = os.environ.get('APP_ENV', settings.app_env)
    app.config['SSJ_DATA_ROOT'] = str(paths.data_root)
    app.config['SSJ_INTERNAL_HOST'] = os.environ.get('SSJ_INTERNAL_HOST', settings.ssj_internal_host)
    app.config['SSJ_INTERNAL_PORT'] = int(os.environ.get('SSJ_INTERNAL_PORT', str(settings.ssj_internal_port)))
    app.config['SESSION_PERMANENT'] = False
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['SESSION_COOKIE_SECURE'] = os.environ.get('SESSION_COOKIE_SECURE', str(settings.session_cookie_secure)).lower() in ('1', 'true', 'yes')
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', settings.secret_key) or secrets.token_hex(32)

    development_bootstrap = app.config['APP_ENV'] != 'production'
    ensure_application_directories(paths, include_instance=development_bootstrap)
    app.config['UPLOAD_FOLDER'] = str(paths.upload_dir)
    app.config['PRINT_TEMPLATE_FOLDER'] = str(paths.print_template_dir)
    app.config['LOG_DIR'] = str(paths.app_log_dir)
    app.config['LOG_MAX_BYTES'] = int(os.environ.get('LOG_MAX_BYTES', str(10 * 1024 * 1024)))
    app.config['LOG_BACKUP_COUNT'] = int(os.environ.get('LOG_BACKUP_COUNT', '10'))
    configure_logging(app, app.config['LOG_DIR'])

    # os.environ wins over the import-time `settings` snapshot so callers such as
    # the unit tests can point a new app at a disposable database.
    database_url = os.environ.get('DATABASE_URL') or settings.database_url
    if database_url.startswith('postgres://'):
        database_url = database_url.replace('postgres://', 'postgresql://', 1)
    if database_url:
        app.config['SQLALCHEMY_DATABASE_URI'] = database_url
    else:
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + str(paths.instance_dir / 'ssj.db')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    app.config['SMTP_HOST'] = settings.smtp_host
    app.config['SMTP_PORT'] = settings.smtp_port
    app.config['SMTP_USERNAME'] = settings.smtp_username
    app.config['SMTP_PASSWORD'] = settings.smtp_password
    app.config['SMTP_FROM_EMAIL'] = settings.smtp_from_email or settings.smtp_username
    app.config['SMTP_FROM_NAME'] = os.environ.get('SMTP_FROM_NAME', 'SSJ')
    app.config['SMTP_USE_TLS'] = settings.smtp_use_tls
    app.config['SMTP_USE_SSL'] = settings.smtp_use_ssl
    app.config['PASSWORD_RESET_OTP_TTL_SECONDS'] = int(os.environ.get('PASSWORD_RESET_OTP_TTL_SECONDS', '600'))
    app.config['PASSWORD_RESET_MAX_ATTEMPTS'] = int(os.environ.get('PASSWORD_RESET_MAX_ATTEMPTS', '5'))
    app.config['PASSWORD_RESET_RESEND_COOLDOWN_SECONDS'] = int(os.environ.get('PASSWORD_RESET_RESEND_COOLDOWN_SECONDS', '60'))
    app.config['LOGIN_MAX_ATTEMPTS'] = int(os.environ.get('LOGIN_MAX_ATTEMPTS', '5'))
    app.config['LOGIN_WINDOW_SECONDS'] = int(os.environ.get('LOGIN_WINDOW_SECONDS', '900'))
    app.config['PASSWORD_RESET_MAX_REQUESTS'] = int(os.environ.get('PASSWORD_RESET_MAX_REQUESTS', '3'))
    app.config['PASSWORD_RESET_WINDOW_SECONDS'] = int(os.environ.get('PASSWORD_RESET_WINDOW_SECONDS', '3600'))
    app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('MAX_UPLOAD_MB', '50')) * 1024 * 1024

    register_template_filters(app)
    db.init_app(app)
    migrate.init_app(app, db, compare_type=True)
    if os.environ.get('TRUST_REVERSE_PROXY', 'false').lower() in ('1', 'true', 'yes'):
        # Only the local Caddy (loopback) may set X-Forwarded-* headers.
        app.wsgi_app = LoopbackProxyFix(app.wsgi_app)

    with app.app_context():
        try:
            # Make sure the public schema exists in PostgreSQL in case it was just dropped
            db_uri = str(app.config.get('SQLALCHEMY_DATABASE_URI', ''))
            if 'postgresql' in db_uri:
                with db.engine.connect() as conn:
                    conn.execute(text("CREATE SCHEMA IF NOT EXISTS public;"))
                    conn.execute(text("SET search_path TO public, pg_catalog;"))
                    conn.commit()

            # create_all() is idempotent: it only creates tables that do not exist
            # yet, so new tables (e.g. hos, ho_photos) appear automatically on
            # existing databases without running `flask db upgrade`.
            db.create_all()
            seed_default_roles()
            seed_default_settings()
            backfill_document_numbers()
        except Exception as e:
            app.logger.warning("Database bootstrap notice: %s", e)

        # Remove stale one-time password records when the application starts.
        try:
            PasswordResetOTP.query.filter(PasswordResetOTP.expires_at < wib_now()).delete()
            db.session.commit()
        except Exception:
            pass

        from apps.authentication.routes import auth_bp
        from apps.scheduling.routes import scheduling_bp
        from apps.system.routes import system_bp
        from apps.documents.routes import documents_bp
        from apps.technician.routes import technician_bp

        app.register_blueprint(auth_bp)
        app.register_blueprint(scheduling_bp)
        app.register_blueprint(system_bp)
        app.register_blueprint(documents_bp)
        app.register_blueprint(technician_bp)

        @app.cli.command('bootstrap-system')
        def bootstrap_system():
            """Create the fixed role and setting catalog after `flask db upgrade`."""
            seed_default_roles()
            seed_default_settings()
            click.echo('Role and default system settings are ready.')

        @app.route('/')
        def index():
            if 'user_id' not in session:
                return redirect(url_for('auth.login'))
            role = session.get('role', '')
            if role == ROLE_TECHNICIAN:
                return redirect(url_for('technician.dashboard'))
            return redirect(url_for('system.dashboard'))

        @app.route('/cstatus')
        def cstatus():
            """Minimal live status endpoint for the local Controller application."""
            checks = {'database': 'connected', 'uploads': 'writable', 'logs': 'writable'}
            healthy = True
            try:
                db.session.execute(text('SELECT 1'))
            except Exception:
                db.session.rollback()
                checks['database'] = 'unavailable'
                healthy = False
            if not os.access(app.config['UPLOAD_FOLDER'], os.W_OK):
                checks['uploads'] = 'unavailable'
                healthy = False
            if not os.access(app.config['LOG_DIR'], os.W_OK):
                checks['logs'] = 'unavailable'
                healthy = False
            return jsonify(status='healthy' if healthy else 'degraded', checks=checks), (200 if healthy else 503)

        @app.before_request
        def check_setup():
            if request.endpoint and not request.endpoint.startswith('static'):
                if request.endpoint not in ('auth.first_setup', 'cstatus'):
                    try:
                        if User.query.count() == 0:
                            session.clear()
                            return redirect(url_for('auth.first_setup'))
                    except Exception:
                        # Database unreachable: do not send users to /first-setup,
                        # which would suggest the accounts were lost.
                        db.session.rollback()
                        app.logger.exception('Database unavailable during request')
                        return (
                            'The SSJ database cannot be reached right now. Contact the Administrator.',
                            503,
                            {'Content-Type': 'text/plain; charset=utf-8'},
                        )

        def csrf_token():
            token = session.get('_csrf_token')
            if not token:
                token = secrets.token_urlsafe(32)
                session['_csrf_token'] = token
            return token

        app.jinja_env.globals['csrf_token'] = csrf_token

        @app.before_request
        def protect_csrf():
            if request.method not in ('POST', 'PUT', 'PATCH', 'DELETE'):
                return
            if app.config.get('TESTING') and not app.config.get('WTF_CSRF_ENABLED', True):
                return
            expected = session.get('_csrf_token')
            supplied = request.form.get('csrf_token') or request.headers.get('X-CSRFToken')
            if not expected or not supplied or not hmac.compare_digest(expected, supplied):
                abort(400, description='The CSRF token is invalid or missing.')

        @app.after_request
        def log_request(response):
            if request.endpoint and not request.endpoint.startswith('static'):
                app.logger.info('%s %s -> %s from %s', request.method, request.path, response.status_code, request.remote_addr)
            return response

        @app.context_processor
        def inject_global_data():
            try:
                # Dynamic: loop all registered doc types — works for any future doc type
                total_pending = sum(
                    model.query.filter_by(status='Pending Approval').count()
                    for model, _, _ in DOC_REGISTRY.values()
                    if hasattr(model, 'status')
                )
            except Exception:
                total_pending = 0

            from apps.core.auth import current_user

            def can(*menus):
                """Template check: may the logged-in account use one of these menus?"""
                user = current_user()
                return bool(user) and user.can(*menus)

            def can_do(action):
                """Template check for an element that writes to another menu's table (CROSS_MENU_ACTIONS)."""
                user = current_user()
                return bool(user) and user.can_do(action)

            def can_open(status):
                """Template check: may the logged-in account open a document with this status?"""
                user = current_user()
                return bool(user) and user.can_open_document(status)

            return dict(pending_approvals_count=total_pending, can=can, can_do=can_do, can_open=can_open,
                        MENUS=MENUS)

    return app


if __name__ == '__main__':
    app = create_app()
    debug_mode = os.environ.get('FLASK_DEBUG', 'false').lower() in ('1', 'true')
    app.run(
        debug=debug_mode,
        host=app.config['SSJ_INTERNAL_HOST'],
        port=app.config['SSJ_INTERNAL_PORT'],
    )
