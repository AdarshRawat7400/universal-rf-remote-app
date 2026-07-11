"""Desktop companion for the GitHub Universe badge universal remote.

The badge firmware intentionally has no dependency on this package.  It runs
under normal CPython and provides durable SQLite storage plus a loopback HTTP
API for synchronisation and richer device management.
"""

from .database import CompanionDatabase
from .profile import ProfileValidationError, validate_profile

__all__ = ("CompanionDatabase", "ProfileValidationError", "validate_profile")
