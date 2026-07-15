"""Conservative filters for text written by automatic memory capture."""

from __future__ import annotations

import re


_PRIVATE_KEY = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----", re.IGNORECASE)
_AUTHORIZATION = re.compile(
    r"\bauthorization\b[\"']?\s*[:=]\s*[\"']?\s*"
    r"(?:bearer|basic)?\s*[A-Za-z0-9._~+/=-]{6,}", re.IGNORECASE)
_BEARER = re.compile(
    r"\bbearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"\b(?:password|passwd|passphrase|api[ _-]*key|access[ _-]*token|"
    r"refresh[ _-]*token|auth(?:entication)?[ _-]*token|client[ _-]*secret|"
    r"secret[ _-]*key|private[ _-]*key|bearer(?:[ _-]*token)?)"
    r"\b[\"']?\s*(?:is\b|[:=])\s*[\"']?\s*\S{3,}",
    re.IGNORECASE,
)
_CREDENTIAL_OPTION = re.compile(
    r"--(?:password|passphrase|api-key|access-token|auth-token)"
    r"(?:=|\s+)\S{3,}", re.IGNORECASE)
_TOKEN_SIGNATURE = re.compile(
    r"(?:\bsk-(?:proj-)?[A-Za-z0-9_-]{12,}|"
    r"\bgh[pousr]_[A-Za-z0-9]{20,}|"
    r"\bAKIA[0-9A-Z]{16}\b|"
    r"\bAIza[0-9A-Za-z_-]{30,}|"
    r"\bxox[baprs]-[A-Za-z0-9-]{12,})")


def contains_sensitive_content(text):
    """True for obvious credential material, without exposing the value."""
    text = str(text or "")
    if not text:
        return False
    return any(pattern.search(text) for pattern in (
        _PRIVATE_KEY,
        _AUTHORIZATION,
        _BEARER,
        _CREDENTIAL_ASSIGNMENT,
        _CREDENTIAL_OPTION,
        _TOKEN_SIGNATURE,
    ))


def safe_excerpt(text, limit=200):
    """A compact excerpt, or empty when the source may contain a secret."""
    text = str(text or "")
    if contains_sensitive_content(text):
        return ""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "..."
