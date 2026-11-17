"""Date formatting shared by routes, exports, and Jinja templates."""

from datetime import date, datetime

DAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
DAY_ABBREVIATIONS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
MONTH_NAMES = [
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December',
]


def format_date(value, include_day=False):
    """Format as ``13 October 2026`` (optionally prefixed with the weekday)."""
    if not value:
        return ''
    if isinstance(value, datetime):
        value = value.date()
    if not isinstance(value, date):
        return str(value)
    text = f'{value.day:02d} {MONTH_NAMES[value.month - 1]} {value.year}'
    if include_day:
        return f'{DAY_NAMES[value.weekday()]}, {text}'
    return text


def format_datetime(value):
    """Format as ``13 October 2026 14:05``."""
    if not value:
        return ''
    if not isinstance(value, datetime):
        return format_date(value)
    return f'{format_date(value)} {value:%H:%M}'


def format_short(value):
    """Compact ``13 Oct`` for tight table cells."""
    if not value:
        return ''
    return f'{value.day:02d} {MONTH_NAMES[value.month - 1][:3]}'


def format_month_year(year, month):
    return f'{MONTH_NAMES[month - 1]} {year}'


def register_template_filters(app):
    app.add_template_filter(lambda d, include_day=True: format_date(d, include_day), 'format_date')
    app.add_template_filter(format_datetime, 'format_datetime')
    app.add_template_filter(format_short, 'format_short')
