from datetime import timedelta
from email.message import EmailMessage
import hashlib
import hmac
import secrets
import smtplib

from flask import Blueprint, current_app, render_template, redirect, url_for, request, flash, session
from werkzeug.security import check_password_hash, generate_password_hash
from apps.models import db, PasswordResetOTP, Role, User, wib_now, ALL_MENUS, ROLE_OFFICE, ROLE_TECHNICIAN
from apps.core import rate_limit

auth_bp = Blueprint('auth', __name__)


def _clear_reset_session():
    for key in ('reset_user_id', 'reset_otp_id', 'reset_verified'):
        session.pop(key, None)


def _masked_email(email):
    local, _, domain = email.partition('@')
    return f"{local[:2]}{'*' * max(1, len(local) - 2)}@{domain}"


def _otp_is_expired():
    reset = db.session.get(PasswordResetOTP, session.get('reset_otp_id'))
    return not reset or wib_now() >= reset.expires_at


def _rate_limit_subject(value):
    """Tie limits to source/identifier without storing an email or IP in plain text."""
    remote_addr = request.remote_addr or 'unknown'
    raw_subject = f'{remote_addr}:{(value or "").strip().lower()}'
    return hashlib.sha256(raw_subject.encode('utf-8')).hexdigest()


def _send_reset_otp(email, otp):
    config = current_app.config
    if not config['SMTP_HOST'] or not config['SMTP_FROM_EMAIL']:
        raise RuntimeError('SMTP is not configured yet.')

    message = EmailMessage()
    message['Subject'] = 'SSJ password reset code'
    message['From'] = f"{config['SMTP_FROM_NAME']} <{config['SMTP_FROM_EMAIL']}>"
    message['To'] = email
    minutes = max(1, config['PASSWORD_RESET_OTP_TTL_SECONDS'] // 60)
    message.set_content(
        f"Your password reset OTP code: {otp}\n\n"
        f"This code is valid for {minutes} minutes. Do not share it with anyone."
    )

    smtp_class = smtplib.SMTP_SSL if config['SMTP_USE_SSL'] else smtplib.SMTP
    with smtp_class(config['SMTP_HOST'], config['SMTP_PORT'], timeout=15) as smtp:
        if config['SMTP_USE_TLS']:
            smtp.starttls()
        if config['SMTP_USERNAME']:
            smtp.login(config['SMTP_USERNAME'], config['SMTP_PASSWORD'])
        smtp.send_message(message)


def _issue_otp(user):
    otp = f"{secrets.randbelow(1_000_000):06d}"
    salt = secrets.token_hex(16)
    otp_hash = hashlib.sha256(f'{salt}:{otp}'.encode()).hexdigest()
    PasswordResetOTP.query.filter(PasswordResetOTP.expires_at < wib_now()).delete()
    PasswordResetOTP.query.filter_by(user_id=user.id).delete()
    reset = PasswordResetOTP(
        user_id=user.id,
        otp_salt=salt,
        otp_hash=otp_hash,
        expires_at=wib_now() + timedelta(seconds=current_app.config['PASSWORD_RESET_OTP_TTL_SECONDS']),
    )
    _send_reset_otp(user.email, otp)
    db.session.add(reset)
    db.session.commit()
    session['reset_user_id'] = user.id
    session['reset_otp_id'] = reset.id
    session.pop('reset_verified', None)


@auth_bp.route('/first-setup', methods=['GET', 'POST'])
def first_setup():
    if User.query.count() > 0:
        flash('Initial setup has already been completed. Please log in.', 'warning')
        return redirect(url_for('auth.login'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        local_part, _, domain = email.partition('@')
        # Collect every problem at once so each field shows its own message.
        errors = {}
        if not username:
            errors['username'] = 'Username is required.'
        if not email:
            errors['email'] = 'Email is required.'
        elif not local_part or not domain:
            # Internal domains without a dot (e.g. admin@ssj) are allowed.
            errors['email'] = 'Please enter a valid email address.'
        if not password:
            errors['password'] = 'Password is required.'
        elif len(password) < 8:
            errors['password'] = 'Password must be at least 8 characters.'
        if errors:
            return render_template(
                'accounts/first_setup.html', errors=errors,
                form_username=username, form_email=email,
            ), 400

        office_role = Role.query.filter_by(code=ROLE_OFFICE).first()
        if not office_role:
            # Re-render instead of redirecting so the typed values are kept.
            flash('Default roles are not available yet. Please run the schema migrations first.', 'danger')
            return render_template(
                'accounts/first_setup.html', form_username=username, form_email=email,
            ), 503
        new_admin = User(
            name=username,
            email=email,
            password=generate_password_hash(password),
            role_id=office_role.id,
            menus=ALL_MENUS,  # the first account manages everything, Access Management included
        )
        db.session.add(new_admin)
        db.session.commit()

        flash('The first account was created with access to every menu. Please log in.', 'success')
        return redirect(url_for('auth.login'))

    return render_template('accounts/first_setup.html')


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if User.query.count() == 0:
        return redirect(url_for('auth.first_setup'))

    if request.method == 'POST':
        identifier = request.form.get('username', '').strip().lower()
        password = request.form.get('password', '')
        limit_subject = _rate_limit_subject(identifier)
        remaining = rate_limit.check('login', limit_subject)
        if remaining:
            flash(f'Too many login attempts. Try again in {remaining // 60 + 1} minutes.', 'danger')
            return render_template('accounts/login.html'), 429

        # Exact, case-insensitive match (no LIKE wildcards). Login by name is only
        # accepted when exactly one account carries that name.
        user = User.query.filter(db.func.lower(User.email) == identifier).first()
        if not user:
            same_name = User.query.filter(db.func.lower(User.name) == identifier).limit(2).all()
            user = same_name[0] if len(same_name) == 1 else None
        if user:
            if not user.is_active:
                rate_limit.record('login', limit_subject)
                flash('Your account has been deactivated. Please contact the Administrator.', 'danger')
            elif check_password_hash(user.password, password) and user.role not in (ROLE_OFFICE, ROLE_TECHNICIAN):
                # e.g. a Superadmin/Admin/Finance account left in a database from before per-menu access
                flash('This account type is no longer recognised. Ask an Access Management admin to create a new account.', 'danger')
            elif check_password_hash(user.password, password):
                rate_limit.clear('login', limit_subject)
                session['user_id'] = user.id
                session['user_name'] = user.name
                session['role'] = user.access_label  # shown under the name in the sidebar

                if user.role == ROLE_TECHNICIAN:
                    return redirect(url_for('technician.dashboard'))
                return redirect(url_for('system.dashboard'))
            else:
                rate_limit.record('login', limit_subject)
                flash('Incorrect email or password.', 'danger')
        else:
            rate_limit.record('login', limit_subject)
            flash('Incorrect email or password.', 'danger')

    return render_template('accounts/login.html')


@auth_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))


@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        limit_subject = _rate_limit_subject(email)
        remaining = rate_limit.check('password_reset', limit_subject)
        if remaining:
            flash(f'Too many reset requests. Try again in {remaining // 60 + 1} minutes.', 'danger')
            return render_template('accounts/forgot_password.html'), 429

        user = User.query.filter(db.func.lower(User.email) == email).first()
        _clear_reset_session()
        rate_limit.record('password_reset', limit_subject)

        # Do not disclose whether an email address is registered.
        if not user or not user.is_active:
            flash('If the email is registered, an OTP code has been sent.', 'success')
            return redirect(url_for('auth.login'))

        try:
            _issue_otp(user)
        except (OSError, smtplib.SMTPException, RuntimeError):
            current_app.logger.exception('Unable to send password reset OTP')
            flash('The OTP code could not be sent. Check the SMTP settings or contact the Administrator.', 'danger')
            return render_template('accounts/forgot_password.html'), 503
        return redirect(url_for('auth.verify_otp'))

    return render_template('accounts/forgot_password.html')


@auth_bp.route('/verify-otp', methods=['GET', 'POST'])
def verify_otp():
    user_id = session.get('reset_user_id')
    reset = db.session.get(PasswordResetOTP, session.get('reset_otp_id'))
    user = db.session.get(User, user_id) if user_id else None
    if not user or not reset or reset.user_id != user.id:
        _clear_reset_session()
        flash('Please start the password reset again.', 'warning')
        return redirect(url_for('auth.forgot_password'))

    if request.method == 'POST':
        otp = request.form.get('otp', '').strip()
        if _otp_is_expired():
            _clear_reset_session()
            flash('The OTP has expired. Please request a new code.', 'warning')
            return redirect(url_for('auth.forgot_password'))

        reset.attempts += 1
        actual_hash = hashlib.sha256(f"{reset.otp_salt}:{otp}".encode()).hexdigest()
        if hmac.compare_digest(actual_hash, reset.otp_hash):
            db.session.delete(reset)
            db.session.commit()
            session['reset_verified'] = True
            session.pop('reset_otp_id', None)
            return redirect(url_for('auth.reset_password'))

        if reset.attempts >= current_app.config['PASSWORD_RESET_MAX_ATTEMPTS']:
            db.session.delete(reset)
            db.session.commit()
            _clear_reset_session()
            flash('Too many OTP attempts. Please request a new code.', 'danger')
            return redirect(url_for('auth.forgot_password'))
        db.session.commit()
        flash('Incorrect OTP code.', 'danger')

    remaining_seconds = max(0, int((reset.expires_at - wib_now()).total_seconds()))
    return render_template('accounts/verify_otp.html', masked_email=_masked_email(user.email), remaining_seconds=remaining_seconds)


@auth_bp.route('/resend-otp', methods=['POST'])
def resend_otp():
    user_id = session.get('reset_user_id')
    reset = db.session.get(PasswordResetOTP, session.get('reset_otp_id'))
    user = db.session.get(User, user_id) if user_id else None
    if not user or not reset:
        flash('Please start the password reset again.', 'warning')
        return redirect(url_for('auth.forgot_password'))
    age_seconds = (wib_now() - reset.created_at).total_seconds()
    cooldown = current_app.config['PASSWORD_RESET_RESEND_COOLDOWN_SECONDS']
    if age_seconds < cooldown:
        flash(f'Wait {int(cooldown - age_seconds) + 1} seconds before resending the OTP.', 'warning')
        return redirect(url_for('auth.verify_otp'))
    try:
        _issue_otp(user)
    except (OSError, smtplib.SMTPException, RuntimeError):
        current_app.logger.exception('Unable to resend password reset OTP')
        flash('The OTP code could not be sent. Check the SMTP settings.', 'danger')
        return redirect(url_for('auth.verify_otp'))
    flash('A new OTP code has been sent.', 'success')
    return redirect(url_for('auth.verify_otp'))


@auth_bp.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    user_id = session.get('reset_user_id')
    user = db.session.get(User, user_id) if user_id else None
    if not user or not session.get('reset_verified'):
        _clear_reset_session()
        flash('OTP verification is required before resetting the password.', 'warning')
        return redirect(url_for('auth.forgot_password'))

    if request.method == 'POST':
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        if len(password) < 8:
            flash('The password must be at least 8 characters.', 'danger')
        elif password != confirm_password:
            flash('The password confirmation does not match.', 'danger')
        else:
            user.password = generate_password_hash(password)
            db.session.commit()
            _clear_reset_session()
            flash('Password reset successfully. Please log in.', 'success')
            return redirect(url_for('auth.login'))
    return render_template('accounts/reset_password.html')
