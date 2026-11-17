"""PySide6 desktop controller for a locally deployed SSJ server."""

import ctypes
import os
import smtplib
import sys
import webbrowser
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import requests
from PySide6.QtCore import Qt, QTime, QTimer
from PySide6.QtGui import QIcon, QPixmap, QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QPlainTextEdit, QScrollArea, QSpinBox, QSplitter, QTabWidget, QTimeEdit, QVBoxLayout, QWidget,
)

from controller import backup, server_process, tasks
from controller.config import DEFAULT_DATA_ROOT, DEFAULTS, env_path, load_values, save_values
from controller.deployment import (
    build_postgresql_url,
    normalize_setup_values,
    normalize_smtp_values,
    port_is_available,
    prepare_initial_setup,
    validate_internal_port,
)


SECRET_KEYS = {'SECRET_KEY', 'DATABASE_URL', 'SMTP_PASSWORD'}
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def resource_path(*parts):
    """Resolve bundled Controller assets and source-tree assets identically."""
    root = Path(getattr(sys, '_MEIPASS', PROJECT_ROOT))
    return root.joinpath(*parts)


def controller_icon():
    ico = resource_path('static', 'assets', 'images', 'logo.ico')
    if ico.is_file():
        return QIcon(str(ico))
    logo = resource_path('static', 'assets', 'images', 'logo.png')
    return QIcon(str(logo)) if logo.is_file() else QIcon()


def set_windows_app_id():
    """Give source and packaged builds their own Windows taskbar identity."""
    if sys.platform == 'win32':
        app_id = 'SSJ.ServerController.App.2026'
        try:
            from ctypes import wintypes
            f = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
            f.argtypes = [wintypes.LPCWSTR]
            f.restype = ctypes.HRESULT
            f(app_id)
        except Exception:
            pass


def apply_win32_icon(widget):
    """Explicitly send WM_SETICON to native Windows HWND to force taskbar icon refresh."""
    if sys.platform != 'win32':
        return
    try:
        hwnd = int(widget.winId())
        ico_path = str(resource_path('static', 'assets', 'images', 'logo.ico'))
        if not Path(ico_path).is_file():
            return
        LR_LOADFROMFILE = 0x00000010
        IMAGE_ICON = 1
        WM_SETICON = 0x0080
        ICON_SMALL = 0
        ICON_BIG = 1
        hicon_big = ctypes.windll.user32.LoadImageW(0, ico_path, IMAGE_ICON, 48, 48, LR_LOADFROMFILE)
        hicon_small = ctypes.windll.user32.LoadImageW(0, ico_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        if hicon_big:
            ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)
        if hicon_small:
            ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)
    except Exception:
        pass


CONTROLLER_STYLESHEET = """
QDialog#initialSetupDialog, QMainWindow {
    background: #f5f5f7;
    color: #1d1d1f;
    font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, sans-serif;
}
QFrame#brandPanel {
    background: #1e293b;
    border-radius: 0px;
}
QFrame#contentPanel {
    background: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 0px;
}
QFrame#sectionCard {
    background: #fbfbfd;
    border: 1px solid #e5e7eb;
    border-radius: 0px;
}
QLabel#eyebrow {
    color: #0071e3;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.5px;
}
QLabel#pageTitle {
    color: #1d1d1f;
    font-size: 24px;
    font-weight: 700;
}
QLabel#sectionTitle {
    color: #1d1d1f;
    font-size: 14px;
    font-weight: 700;
}
QLabel#helperText {
    color: #6e6e73;
    font-size: 11px;
}
QLineEdit {
    min-height: 38px;
    padding: 0 12px;
    background: #ffffff;
    border: 1px solid #d2d2d7;
    border-radius: 0px;
    selection-background-color: #0071e3;
}
QLineEdit:focus {
    border: 2px solid #0071e3;
}
QLineEdit:disabled {
    background: #f1f1f3;
    color: #86868b;
}
QPushButton {
    min-height: 36px;
    padding: 0 16px;
    border-radius: 0px;
    border: 1px solid #d2d2d7;
    background: #ffffff;
    color: #1d1d1f;
    font-weight: 600;
}
QPushButton:hover {
    background: #f5f5f7;
    border-color: #a1a1a6;
}
QPushButton:disabled {
    background: #f1f1f3;
    color: #8e8e93;
    border: 1px solid #d2d2d7;
}
QPushButton#primaryButton {
    min-height: 40px;
    padding: 0 20px;
    background: #0071e3;
    color: #ffffff;
    border: none;
    border-radius: 0px;
}
QPushButton#primaryButton:hover {
    background: #0077ed;
}
QPushButton#primaryButton:disabled {
    background: #9ac4f5;
    color: #ffffff;
    border: none;
}
QPushButton#secondaryAction {
    color: #0071e3;
    border-color: #b9d8f7;
}
QPushButton#secondaryAction:hover {
    background: #eff6ff;
}
QPushButton#dangerButton {
    min-height: 36px;
    padding: 0 16px;
    border-radius: 0px;
    border: 1px solid #fecdd3;
    background: #ffffff;
    color: #e11d48;
    font-weight: 600;
}
QPushButton#dangerButton:hover {
    background: #ffe4e6;
    border-color: #fda4af;

}
QPushButton#tabButtonActive {
    min-height: 30px;
    padding: 0 14px;
    background: #0071e3;
    color: #ffffff;
    border: none;
    border-radius: 8px;
    font-weight: 600;
    font-size: 12px;
}
QPushButton#tabButtonInactive {
    min-height: 30px;
    padding: 0 14px;
    background: #f5f5f7;
    color: #1d1d1f;
    border: 1px solid #d2d2d7;
    border-radius: 8px;
    font-weight: 500;
    font-size: 12px;
}
QPushButton#tabButtonInactive:hover {
    background: #e5e5ea;
}
QPlainTextEdit#logConsole {
    background: #18181b;
    color: #f4f4f5;
    border: 1px solid #27272a;
    border-radius: 12px;
    font-family: "Consolas", "Cascadia Code", "Courier New", monospace;
    font-size: 11px;
    padding: 10px;
    line-height: 1.4;
}
QCheckBox { color: #3a3a3c; spacing: 8px; }
QScrollArea { border: none; background: transparent; }
QWidget#scrollContent { background: transparent; }
"""


