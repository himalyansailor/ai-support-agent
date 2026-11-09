"""Validators shared by request schemas."""

import re
from typing import Annotated

from pydantic import AfterValidator, BeforeValidator, Field

# WHATWG-style address: ASCII local part without quotes, angle brackets, commas, whitespace or controls,
# and a dotted hostname. Anything that could smuggle extra recipients or headers is rejected.
_EMAIL = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_{|}~-]+"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)


def _strip(v: object) -> object:
    return v.strip() if isinstance(v, str) else v


def _valid_email(v: str) -> str:
    if not _EMAIL.fullmatch(v):
        raise ValueError("invalid email address")
    return v


# Surrounding whitespace is trimmed first; storage still lower-cases (normalise_email).
EmailAddress = Annotated[str, BeforeValidator(_strip), Field(max_length=320), AfterValidator(_valid_email)]

# A display name: trimmed, so whitespace-only is rejected as empty.
DisplayName = Annotated[str, BeforeValidator(_strip), Field(min_length=1, max_length=255)]
