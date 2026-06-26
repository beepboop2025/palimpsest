"""Censorwatch — the *velocity* leg of DDTI (Deletion-Driven Tipping Index).

This subpackage actively observes public Chinese social/financial posts, archives
them on first sight, then re-fetches them on a schedule to detect deletions that
*we* observe directly (as opposed to the selectivity/novelty legs in
``processors/ddti_index.py``, which read China Digital Times' already-published
deletion list).

Design constraints (see README.md for the full architecture):
- **Feature-flagged**: nothing here runs unless ``CENSORWATCH_ENABLED`` is set.
  When the flag is unset, the Celery beat entries don't exist and the FastAPI
  router isn't mounted — production collectors are untouched.
- **Isolated storage**: three dedicated tables (``censored_posts``,
  ``post_deletions``, ``deletion_velocity_snapshots``). Never writes to the
  production ``articles``/``economic_data`` tables.
- **Defensive by default**: an ambiguous fetch (403/timeout/anti-bot) is
  ``UNKNOWN``, never a deletion. See ``detector.py`` for the state machine.
"""

__version__ = "0.0.1"
