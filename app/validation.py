"""Validation helpers shared by request schemas and per-recipient validation."""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any

from email_validator import EmailNotValidError, validate_email

MAX_EMAIL_LENGTH = 254


def check_text(value: Any, field: str, max_length: int) -> tuple[str | None, list[str]]:
    """Validate a free-text field. Returns (cleaned_value, errors).

    Rules: must be a string, whitespace is collapsed, non-empty, bounded length, no control
    characters, and only characters ReportLab's built-in fonts can render (Windows-1252).
    """
    if not isinstance(value, str):
        return None, [f"{field} must be a string"]
    cleaned = " ".join(value.split())
    if not cleaned:
        return None, [f"{field} must not be empty"]
    errors: list[str] = []
    if len(cleaned) > max_length:
        errors.append(f"{field} must be at most {max_length} characters")
    if any(unicodedata.category(ch).startswith("C") for ch in cleaned):
        errors.append(f"{field} must not contain control or invisible characters")
    try:
        cleaned.encode("cp1252")
    except UnicodeEncodeError:
        errors.append(f"{field} contains characters the certificate font cannot render (Latin script only)")
    return (None if errors else cleaned), errors


@dataclass(frozen=True)
class RecipientValidation:
    name: str | None
    email: str | None
    errors: list[str]
    display_name: str  # safe, truncated text to persist even when invalid
    display_email: str

    @property
    def is_valid(self) -> bool:
        return not self.errors


def _display(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else repr(value)
    return text[:limit]


def validate_recipient(raw: Any, max_name_length: int) -> RecipientValidation:
    """Validate one recipient independently of all others."""
    if not isinstance(raw, dict):
        return RecipientValidation(
            None, None, ["recipient must be an object with 'name' and 'email'"], _display(raw, 255), ""
        )
    raw_name, raw_email = raw.get("name"), raw.get("email")
    errors: list[str] = []

    name, name_errors = check_text(raw_name, "name", max_name_length)
    errors.extend(name_errors)

    email: str | None = None
    if not isinstance(raw_email, str):
        errors.append("email must be a string")
    else:
        candidate = raw_email.strip()
        if not candidate:
            errors.append("email must not be empty")
        elif len(candidate) > MAX_EMAIL_LENGTH:
            errors.append(f"email must be at most {MAX_EMAIL_LENGTH} characters")
        else:
            try:
                email = validate_email(candidate, check_deliverability=False).normalized
            except EmailNotValidError as exc:
                errors.append(f"email is not valid: {exc}")

    return RecipientValidation(
        name=name,
        email=email,
        errors=errors,
        display_name=name or _display(raw_name, 255),
        display_email=email or _display(raw_email, 320),
    )
