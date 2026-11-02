"""Fixed paper CFD day policy, not an exchange calendar or a fee schedule.

The cutoff is deliberately UTC and does not claim to replicate every market's
close or broker rollover. Closed markets exit at the next executable quote.
"""
from datetime import datetime, time, timezone
import math

from .performance import recorded_profile

ENTRY_CUTOFF = time(20, 30)
EXIT_CUTOFF = time(20, 45)


def _instant(now_ms):
    if isinstance(now_ms, bool) or not isinstance(now_ms, (int, float)) or not math.isfinite(now_ms):
        raise ValueError('Horloge intrajournalière invalide.')
    return datetime.fromtimestamp(now_ms / 1000, timezone.utc)


def cfd_entry_allowed(profile, now_ms):
    if profile != 'expanded_v2':
        return True
    now = _instant(now_ms)
    return now.weekday() < 5 and now.time().replace(tzinfo=None) < ENTRY_CUTOFF


def cfd_exit_due(position, now_ms):
    if position.get('kind') != 'cfd' or recorded_profile(position) != 'expanded_v2':
        return False
    try:
        opened = datetime.fromisoformat(position['opened_at'])
        if opened.tzinfo is None:
            return False
        deadline = datetime.combine(opened.astimezone(timezone.utc).date(), EXIT_CUTOFF, timezone.utc)
        return _instant(now_ms) >= deadline
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return False
