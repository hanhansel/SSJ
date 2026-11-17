"""
apps/core/auth.py — Centralized authentication & authorization decorators.

All blueprints import from here instead of defining their own copies.
Usage:
    from apps.core.auth import login_required, office_required, menu_required, list_menu_required

An office account may open a page when it has one of the page's menus
(models.MENUS); a technician account only uses the phone pages.  An element that
writes to another menu's table needs all the menus in models.CROSS_MENU_ACTIONS.
"""

from functools import wraps
from flask import g, session, redirect, request, url_for, flash
from apps.models import db, User, MENUS, CROSS_MENU_ACTIONS, MENU_DOCUMENTS, MENU_STORAGE, ROLE_TECHNICIAN


def _get_active_user():
    """Load current permissions from the database, not from a stale session."""
    user_id = session.get('user_id')
    user = db.session.get(User, user_id) if user_id else None
    if not user or not user.is_active:
        session.clear()
        g.current_user = None
        return None
    session['user_name'] = user.name
    session['role'] = user.access_label  # shown under the name in the sidebar
    g.current_user = user
    return user


def current_user():
    """The logged-in, active user of this request, or None."""
    if 'current_user' not in g:
        if session.get('user_id'):
            _get_active_user()
        else:
            g.current_user = None  # e.g. the login page: leave the session (and its CSRF token) alone
    return g.current_user


def _role_home(user):
    """Return a safe landing page that the current account may access."""
    if user.role == ROLE_TECHNICIAN:
        return url_for('technician.dashboard')
    if user.is_office:
        return url_for('system.dashboard')
    # An account type this version does not know (e.g. from an old database):
    # log it out instead of bouncing between pages it may not open.
    return url_for('auth.logout')


def login_required(f):
    """Requires any logged-in user."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _get_active_user():
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated


def _deny(user, message):
    flash(message, 'danger')
    return redirect(_role_home(user))


def office_required(f):
    """Requires an Office account, whatever its menus."""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = _get_active_user()
        if not user:
            return redirect(url_for('auth.login'))
        if not user.is_office:
            return _deny(user, 'Access denied: this page is for Office accounts only.')
        return f(*args, **kwargs)
    return decorated


def _check_menus(menus):
    user = _get_active_user()
    if not user:
        return redirect(url_for('auth.login'))
    if not user.can(*menus):
        if not user.is_office:
            return _deny(user, 'Access denied: this page is for Office accounts only.')
        names = ' or '.join(MENUS[menu] for menu in menus)
        return _deny(user, f'Access denied: your account has no access to the {names} menu.')
    return None


def menu_required(*menus):
    """Requires an office account with at least one of the menus."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            return _check_menus(menus) or f(*args, **kwargs)
        return decorated
    return decorator


def action_required(action):
    """Requires every menu of a CROSS_MENU_ACTIONS entry (an element that writes to another menu's table)."""
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            user = _get_active_user()
            if not user:
                return redirect(url_for('auth.login'))
            if not user.can_do(action):
                names = ' and '.join(MENUS[menu] for menu in CROSS_MENU_ACTIONS[action][0])
                return _deny(user, f'Access denied: this feature needs access to the {names} menus.')
            return f(*args, **kwargs)
        return decorated
    return decorator


def deny_unless_can_open(doc):
    """None when the current user may open this document, else a redirect away from it."""
    user = current_user()
    if user and user.can_open_document(doc.status):
        return None
    return _deny(user, 'Access denied: active documents can only be opened with access to the Documents menu.')


def list_menu_required(f):
    """Document lists serve both menus: ?storage=1 is Data Storage, otherwise Documents."""
    @wraps(f)
    def decorated(*args, **kwargs):
        menu = MENU_STORAGE if request.args.get('storage') == '1' else MENU_DOCUMENTS
        return _check_menus((menu,)) or f(*args, **kwargs)
    return decorated


def technician_required(f):
    """Requires Technician role."""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = _get_active_user()
        if not user:
            return redirect(url_for('auth.login'))
        if user.role != ROLE_TECHNICIAN:
            flash('Access denied: this page is for Technicians only.', 'danger')
            return redirect(_role_home(user))
        return f(*args, **kwargs)
    return decorated
