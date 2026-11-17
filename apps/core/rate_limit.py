from datetime import timedelta

from flask import current_app

from apps.models import db, AuthRateLimit, wib_now


def _window_config(action):
    if action == 'login':
        return (
            current_app.config['LOGIN_MAX_ATTEMPTS'],
            current_app.config['LOGIN_WINDOW_SECONDS'],
        )
    return (
        current_app.config['PASSWORD_RESET_MAX_REQUESTS'],
        current_app.config['PASSWORD_RESET_WINDOW_SECONDS'],
    )


def check(action, subject):
    """Return seconds remaining when a subject is currently blocked, else zero."""
    limit, window_seconds = _window_config(action)
    entry = AuthRateLimit.query.filter_by(action=action, subject=subject).first()
    if not entry:
        return 0

    now = wib_now()
    elapsed = (now - entry.window_started).total_seconds()
    if elapsed >= window_seconds:
        return 0
    if entry.attempts < limit:
        return 0
    return max(1, int(window_seconds - elapsed))


def record(action, subject):
    """Record an attempt in the configured rolling-fixed window."""
    _limit, window_seconds = _window_config(action)
    now = wib_now()
    entry = AuthRateLimit.query.filter_by(action=action, subject=subject).first()
    if not entry:
        db.session.add(AuthRateLimit(
            action=action, subject=subject, window_started=now,
            last_attempt=now, attempts=1,
        ))
        db.session.commit()
        return

    if (now - entry.window_started) >= timedelta(seconds=window_seconds):
        entry.window_started = now
        entry.attempts = 1
    else:
        entry.attempts += 1
    entry.last_attempt = now
    db.session.commit()


def clear(action, subject):
    AuthRateLimit.query.filter_by(action=action, subject=subject).delete()
    db.session.commit()
