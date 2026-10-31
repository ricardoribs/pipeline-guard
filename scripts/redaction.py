"""Pure redaction primitives shared by diagnosis runtime boundaries."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

REDACTED_SECRET = "[REDACTED_SECRET]"
REDACTED_EMAIL = "[REDACTED_EMAIL]"
REDACTED_PRIVATE_KEY = "[REDACTED_PRIVATE_KEY]"

_SENSITIVE_FIELD_SUFFIXES = (
    "apikey",
    "authorization",
    "chave",
    "chavedeapi",
    "cookie",
    "credencial",
    "credenciais",
    "credential",
    "password",
    "privatekey",
    "segredo",
    "secret",
    "senha",
    "token",
)
_SENSITIVE_FIELDS = frozenset(
    {
        "accountkey",
        "auth",
        "connectionstring",
        "credentials",
        "databaseurl",
        "email",
        "emailaddress",
        "passwd",
        "proxyauthorization",
        "pwd",
        "sessionid",
        "sharedaccesssignature",
    }
)

_SECRET_LABEL_BASE = (
    r"account[\t _-]*key|api[\t _-]*key|access[\t _-]*token|"
    r"auth[\t _-]*token|bearer[\t _-]*token|auth|"
    r"cookie|set[\t _-]*cookie|session[\t _-]*(?:cookie|id|token)|"
    r"shared[\t _-]*access[\t _-]*signature|"
    r"chave[\t _-]*(?:de[\t _-]*)?api|chave|credencia(?:l|is)|segredo|senha|"
    r"client[\t _-]*secret|connection[\t _-]*string|credential|credentials|"
    r"database[\t _-]*url|encryption[\t _-]*key|pass|password|passwd|"
    r"private[\t _-]*key|pwd|"
    r"refresh[\t _-]*token|secret|secret[\t _-]*access[\t _-]*key|"
    r"secret[\t _-]*key|signing[\t _-]*secret|token|webhook[\t _-]*secret"
)
_SECRET_LABEL = rf"(?:(?:[a-z0-9]+[_-])*(?:{_SECRET_LABEL_BASE}))"
_SENSITIVE_FIELD_RE = re.compile(_SECRET_LABEL, re.IGNORECASE)

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----.*?"
    r"(?:-----END(?: [A-Z0-9]+)* PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
_URL_USERINFO_RE = re.compile(
    r"(?P<scheme>\b[a-z][a-z0-9+.-]*://)(?P<userinfo>[^\s/?#]+)@",
    re.IGNORECASE,
)
_AUTHORIZATION_RE = re.compile(
    r"(?P<prefix>\b(?:Proxy-)?Authorization[\"']?[\t ]*[:=][\t ]*)"
    r"(?:(?P<quote>[\"'])(?P<quoted>(?:\\.|(?!(?P=quote))[^\\\r\n])*)(?P=quote)"
    r"|(?P<unquoted>[^\r\n]+))",
    re.IGNORECASE,
)
_COOKIE_HEADER_RE = re.compile(
    r"(?P<prefix>\b(?:Set-)?Cookie[\"']?[\t ]*:[\t ]*)"
    r"(?:(?P<quote>[\"'])(?P<quoted>(?:\\.|(?!(?P=quote))[^\\\r\n])*)(?P=quote)"
    r"|(?P<unquoted>[^\r\n]+))",
    re.IGNORECASE,
)
_SHARED_ACCESS_SIGNATURE_RE = re.compile(
    r"(?P<prefix>\bShared[\t _-]*Access[\t _-]*Signature[\t ]*[:=][\t ]*)"
    r"(?:(?P<quote>[\"'])(?P<quoted>(?:\\.|(?!(?P=quote))[^\\\r\n])*)(?P=quote)"
    r"|(?P<unquoted>[^\s,;}]+))",
    re.IGNORECASE,
)
_BEARER_TOKEN_RE = re.compile(
    r"(?P<scheme>\bBearer)[\t ]+[A-Za-z0-9._~+/=-]+",
    re.IGNORECASE,
)
_QUOTED_ASSIGNMENT_RE = re.compile(
    rf"(?P<prefix>[\"']?(?:{_SECRET_LABEL})[\"']?\s*[:=]\s*)"
    r"(?P<quote>[\"'])(?P<value>[^\r\n]*?)(?P=quote)",
    re.IGNORECASE,
)
_UNQUOTED_ASSIGNMENT_RE = re.compile(
    rf"(?P<prefix>[\"']?(?:{_SECRET_LABEL})[\"']?\s*[:=]\s*)"
    r"(?![\"'])(?P<value>[^\s,;&#}]+)",
    re.IGNORECASE,
)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_PREFIXED_TOKEN_RE = re.compile(
    r"\b(?:"
    r"AKIA[A-Z0-9]{16}|ASIA[A-Z0-9]{16}|"
    r"ATATT3[A-Za-z0-9_=-]{20,}|"
    r"AIza[A-Za-z0-9_-]{35}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"glpat-[A-Za-z0-9_-]{20,}|"
    r"dapi[0-9A-Fa-f]{32}|"
    r"do[opr]_v1_[0-9A-Fa-f]{20,}|"
    r"sk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}|"
    r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}"
    r")\b"
)
_AWS_SECRET_ACCESS_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9/+=])"
    r"(?=[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=]))"
    r"(?=[A-Za-z0-9/+=]{0,39}[A-Z])"
    r"(?=[A-Za-z0-9/+=]{0,39}[a-z])"
    r"(?=[A-Za-z0-9/+=]{0,39}[G-Zg-z/+])"
    r"[A-Za-z0-9/+=]{40}"
)
_SLACK_WEBHOOK_RE = re.compile(
    r"\bhttps://hooks\.slack(?:-gov)?\.com/services/"
    r"[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+",
    re.IGNORECASE,
)
_HTTP_URL_RE = re.compile(r"""\bhttps?://[^\s<>"']+""", re.IGNORECASE)
_URL_SIG_RE = re.compile(
    r"(?P<prefix>(?:^|&)(?:sig|x-amz-signature|x-goog-signature)=)[^&]*",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9.!#$%&'*+=?^_`{|}~-])"
    r"[A-Za-z0-9.!#$%&'*+=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
    r"(?![A-Za-z0-9-])"
)