class InitialSetupDialog(QDialog):
    """Prepare the production configuration using approachable, explicit fields."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('initialSetupDialog')
        self.setWindowTitle('SSJ - Initial Server Setup')
        self.setWindowIcon(controller_icon())
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowMinMaxButtonsHint | Qt.WindowType.WindowCloseButtonHint)
        self.setStyleSheet(CONTROLLER_STYLESHEET)
        self.resize(1100, 780)
        self.setMinimumSize(960, 680)

        root = QHBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(18)
        root.addWidget(self._build_brand_panel())
        root.addWidget(self._build_content_panel(), 1)

        for field in (
            self.data_root, self.internal_port,
            self.pg_host, self.pg_port, self.pg_database,
            self.pg_username, self.pg_password,
            self.smtp_host, self.smtp_port, self.smtp_username,
            self.smtp_password, self.smtp_from_email,
        ):
            field.textChanged.connect(self._check_ready)
        self._check_ready()

    def showEvent(self, event):
        super().showEvent(event)
        apply_win32_icon(self)

    def _check_ready(self):
        fields = [
            self.data_root, self.internal_port,
            self.pg_host, self.pg_port, self.pg_database,
            self.pg_username, self.pg_password,
        ]
        is_ready = all(bool(f.text().strip()) for f in fields)
        self.prepare_button.setEnabled(is_ready)

    def _build_brand_panel(self):
        panel = QFrame()
        panel.setObjectName('brandPanel')
        panel.setFixedWidth(300)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(34, 38, 34, 38)

        logo_label = QLabel()
        logo = resource_path('static', 'assets', 'images', 'logo.png')
        if logo.is_file():
            pixmap = QPixmap(str(logo)).scaled(
                172, 172,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            logo_label.setPixmap(pixmap)
        logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(logo_label)
        layout.addSpacing(18)

        title = QLabel('SSJ Server\nController')
        title.setStyleSheet('color: white; font-size: 29px; font-weight: 700; line-height: 1.1;')
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        layout.addStretch()
        return panel

    def _build_content_panel(self):
        panel = QFrame()
        panel.setObjectName('contentPanel')
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(28, 25, 28, 22)
        layout.setSpacing(14)

        eyebrow = QLabel('INITIAL SETUP')
        eyebrow.setObjectName('eyebrow')
        title = QLabel('Set up the SSJ server')
        title.setObjectName('pageTitle')
        layout.addWidget(eyebrow)
        layout.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName('scrollContent')
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 5, 8, 5)
        content_layout.setSpacing(12)
        content_layout.addWidget(self._storage_section())
        content_layout.addWidget(self._database_section())
        content_layout.addWidget(self._smtp_section())
        content_layout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        buttons = QHBoxLayout()
        cancel_button = QPushButton('Cancel')
        cancel_button.clicked.connect(self.reject)
        self.prepare_button = QPushButton('Prepare configuration')
        self.prepare_button.setObjectName('primaryButton')
        self.prepare_button.clicked.connect(self.prepare)
        buttons.addStretch()
        buttons.addWidget(cancel_button)
        buttons.addWidget(self.prepare_button)
        layout.addLayout(buttons)
        return panel

    @staticmethod
    def _field(control):
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(control)
        return container

    @staticmethod
    def _section(title_text):
        card = QFrame()
        card.setObjectName('sectionCard')
        outer = QVBoxLayout(card)
        outer.setContentsMargins(18, 16, 18, 17)
        outer.setSpacing(10)
        title = QLabel(title_text)
        title.setObjectName('sectionTitle')
        outer.addWidget(title)
        form = QFormLayout()
        form.setContentsMargins(0, 4, 0, 0)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        outer.addLayout(form)
        return card, form

    @staticmethod
    def _browse_row(line_edit, callback):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        browse = QPushButton('Browse...')
        browse.setObjectName('secondaryAction')
        browse.clicked.connect(callback)
        layout.addWidget(line_edit, 1)
        layout.addWidget(browse)
        return row

    def _storage_section(self):
        card, form = self._section('Storage & local access')
        self.data_root = QLineEdit(DEFAULTS['SSJ_DATA_ROOT'])
        data_row = self._browse_row(self.data_root, self.choose_data_root)
        form.addRow('Data & backup location', self._field(data_row))

        self.internal_port = QLineEdit(DEFAULTS['SSJ_INTERNAL_PORT'])
        form.addRow('Internal port', self._field(self.internal_port))
        return card

    def _database_section(self):
        card, form = self._section('PostgreSQL')
        self.pg_host = QLineEdit('127.0.0.1')
        self.pg_port = QLineEdit('5432')
        host_row = QWidget()
        host_layout = QHBoxLayout(host_row)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.setSpacing(7)
        host_layout.addWidget(self.pg_host, 1)
        self.pg_port.setMaximumWidth(100)
        host_layout.addWidget(self.pg_port)
        form.addRow('Host / port', self._field(host_row))

        self.pg_database = QLineEdit('ssj_db')
        self.pg_username = QLineEdit('ssj_app')
        self.pg_password = QLineEdit('')
        self.pg_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.show_pg_password = QCheckBox('Show password')
        self.show_pg_password.toggled.connect(
            lambda checked: self.pg_password.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )
        password_box = QWidget()
        password_layout = QVBoxLayout(password_box)
        password_layout.setContentsMargins(0, 0, 0, 0)
        password_layout.setSpacing(5)
        password_layout.addWidget(self.pg_password)
        password_layout.addWidget(self.show_pg_password)
        form.addRow('Database name', self.pg_database)
        form.addRow('Username', self.pg_username)
        form.addRow('Password', password_box)

        test_button = QPushButton('Test PostgreSQL connection')
        test_button.setObjectName('secondaryAction')
        test_button.clicked.connect(self.test_database)
        form.addRow('', test_button)
        return card

    def _smtp_section(self):
        card, form = self._section('Email SMTP')
        self.smtp_host = QLineEdit('')
        self.smtp_host.setPlaceholderText('smtp.example.com')
        self.smtp_port = QLineEdit('587')
        smtp_host_row = QWidget()
        smtp_host_layout = QHBoxLayout(smtp_host_row)
        smtp_host_layout.setContentsMargins(0, 0, 0, 0)
        smtp_host_layout.setSpacing(7)
        smtp_host_layout.addWidget(self.smtp_host, 1)
        self.smtp_port.setMaximumWidth(100)
        smtp_host_layout.addWidget(self.smtp_port)
        form.addRow('Host / port', smtp_host_row)

        self.smtp_username = QLineEdit('')
        self.smtp_password = QLineEdit('')
        self.smtp_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.smtp_from_email = QLineEdit('')
        self.smtp_from_name = QLineEdit('SSJ')
        self.smtp_tls = QCheckBox('STARTTLS')
        self.smtp_tls.setChecked(True)
        self.smtp_ssl = QCheckBox('Direct SSL')
        encryption_row = QWidget()
        encryption_layout = QHBoxLayout(encryption_row)
        encryption_layout.setContentsMargins(0, 0, 0, 0)
        encryption_layout.addWidget(self.smtp_tls)
        encryption_layout.addWidget(self.smtp_ssl)
        encryption_layout.addStretch()
        self.smtp_tls.toggled.connect(lambda checked: self.smtp_ssl.setChecked(False) if checked else None)
        self.smtp_ssl.toggled.connect(lambda checked: self.smtp_tls.setChecked(False) if checked else None)

        form.addRow('Username', self.smtp_username)
        form.addRow('Password', self.smtp_password)
        form.addRow('Sender email', self.smtp_from_email)
        form.addRow('Sender name', self.smtp_from_name)
        form.addRow('Security', encryption_row)
        test_button = QPushButton('Send test email')
        test_button.setObjectName('secondaryAction')
        test_button.clicked.connect(self.test_smtp)
        form.addRow('', test_button)

        return card

    def choose_data_root(self):
        selected = QFileDialog.getExistingDirectory(self, 'Choose the SSJ data location', self.data_root.text())
        if selected:
            self.data_root.setText(selected)

    def database_url(self):
        return build_postgresql_url(
            self.pg_host.text(), self.pg_port.text(), self.pg_database.text(),
            self.pg_username.text(), self.pg_password.text(),
        )

    def test_database(self):
        try:
            import psycopg2
        except ImportError as error:
            QMessageBox.critical(
                self,
                'PostgreSQL driver not available',
                f'{error}\n\nInstall requirements.txt and requirements-controller.txt first.',
            )
            return
        try:
            connection = psycopg2.connect(
                host=self.pg_host.text().strip(),
                port=int(self.pg_port.text()),
                dbname=self.pg_database.text().strip(),
                user=self.pg_username.text().strip(),
                password=self.pg_password.text(),
                connect_timeout=5,
            )
            connection.close()
        except (OSError, ValueError, psycopg2.Error) as error:
            QMessageBox.critical(self, 'PostgreSQL connection failed', str(error))
            return
        QMessageBox.information(self, 'PostgreSQL', 'PostgreSQL connection succeeded.')

    def smtp_values(self):
        return normalize_smtp_values(
            self.smtp_host.text(), self.smtp_port.text(), self.smtp_username.text(),
            self.smtp_password.text(), self.smtp_from_email.text(), self.smtp_from_name.text(),
            self.smtp_tls.isChecked(), self.smtp_ssl.isChecked(),
        )

    def test_smtp(self):
        try:
            values = self.smtp_values()
            recipient = values['SMTP_FROM_EMAIL']
            if not values['SMTP_HOST'] or not recipient:
                QMessageBox.warning(self, 'SMTP not filled in', 'Fill in the SMTP host and sender email first to send a test email.')
                return
            message = EmailMessage()
            message['Subject'] = 'SSJ SMTP configuration test'
            message['From'] = f"{values['SMTP_FROM_NAME']} <{recipient}>"
            message['To'] = recipient
            message.set_content('This is a test sent by SSJ Server Controller.')
            smtp_class = smtplib.SMTP_SSL if values['SMTP_USE_SSL'] == 'true' else smtplib.SMTP
            with smtp_class(values['SMTP_HOST'], int(values['SMTP_PORT']), timeout=15) as smtp:
                if values['SMTP_USE_TLS'] == 'true':
                    smtp.starttls()
                if values['SMTP_USERNAME']:
                    smtp.login(values['SMTP_USERNAME'], values['SMTP_PASSWORD'])
                smtp.send_message(message)
        except (OSError, ValueError, smtplib.SMTPException) as error:
            QMessageBox.critical(self, 'SMTP test failed', str(error))
            return
        QMessageBox.information(self, 'SMTP', f'Test email sent to {recipient}.')

    def prepare(self):
        try:
            values = normalize_setup_values(
                self.data_root.text(),
                self.internal_port.text(),
                self.database_url(),
            )
            values.update(self.smtp_values())
            host = values['SSJ_INTERNAL_HOST']
            port = int(values['SSJ_INTERNAL_PORT'])
            if not port_is_available(host, port):
                raise ValueError(f'Internal port {port} is being used by another application.')
            prepare_initial_setup(values, env_path())
        except (OSError, ValueError) as error:
            QMessageBox.critical(self, 'Initial setup failed', str(error))
            return
        QMessageBox.information(
            self,
            'Configuration ready',
            f'Configuration: {env_path()}\n\n'
            f'Next: start the server, then open SSJ at http://<server IP>:{values["SSJ_INTERNAL_PORT"]}.',
        )
        self.accept()


class SettingsPanel(QWidget):
    def __init__(self, parent_controller=None):
        super().__init__(parent_controller)
        self.parent_controller = parent_controller
        self.values = load_values()
        self.setStyleSheet("""
            QDialog { background: #f5f5f7; color: #1d1d1f; }
            QLabel { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; font-size: 13px; font-weight: 500; color: #1d1d1f; }
            QLineEdit { border: 1px solid #d2d2d7; border-radius: 6px; padding: 8px; font-size: 13px; background: white; color: #1d1d1f; }
            QLineEdit:focus { border: 1px solid #0071e3; }
            QLineEdit[readOnly="true"] { background: #e5e5ea; color: #86868b; }
            QCheckBox { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; font-size: 13px; font-weight: 500; color: #1d1d1f; spacing: 8px; }
            QPushButton { background: #0071e3; color: white; border-radius: 6px; padding: 8px 16px; font-weight: 600; font-size: 13px; border: none; }
            QPushButton:hover { background: #0077ed; }
            QPushButton#cancel { background: #e5e5ea; color: #1d1d1f; }
            QPushButton#cancel:hover { background: #d1d1d6; }
            QTabWidget::pane { border: 1px solid #d2d2d7; border-radius: 6px; background: white; }
            QTabBar::tab { background: transparent; padding: 8px 16px; margin-right: 2px; color: #86868b; font-weight: 600; }
            QTabBar::tab:selected { color: #1d1d1f; border-bottom: 2px solid #0071e3; }
        """)
        layout = QVBoxLayout(self)
        
        info_label = QLabel(f'Config: {env_path()}')
        info_label.setStyleSheet("color: #86868b; font-size: 11px;")
        layout.addWidget(info_label)
        layout.addSpacing(10)
        
        tabs = QTabWidget()
        self.inputs = {}
        
        groups = {
            # APP_ENV stays in ssj.env (production) but is not edited here.
            'General & Network': ['SSJ_INTERNAL_HOST', 'SSJ_INTERNAL_PORT', 'TRUST_REVERSE_PROXY', 'SESSION_COOKIE_SECURE', 'SECRET_KEY', 'DATABASE_URL'],
            'Paths & Storage': ['SSJ_DATA_ROOT', 'UPLOAD_FOLDER', 'PRINT_TEMPLATE_FOLDER', 'LOG_ROOT', 'LOG_DIR', 'LOG_MAX_BYTES', 'LOG_BACKUP_COUNT', 'BACKUP_FOLDER'],
            'SMTP / Email': ['SMTP_HOST', 'SMTP_PORT', 'SMTP_USERNAME', 'SMTP_PASSWORD', 'SMTP_FROM_EMAIL', 'SMTP_FROM_NAME', 'SMTP_USE_TLS', 'SMTP_USE_SSL'],
            'Security & Auth': ['PASSWORD_RESET_OTP_TTL_SECONDS', 'PASSWORD_RESET_MAX_ATTEMPTS', 'PASSWORD_RESET_RESEND_COOLDOWN_SECONDS', 'LOGIN_MAX_ATTEMPTS', 'LOGIN_WINDOW_SECONDS', 'PASSWORD_RESET_MAX_REQUESTS', 'PASSWORD_RESET_WINDOW_SECONDS'],
            'Advanced': ['CONTROLLER_SERVICE_NAME', 'CONTROLLER_STATUS_URL']
        }
        
        read_only_keys = {
            'SSJ_DATA_ROOT', 'UPLOAD_FOLDER', 'PRINT_TEMPLATE_FOLDER', 'LOG_ROOT', 'LOG_DIR',
            'BACKUP_FOLDER', 'CONTROLLER_STATUS_URL',
        }
        
        BOOLEAN_KEYS = {'TRUST_REVERSE_PROXY', 'SESSION_COOKIE_SECURE', 'SMTP_USE_TLS', 'SMTP_USE_SSL'}

        for tab_name, keys in groups.items():
            form = QFormLayout()
            form.setVerticalSpacing(12)
            form.setHorizontalSpacing(20)
            form.setContentsMargins(20, 20, 20, 20)
            
            for key in keys:
                raw_val = str(self.values.get(key, DEFAULTS.get(key, ''))).strip()
                if key in BOOLEAN_KEYS or raw_val.lower() in ('true', 'false'):
                    is_checked = raw_val.lower() in ('true', '1', 'yes')
                    field = QCheckBox('True' if is_checked else 'False')
                    field.setChecked(is_checked)
                    field.toggled.connect(lambda chk, c=field: c.setText('True' if chk else 'False'))
                    if key in read_only_keys:
                        field.setEnabled(False)
                    self.inputs[key] = field
                    form.addRow(key, field)
                else:
                    field = QLineEdit(raw_val)
                    if key in SECRET_KEYS:
                        field.setEchoMode(QLineEdit.EchoMode.Password)
                    if key in read_only_keys:
                        field.setReadOnly(True)
                    self.inputs[key] = field
                    form.addRow(key, field)
                
            tab_widget = QWidget()
            tab_widget.setLayout(form)
            
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(tab_widget)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            
            tabs.addTab(scroll, tab_name)

        if 'SMTP_USE_TLS' in self.inputs and 'SMTP_USE_SSL' in self.inputs:
            tls_cb = self.inputs['SMTP_USE_TLS']
            ssl_cb = self.inputs['SMTP_USE_SSL']
            tls_cb.toggled.connect(lambda checked: ssl_cb.setChecked(False) if checked else None)
            ssl_cb.toggled.connect(lambda checked: tls_cb.setChecked(False) if checked else None)

        layout.addWidget(tabs)
        layout.addSpacing(10)
        
        buttons = QHBoxLayout()
        test_button = QPushButton('Test SMTP')
        test_button.clicked.connect(self.test_smtp)
        save_button = QPushButton('Save Configuration')
        save_button.clicked.connect(self.save)
        buttons.addWidget(test_button)
        buttons.addStretch()
        buttons.addWidget(save_button)
        layout.addLayout(buttons)

    def current_values(self):
        # Start from the file, not the values read at startup: the Automation tab saves its own keys.
        values = load_values()
        for key, field in self.inputs.items():
            if isinstance(field, QCheckBox):
                values[key] = 'true' if field.isChecked() else 'false'
            else:
                values[key] = field.text().strip()
        return values

    def save(self):
        values = self.current_values()
        try:
            port = validate_internal_port(values['SSJ_INTERNAL_PORT'])
            smtp_values = normalize_smtp_values(
                values.get('SMTP_HOST'), values.get('SMTP_PORT'), values.get('SMTP_USERNAME'),
                values.get('SMTP_PASSWORD'), values.get('SMTP_FROM_EMAIL'),
                values.get('SMTP_FROM_NAME'),
                values.get('SMTP_USE_TLS', '').lower() in ('1', 'true', 'yes'),
                values.get('SMTP_USE_SSL', '').lower() in ('1', 'true', 'yes'),
            )
        except ValueError as error:
            QMessageBox.warning(self, 'Configuration invalid', str(error))
            return
        if values.get('APP_ENV', 'production').lower() == 'production' and not values.get('DATABASE_URL'):
            QMessageBox.warning(self, 'Configuration incomplete', 'DATABASE_URL PostgreSQL is required for production.')
            return
        values['SSJ_INTERNAL_PORT'] = str(port)
        values['CONTROLLER_STATUS_URL'] = f'http://127.0.0.1:{port}/cstatus'
        values.update(smtp_values)
        try:
            save_values(values)
        except OSError as error:
            QMessageBox.critical(self, 'Unable to save settings', str(error))
            return
        if server_process.is_running(values):
            QMessageBox.information(
                self,
                'Settings Saved',
                'Configuration saved.\n\nNote: the server is running. Click "Stop Server" and then "Start Server" in the controller to apply the changes.'
            )
        else:
            QMessageBox.information(self, 'Settings Saved', 'Configuration has been successfully saved.')
        if self.parent_controller:
            self.parent_controller.refresh()

    def test_smtp(self):
        values = self.current_values()
        try:
            values.update(normalize_smtp_values(
                values.get('SMTP_HOST'), values.get('SMTP_PORT'), values.get('SMTP_USERNAME'),
                values.get('SMTP_PASSWORD'), values.get('SMTP_FROM_EMAIL'),
                values.get('SMTP_FROM_NAME'),
                values.get('SMTP_USE_TLS', '').lower() in ('1', 'true', 'yes'),
                values.get('SMTP_USE_SSL', '').lower() in ('1', 'true', 'yes'),
            ))
        except ValueError as error:
            QMessageBox.warning(self, 'SMTP incomplete', str(error))
            return
        recipient = values.get('SMTP_FROM_EMAIL')
        if not values.get('SMTP_HOST') or not recipient:
            QMessageBox.warning(self, 'SMTP not filled in', 'Fill in the SMTP host and sender email first to send a test email.')
            return
        try:
            message = EmailMessage()
            message['Subject'] = 'SSJ SMTP configuration test'
            message['From'] = f"{values.get('SMTP_FROM_NAME', 'SSJ')} <{recipient}>"
            message['To'] = recipient
            message.set_content('This is a test sent by SSJ Server Controller.')
            smtp_class = smtplib.SMTP_SSL if values.get('SMTP_USE_SSL', '').lower() == 'true' else smtplib.SMTP
            with smtp_class(values['SMTP_HOST'], int(values.get('SMTP_PORT', '587')), timeout=15) as smtp:
                if values.get('SMTP_USE_TLS', '').lower() == 'true':
                    smtp.starttls()
                if values.get('SMTP_USERNAME'):
                    smtp.login(values['SMTP_USERNAME'], values.get('SMTP_PASSWORD', ''))
                smtp.send_message(message)
            QMessageBox.information(self, 'SMTP', f'Test email sent to {recipient}.')
        except (OSError, ValueError, smtplib.SMTPException) as error:
            QMessageBox.critical(self, 'SMTP test failed', str(error))


class AutomationPanel(QWidget):
    """Automation: start the server at boot and make backups automatically."""

    def __init__(self, parent_controller=None):
        super().__init__(parent_controller)
        self.parent_controller = parent_controller
        values = load_values()
        self.setStyleSheet("""
            QLabel { font-size: 13px; color: #1d1d1f; }
            QLabel#helperText { color: #6e6e73; font-size: 11px; }
            QLabel#sectionTitle { font-size: 14px; font-weight: 700; }
            QLineEdit, QComboBox, QSpinBox, QTimeEdit { border: 1px solid #d2d2d7; border-radius: 6px; padding: 6px; font-size: 13px; background: white; min-height: 22px; }
            QCheckBox { font-size: 13px; color: #1d1d1f; spacing: 8px; }
            QPushButton { background: #0071e3; color: white; border-radius: 6px; padding: 8px 16px; font-weight: 600; font-size: 13px; border: none; }
            QPushButton:hover { background: #0077ed; }
            QPushButton#browse { background: #e5e5ea; color: #1d1d1f; }
        """)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        def helper(text):
            label = QLabel(text)
            label.setObjectName('helperText')
            label.setWordWrap(True)
            return label

        title = QLabel('Automatic server start')
        title.setObjectName('sectionTitle')
        layout.addWidget(title)
        self.autostart = QCheckBox('Start the server automatically when the PC boots (no login needed)')
        self.autostart.setChecked(values.get('AUTOSTART_ENABLED') == 'true')
        layout.addWidget(self.autostart)
        layout.addSpacing(12)

        title = QLabel('Automatic backup')
        title.setObjectName('sectionTitle')
        layout.addWidget(title)
        form = QFormLayout()
        form.setVerticalSpacing(10)
        self.mode = QComboBox()
        for key, label in backup.BACKUP_MODES.items():
            self.mode.addItem(label, key)
        self.mode.setCurrentIndex(max(0, self.mode.findData(values.get('BACKUP_MODE', 'off'))))
        form.addRow('Backup schedule', self.mode)

        self.weekday = QComboBox()
        for number, name in enumerate(backup.WEEKDAYS, start=1):
            self.weekday.addItem(name, number)
        self.weekday.setCurrentIndex(max(0, self.weekday.findData(int(values.get('BACKUP_WEEKDAY', 1)))))
        self.day = QSpinBox()
        self.day.setRange(1, backup.MAX_MONTH_DAY)
        self.day.setValue(int(values.get('BACKUP_DAY', 1)))
        self.month = QComboBox()
        for number, name in enumerate(backup.MONTHS, start=1):
            self.month.addItem(name, number)
        self.month.setCurrentIndex(max(0, self.month.findData(int(values.get('BACKUP_MONTH', 1)))))
        self.time = QTimeEdit(QTime.fromString(values.get('BACKUP_TIME', '12:00'), 'HH:mm'))
        self.time.setDisplayFormat('HH:mm')
        self.rows = {}
        for key, label, widget in (('weekday', 'Weekday', self.weekday), ('day', 'Day', self.day),
                                   ('month', 'Month', self.month), ('time', 'Time', self.time)):
            form.addRow(label, widget)
            self.rows[key] = (form.labelForField(widget), widget)

        self.run_missed = QCheckBox('Run as soon as possible when a scheduled backup was missed (e.g. the PC was off)')
        self.run_missed.setChecked(backup.run_missed(values))
        form.addRow('', self.run_missed)

        self.folder = QLineEdit(values.get('SNAPSHOT_FOLDER', ''))
        self.folder.setPlaceholderText(str(backup.snapshot_folder({})))
        browse = QPushButton('Browse...')
        browse.setObjectName('browse')
        browse.clicked.connect(self.choose_folder)
        folder_row = QWidget()
        folder_layout = QHBoxLayout(folder_row)
        folder_layout.setContentsMargins(0, 0, 0, 0)
        folder_layout.addWidget(self.folder, 1)
        folder_layout.addWidget(browse)
        form.addRow('Backup folder', folder_row)
        layout.addLayout(form)

        self.retention = helper('')
        layout.addWidget(self.retention)
        self.status = QLabel('')
        self.status.setWordWrap(True)
        layout.addSpacing(6)
        layout.addWidget(self.status)
        layout.addStretch()

        apply_button = QPushButton('Save & Apply')
        apply_button.clicked.connect(self.apply)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(apply_button)
        layout.addLayout(buttons)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        self.mode.currentIndexChanged.connect(self._update_fields)
        self._update_fields()

    def _update_fields(self):
        mode = self.mode.currentData()
        visible = {
            'weekday': mode == 'weekly',
            'day': mode in ('monthly', 'yearly'),
            'month': mode == 'yearly',
            'time': mode in backup.SCHEDULED_MODES,
        }
        for key, (label, widget) in self.rows.items():
            label.setVisible(visible[key])
            widget.setVisible(visible[key])
        self.run_missed.setVisible(mode in backup.SCHEDULED_MODES)
        if mode == 'off':
            self.retention.setText('Automatic backup is off. Manual snapshots can still be made in the Database tab.')
        else:
            self.retention.setText(
                f'Automatic backups older than {backup.RETENTION_LABELS[mode]} are deleted automatically. '
                'Manual and pre-import snapshots are never deleted.'
            )

    def choose_folder(self):
        selected = QFileDialog.getExistingDirectory(self, 'Choose the backup folder', self.folder.text() or self.folder.placeholderText())
        if selected:
            self.folder.setText(selected)

    def refresh_status(self):
        values = load_values()
        status = backup.read_status(values)
        if not status:
            last = 'Last backup: never.'
        else:
            when = status.get('finished_at', '-').replace('T', ' ')
            result = 'succeeded' if status.get('ok') else 'FAILED'
            last = f'Last backup: {when} — {result}. {status.get("message", "")}'
        upcoming = backup.next_backup_time(values, datetime.now())
        if upcoming:
            last += f'\nNext scheduled: {upcoming.strftime("%d-%m-%Y %H:%M")}'
        elif values.get('BACKUP_MODE') == 'on_start':
            last += '\nNext backup: when the server starts.'
        self.status.setText(last)

    def apply(self):
        try:
            backup_values = backup.normalize_backup_settings(
                self.mode.currentData(), self.time.time().toString('HH:mm'), self.weekday.currentData(),
                self.day.value(), self.month.currentData(), self.run_missed.isChecked(), self.folder.text(),
            )
        except ValueError as error:
            QMessageBox.warning(self, 'Invalid settings', str(error))
            return
        values = load_values()
        values.update(backup_values)
        values['AUTOSTART_ENABLED'] = 'true' if self.autostart.isChecked() else 'false'
        try:
            save_values(values)
            tasks.write_task_files(values)
        except OSError as error:
            QMessageBox.critical(self, 'Could not save', str(error))
            return
        QMessageBox.information(
            self, 'Administrator Permission',
            'Windows will ask for administrator permission once to register the tasks '
            'and open the server port in the firewall.',
        )
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            ok, message = tasks.apply_elevated()
        finally:
            QApplication.restoreOverrideCursor()
        if ok:
            QMessageBox.information(self, 'Automation Applied', message)
        else:
            QMessageBox.warning(self, 'Automation Not Applied', message)
        self.refresh_status()


class ControllerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('SSJ Server Controller')
        self.setWindowIcon(controller_icon())
        self.setStyleSheet(CONTROLLER_STYLESHEET)
        self.setMinimumSize(400, 680)
        self.resize(400, 680)
        self.active_log = 'app'

        central = QWidget()
        self.setCentralWidget(central)
        
        # Use QSplitter to allow resizing the log panel
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # Left Panel (Controls & Status)
        self.left_panel = self._build_left_panel()
        self.splitter.addWidget(self.left_panel)
        
        # Right Panel (Logs)
        self.right_panel = self._build_right_panel()
        self.splitter.addWidget(self.right_panel)
        
        # Hide right panel by default
        self.right_panel.hide()
        
        # Add splitter to central widget
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.splitter)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(4000)
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        apply_win32_icon(self)

    # Closing the window leaves the server running; Stop Server is the only way to stop it.

    def _build_left_panel(self):
        panel = QFrame()
        panel.setObjectName('brandPanel')
        panel.setFixedWidth(400)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(24, 28, 24, 28)
        layout.setSpacing(14)

        # 1. Branding
        logo_label = QLabel()
        logo = resource_path('static', 'assets', 'images', 'logo.png')
        if logo.is_file():
            pixmap = QPixmap(str(logo)).scaled(
                80, 80,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            logo_label.setPixmap(pixmap)
        logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(logo_label)

        title = QLabel('SSJ Server')
        title.setStyleSheet('color: white; font-size: 22px; font-weight: 700;')
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        
        layout.addSpacing(10)

        # 2. Status Card (Moved from main panel)
        card_app = QFrame()
        card_app.setObjectName('sectionCard')
        c1_lay = QVBoxLayout(card_app)
        c1_lay.setContentsMargins(14, 10, 14, 10)
        c1_lay.setSpacing(4)
        c1_title = QLabel('Operational Status')
        c1_title.setStyleSheet('color: #6e6e73; font-size: 11px; font-weight: 700;')
        self.app_status_badge = QLabel('CHECKING...')
        self.app_status_badge.setStyleSheet('font-size: 16px; font-weight: 700; color: #1d1d1f;')
        self.app_status_sub = QLabel('Checking...')
        self.app_status_sub.setStyleSheet('color: #6e6e73; font-size: 11px;')
        c1_lay.addWidget(c1_title)
        c1_lay.addWidget(self.app_status_badge)
        c1_lay.addWidget(self.app_status_sub)
        layout.addWidget(card_app)

        layout.addSpacing(10)

        # 3. Core Controls
        self.btn_start = QPushButton('Start Server')
        self.btn_start.setObjectName('primaryButton')
        self.btn_start.clicked.connect(lambda: self.service_action('start'))
        layout.addWidget(self.btn_start)

        self.btn_stop = QPushButton('Stop Server')
        self.btn_stop.setObjectName('dangerButton')
        self.btn_stop.clicked.connect(lambda: self.service_action('stop'))
        layout.addWidget(self.btn_stop)

        layout.addSpacing(10)

        # 4. Access Links
        self.btn_open_http = QPushButton('Open SSJ')
        self.btn_open_http.clicked.connect(self.open_http)
        layout.addWidget(self.btn_open_http)

        # 5. Utilities
        util_layout = QHBoxLayout()
        self.btn_settings = QPushButton('Settings')
        self.btn_settings.clicked.connect(self.open_settings)
        self.btn_refresh = QPushButton('Refresh')
        self.btn_refresh.clicked.connect(self.refresh)
        util_layout.addWidget(self.btn_settings)
        util_layout.addWidget(self.btn_refresh)
        layout.addLayout(util_layout)

        layout.addStretch()
        
        # 6. Log Toggle
        self.btn_toggle_log = QPushButton('Show Panel >>')
        self.btn_toggle_log.clicked.connect(self.toggle_logs)
        layout.addWidget(self.btn_toggle_log)

        footer = QLabel('SSJ v1.0 - Standalone')
        footer.setStyleSheet('color: rgba(255, 255, 255, 0.5); font-size: 11px;')
        footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(footer)

        return panel

    def _build_right_panel(self):
        panel = QFrame()
        panel.setObjectName('contentPanel')
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.right_tabs = QTabWidget()
        self.right_tabs.setObjectName('rightTabs')
        self.right_tabs.setStyleSheet("""
            QTabWidget::pane { border: none; }
            QTabBar::tab { background: transparent; padding: 8px 16px; margin-right: 2px; color: #86868b; font-weight: 600; font-size: 14px; }
            QTabBar::tab:selected { color: #1d1d1f; border-bottom: 2px solid #0071e3; }
        """)

        # Tab 1: Logs
        log_panel = QWidget()
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(10, 10, 10, 10)
        log_layout.setSpacing(10)

        log_header = QHBoxLayout()
        log_title = QLabel('Console Log')
        log_title.setObjectName('pageTitle')
        log_header.addWidget(log_title)
        log_header.addStretch()

        btn_open_logs = QPushButton('Open Log Folder')
        btn_open_logs.clicked.connect(self.open_logs_dir)
        btn_open_logs.setStyleSheet("background: #e5e5ea; color: #1d1d1f; border-radius: 6px; padding: 6px 12px; font-weight: 600;")
        log_header.addWidget(btn_open_logs)

        log_layout.addLayout(log_header)

        self.logs = QPlainTextEdit(readOnly=True)
        self.logs.setObjectName('logConsole')
        self.logs.setPlaceholderText('Server activity logs will load here...')
        self.logs.setStyleSheet('font-family: Consolas, monospace; font-size: 12px; background: #1e1e1e; color: #d4d4d4; border-radius: 8px; padding: 10px;')
        log_layout.addWidget(self.logs, 1)

        self.right_tabs.addTab(log_panel, "Server Logs")

        # Tab 2: Database Management
        db_panel = QWidget()
        db_layout = QVBoxLayout(db_panel)
        db_layout.setContentsMargins(20, 20, 20, 20)
        db_layout.setSpacing(15)

        db_title = QLabel('Database & Data Management')
        db_title.setStyleSheet('font-size: 18px; font-weight: bold; color: #1d1d1f;')
        db_layout.addWidget(db_title)

        def db_card(title, description, button_text, color, handler, title_color='#1d1d1f'):
            card = QFrame()
            card.setStyleSheet('QFrame { background: white; border-radius: 8px; border: 1px solid #d2d2d7; }')
            card_layout = QVBoxLayout(card)
            label = QLabel(title)
            label.setStyleSheet(f'font-weight: bold; color: {title_color}; border: none;')
            desc = QLabel(description)
            desc.setStyleSheet('color: #6e6e73; border: none;')
            desc.setWordWrap(True)
            button = QPushButton(button_text)
            button.setStyleSheet(
                f'QPushButton {{ background: {color}; color: white; padding: 8px 16px; border-radius: 6px; font-weight: bold; }}'
                'QPushButton:disabled { background: #a1a1a6; }'
            )
            button.clicked.connect(handler)
            card_layout.addWidget(label)
            card_layout.addWidget(desc)
            card_layout.addWidget(button)
            db_layout.addWidget(card)
            return button

        self.btn_db_export = db_card(
            'Export Snapshot',
            'Saves ALL data (accounts, clients, documents, HO, schedules, reports, settings) '
            'with every uploaded photo to one .ssjbak file in the Documents\\SSJ Snapshots folder.',
            'Export Snapshot Now', '#0071e3', self.action_export_snapshot,
        )
        self.btn_db_import = db_card(
            'Import Snapshot',
            'Replaces the whole database with the contents of a .ssjbak file (photos included). The tables are rebuilt '
            'from the latest models; new columns get their default/empty value. The current data is snapshotted first as a safeguard.',
            'Choose File & Import', '#5856d6', self.action_import_snapshot,
        )

        open_folder = QPushButton('Open Snapshot Folder')
        open_folder.clicked.connect(self.open_snapshot_dir)
        db_layout.addWidget(open_folder)

        db_layout.addStretch()
        self.right_tabs.addTab(db_panel, "Database")

        # Tab 3: Automation (auto-start and automatic backup)
        self.automation_panel = AutomationPanel(self)
        self.right_tabs.addTab(self.automation_panel, "Automation")

        # Tab 4: Settings
        self.settings_panel = SettingsPanel(self)
        self.right_tabs.addTab(self.settings_panel, "Configuration")

        layout.addWidget(self.right_tabs)
        return panel

    # ---------- Database & snapshot actions ----------
    def snapshot_dir(self):
        return backup.snapshot_folder(self.values())

    def open_snapshot_dir(self):
        folder = self.snapshot_dir()
        folder.mkdir(parents=True, exist_ok=True)
        if sys.platform == 'win32':
            os.startfile(folder)

    def _server_is_running(self):
        """Started from this window, another one or the boot task, or anything answering on the port."""
        values = self.values()
        if server_process.is_running(values):
            return True
        try:
            requests.get(values.get('CONTROLLER_STATUS_URL', DEFAULTS['CONTROLLER_STATUS_URL']), timeout=1.0)
            return True
        except requests.RequestException:
            return False

    def _require_stopped_server(self, action_name):
        if self._server_is_running():
            QMessageBox.warning(
                self, 'Server Running',
                f'Stop the SSJ server first before {action_name}.',
            )
            return False
        return True

    def _run_busy(self, button, busy_text, work):
        """Run a blocking DB task while the button shows progress; returns (ok, result)."""
        from controller.snapshot import SnapshotError
        original_text = button.text()
        buttons = (self.btn_db_export, self.btn_db_import)
        for item in buttons:
            item.setEnabled(False)
        button.setText(busy_text)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            return True, work()
        except SnapshotError as error:
            return False, str(error)
        except Exception as error:  # surface anything unexpected instead of crashing the GUI
            return False, f'An error occurred: {error}'
        finally:
            QApplication.restoreOverrideCursor()
            button.setText(original_text)
            for item in buttons:
                item.setEnabled(True)

    def action_export_snapshot(self):
        from controller.snapshot import export_snapshot
        values = self.values()
        ok, result = self._run_busy(
            self.btn_db_export, 'Creating Snapshot...',
            lambda: export_snapshot(values.get('DATABASE_URL'), values.get('UPLOAD_FOLDER'), self.snapshot_dir()),
        )
        if ok:
            QMessageBox.information(self, 'Snapshot Created', f'Snapshot saved to:\n{result}')
        else:
            QMessageBox.critical(self, 'Snapshot Failed', result)

    def action_import_snapshot(self):
        from controller.snapshot import SnapshotError, SNAPSHOT_SUFFIX, export_snapshot, import_snapshot, plan_import
        if not self._require_stopped_server('importing a snapshot'):
            return
        path, _ = QFileDialog.getOpenFileName(
            self, 'Choose a Snapshot File', str(self.snapshot_dir()), f'SSJ Snapshot (*{SNAPSHOT_SUFFIX})',
        )
        if not path:
            return
        try:
            plan = plan_import(path)
        except SnapshotError as error:
            QMessageBox.critical(self, 'Invalid Snapshot', str(error))
            return
        if plan.blocking:
            QMessageBox.critical(self, 'Import Cannot Continue', plan.summary())
            return
        reply = QMessageBox.question(
            self, 'Confirm Import',
            f'{plan.summary()}\n\nThe WHOLE current database will be replaced with this snapshot '
            '(a snapshot of the current data is made automatically first). Continue?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        values = self.values()

        def work():
            # The lock keeps a scheduled backup from reading the database while it is rebuilt.
            with backup.snapshot_lock(values):
                safety = export_snapshot(values.get('DATABASE_URL'), values.get('UPLOAD_FOLDER'),
                                         self.snapshot_dir(), label='pre-import')
                summary = import_snapshot(values.get('DATABASE_URL'), path, values.get('UPLOAD_FOLDER'))
            return f'{summary}\n\nSnapshot of the previous data:\n{safety}'

        ok, result = self._run_busy(self.btn_db_import, 'Importing...', work)
        if ok:
            QMessageBox.information(self, 'Import Succeeded', result)
        else:
            QMessageBox.critical(self, 'Import Failed', result)

    def toggle_logs(self):
        if self.right_panel.isHidden():
            self.right_panel.show()
            self.btn_toggle_log.setText('Hide Panel <<')
            self.resize(1000, 680)
            self.splitter.setSizes([400, 600])
        else:
            self.right_panel.hide()
            self.btn_toggle_log.setText('Show Panel >>')
            self.resize(400, 680)

    def open_logs_dir(self):
        values = self.values()
        log_root = Path(values.get('LOG_ROOT', DEFAULT_DATA_ROOT / 'logs'))
        log_root.mkdir(parents=True, exist_ok=True)
        if sys.platform == 'win32':
            os.startfile(log_root)

    def values(self):
        return load_values()

    def service_action(self, action):
        values = self.values()
        if action == 'start':
            if server_process.is_running(values):
                QMessageBox.information(self, 'Server Running', 'The SSJ server is already running.')
            elif self._server_is_running():
                QMessageBox.warning(
                    self, 'Port In Use',
                    f'Port {values.get("SSJ_INTERNAL_PORT")} is already used by another program. '
                    'Close that program or change the port in Configuration.',
                )
            else:
                server_process.start_background(values)
        elif action == 'stop':
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                stopped, message = server_process.stop(values)
            finally:
                QApplication.restoreOverrideCursor()
            if not stopped:
                QMessageBox.critical(self, 'Stop Failed', message)
        self.refresh()

    def open_http(self):
        values = self.values()
        internal_port = values.get('SSJ_INTERNAL_PORT', DEFAULTS['SSJ_INTERNAL_PORT'])
        webbrowser.open(f'http://127.0.0.1:{internal_port}')

    def refresh(self):
        values = self.values()
        url = values.get('CONTROLLER_STATUS_URL', DEFAULTS['CONTROLLER_STATUS_URL'])
        internal_port = values.get('SSJ_INTERNAL_PORT', DEFAULTS['SSJ_INTERNAL_PORT'])

        try:
            response = requests.get(url, timeout=1.0)
            payload = response.json()
            healthy = response.ok and payload.get('status') == 'healthy'

            if healthy:
                self.app_status_badge.setText('ONLINE')
                self.app_status_badge.setStyleSheet('font-size: 16px; font-weight: 700; color: #10b981;')
                self.app_status_sub.setText(f'Port {internal_port} - Server running and ready for requests')
            else:
                self.app_status_badge.setText('DEGRADED')
                self.app_status_badge.setStyleSheet('font-size: 16px; font-weight: 700; color: #f59e0b;')
                self.app_status_sub.setText('Some system components have problems (check the log)')
        except (requests.RequestException, ValueError):
            self.app_status_badge.setText('OFFLINE')
            self.app_status_badge.setStyleSheet('font-size: 16px; font-weight: 700; color: #ef4444;')
            self.app_status_sub.setText('The server is not running')

        state = server_process.server_state(values)
        if state:
            source = 'automatically at boot' if state.get('started_by') == server_process.STARTED_BY_TASK else 'from the Controller'
            if self.app_status_badge.text() == 'OFFLINE':
                self.app_status_sub.setText(f'The server is starting ({source})...')
            else:
                self.app_status_sub.setText(f'{self.app_status_sub.text()}\nStarted {source} at {state.get("started_at", "-")}')
        self.btn_start.setEnabled(state is None)
        self.btn_stop.setEnabled(state is not None)
        self.automation_panel.refresh_status()

        self.refresh_logs(values)

    def refresh_logs(self, values):
        app_log_file = Path(values.get('LOG_DIR', DEFAULTS['LOG_DIR'])) / 'ssj-app.log'

        combined_lines = []

        if app_log_file.exists():
            try:
                import json
                with app_log_file.open('rb') as stream:
                    stream.seek(max(0, app_log_file.stat().st_size - 30_000))
                    content = stream.read().decode('utf-8', errors='replace')
                    for line in content.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            asctime = data.get('asctime', '')
                            levelname = data.get('levelname', '')
                            name = data.get('name', '')
                            message = data.get('message', '')
                            combined_lines.append(f"[{asctime}] [{levelname}] [{name}] {message}")
                        except Exception:
                            combined_lines.append(line)
            except OSError:
                pass

        if not combined_lines:
            self.logs.setPlainText('No activity has been logged yet.\nApplication logs appear here automatically while the server runs.')
            return

        full_text = '\n'.join(combined_lines[-250:])
        if self.logs.toPlainText() != full_text:
            self.logs.setPlainText(full_text)
            self.logs.verticalScrollBar().setValue(self.logs.verticalScrollBar().maximum())

    def open_settings(self):
        if self.right_panel.isHidden():
            self.toggle_logs()
        self.right_tabs.setCurrentWidget(self.settings_panel)

def main():
    if '--serve' in sys.argv:
        # We are being launched as the background server process by the controller
        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if root_dir not in sys.path:
            sys.path.insert(0, root_dir)
        import serve
        serve.main()
        return

    # Modes without a window: Task Scheduler (--autostart, --backup), the installer
    # (--stop, --unregister-tasks) and the elevated Automation helper (--register-tasks).
    if '--autostart' in sys.argv:
        started_by = server_process.STARTED_BY_CONTROLLER if '--from-controller' in sys.argv else server_process.STARTED_BY_TASK
        sys.exit(server_process.supervise(load_values(), started_by))
    if '--backup' in sys.argv:
        sys.exit(backup.run_backup(load_values()))
    if '--stop' in sys.argv:
        sys.exit(0 if server_process.stop(load_values())[0] else 1)
    if '--register-tasks' in sys.argv:
        sys.exit(tasks.register_from_files())
    if '--unregister-tasks' in sys.argv:
        sys.exit(tasks.unregister_all())

    force_setup = '--setup' in sys.argv
    qt_arguments = [argument for argument in sys.argv if argument not in ('--setup', '--serve')]
    set_windows_app_id()
    app = QApplication(qt_arguments)
    app.setApplicationName('SSJ Server Controller')
    app.setWindowIcon(controller_icon())
    app.setStyle('Fusion')
    font = QFont("SF Pro", 10)
    font.insertSubstitution("SF Pro", "San Francisco")
    font.insertSubstitution("San Francisco", "Helvetica Neue")
    font.insertSubstitution("Helvetica Neue", "Segoe UI")
    app.setFont(font)
    if force_setup or not env_path().exists():
        setup = InitialSetupDialog()
        if not setup.exec():
            return
    window = ControllerWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
