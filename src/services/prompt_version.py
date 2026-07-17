"""Resolve application-owned version metadata for the effective AI prompt."""

from __future__ import annotations

import hashlib
import re


_MAX_VERSION_LENGTH = 64
_VERSION_TOKEN = rf"[A-Za-z0-9][A-Za-z0-9._:+-]{{0,{_MAX_VERSION_LENGTH - 1}}}"
_SAFE_VERSION = re.compile(rf"^{_VERSION_TOKEN}$")
_VERSION_PATTERNS = (
    re.compile(
        rf"^\s*[\"']prompt_version[\"']\s*[:：=]\s*"
        rf"[\"'](?P<version>{_VERSION_TOKEN})[\"']\s*,?\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
    re.compile(
        rf"^\s*prompt_version\s*[:：=]\s*"
        rf"[\"']?(?P<version>{_VERSION_TOKEN})[\"']?\s*,?\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
    re.compile(
        rf"^\s*版本\s*[:：]\s*"
        rf"[\"']?(?P<version>{_VERSION_TOKEN})[\"']?\s*$",
        re.MULTILINE,
    ),
)


def _validated_version(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > _MAX_VERSION_LENGTH:
        return None
    return candidate if _SAFE_VERSION.fullmatch(candidate) else None


def resolve_canonical_prompt_version(
    prompt_text: str,
    *,
    explicit_version: str | None = None,
) -> str:
    """Return explicit, declared, or content-addressed prompt metadata."""
    if not isinstance(prompt_text, str):
        raise TypeError("prompt_text must be a string")

    if explicit_version is not None:
        validated = _validated_version(explicit_version)
        if validated is None:
            raise ValueError("ai_prompt_version must use safe version characters")
        return validated

    for pattern in _VERSION_PATTERNS:
        match = pattern.search(prompt_text)
        if match:
            return match.group("version")

    digest = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:12]
    return f"sha256:{digest}"
