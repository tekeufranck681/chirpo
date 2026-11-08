"""Role-based access control: who may run which action (D14).

For now this only defines the roles; the `check(actor, action)` logic arrives in M3.5.
"""

from enum import StrEnum


class Role(StrEnum):
    """A member's role inside one workspace. Ranked: admin > maintainer > member."""

    ADMIN = "admin"
    MAINTAINER = "maintainer"
    MEMBER = "member"
