"""Shared canonical regex patterns.

Consolidates patterns previously duplicated across canonical modules.
Constants only — no behavioral logic. Import these instead of
re-defining the same regex inline.
"""

from __future__ import annotations

import re

# Loose RFC-5321 shape: not a full validator (real validation is harder
# than a regex). Catches obvious shape violations like missing @, missing
# domain TLD, embedded whitespace.
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

# Disallowed control characters in addresses + freeform user text.
# Excludes \t (\x09) and \n (\x0a) and \r (\x0d) which are routine.
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