def _normalize_field_name(field_name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", field_name.casefold())


def _is_sensitive_field(field_name: str) -> bool:
    normalized = _normalize_field_name(field_name)
    return (
        normalized in _SENSITIVE_FIELDS
        or normalized.endswith(_SENSITIVE_FIELD_SUFFIXES)
        or _SENSITIVE_FIELD_RE.fullmatch(field_name.strip()) is not None
    )


def _redact_url_userinfo(match: re.Match[str]) -> str:
    userinfo = match.group("userinfo")
    if ":" not in userinfo:
        return f"{match.group('scheme')}{REDACTED_SECRET}@"
    username, _password = userinfo.split(":", maxsplit=1)
    return f"{match.group('scheme')}{username}:{REDACTED_SECRET}@"


def _redact_quoted_assignment(match: re.Match[str]) -> str:
    quote = match.group("quote")
    return f"{match.group('prefix')}{quote}{REDACTED_SECRET}{quote}"


def _redact_unquoted_assignment(match: re.Match[str]) -> str:
    return f"{match.group('prefix')}{REDACTED_SECRET}"


def _redact_authorization(match: re.Match[str]) -> str:
    quote = match.group("quote") or ""
    value = match.group("quoted") if quote else match.group("unquoted")
    value = value.strip()
    scheme = re.match(r"(?P<scheme>[A-Za-z][A-Za-z0-9_-]*)[\t ]+.+", value)
    replacement = (
        f"{scheme.group('scheme')} {REDACTED_SECRET}" if scheme is not None else REDACTED_SECRET
    )
    return f"{match.group('prefix')}{quote}{replacement}{quote}"


def _redact_sensitive_header(match: re.Match[str]) -> str:
    quote = match.group("quote") or ""
    return f"{match.group('prefix')}{quote}{REDACTED_SECRET}{quote}"


def _redact_url_signature(match: re.Match[str]) -> str:
    url, fragment_separator, fragment = match.group().partition("#")
    address, query_separator, query = url.partition("?")
    if not query_separator:
        return match.group()
    query = _URL_SIG_RE.sub(
        lambda signature: f"{signature.group('prefix')}{REDACTED_SECRET}", query
    )
    return f"{address}?{query}{fragment_separator}{fragment}"


def redact_text(text: str) -> str:
    """Return ``text`` with recognized secrets and email addresses masked."""
    redacted = _PRIVATE_KEY_RE.sub(REDACTED_PRIVATE_KEY, text)
    redacted = _URL_USERINFO_RE.sub(_redact_url_userinfo, redacted)
    redacted = _SLACK_WEBHOOK_RE.sub(REDACTED_SECRET, redacted)
    redacted = _HTTP_URL_RE.sub(_redact_url_signature, redacted)
    redacted = _AUTHORIZATION_RE.sub(_redact_authorization, redacted)
    redacted = _COOKIE_HEADER_RE.sub(_redact_sensitive_header, redacted)
    redacted = _SHARED_ACCESS_SIGNATURE_RE.sub(_redact_sensitive_header, redacted)
    redacted = _BEARER_TOKEN_RE.sub(
        lambda match: f"{match.group('scheme')} {REDACTED_SECRET}", redacted
    )
    redacted = _QUOTED_ASSIGNMENT_RE.sub(_redact_quoted_assignment, redacted)
    redacted = _UNQUOTED_ASSIGNMENT_RE.sub(_redact_unquoted_assignment, redacted)
    redacted = _JWT_RE.sub(REDACTED_SECRET, redacted)
    redacted = _PREFIXED_TOKEN_RE.sub(REDACTED_SECRET, redacted)
    redacted = _AWS_SECRET_ACCESS_KEY_RE.sub(REDACTED_SECRET, redacted)
    return _EMAIL_RE.sub(REDACTED_EMAIL, redacted)


def _redact_sensitive_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return {
            redact_text(str(key)): _redact_sensitive_value(nested_value)
            for key, nested_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_sensitive_value(item) for item in value)
    return REDACTED_SECRET


def redact_payload(value: Any) -> Any:
    """Recursively sanitize a JSON-like value while preserving its containers."""
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for raw_key, nested_value in value.items():
            key = str(raw_key)
            safe_key = redact_text(key)
            if _is_sensitive_field(key):
                redacted[safe_key] = _redact_sensitive_value(nested_value)
            else:
                redacted[safe_key] = redact_payload(nested_value)
        return redacted
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_payload(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value
