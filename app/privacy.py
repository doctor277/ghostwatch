from __future__ import annotations

import copy
import ipaddress
import os
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal

RedactionLevel = Literal["standard", "strict"]

_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_WINDOWS_QUOTED_PATH = re.compile(
    r"(?i)([\"'])(?:[A-Z]:[\\/]|\\\\)[^\r\n\"'<>|]+\1"
)
_WINDOWS_SPACED_PATH = re.compile(
    r"(?i)(?:[A-Z]:[\\/]|\\\\)[^\r\n\"'<>|]*?"
    r"(?=(?:\s+[A-Z][A-Z0-9_.-]*\s*[=:])|[,;|]|$)"
)
_WINDOWS_PATH = re.compile(r"(?i)(?:[A-Z]:[\\/]|\\\\)[^\s\"'<>|,;]+")
_WINDOWS_ACCOUNT = re.compile(
    r"(?i)(?<![\w\\])(?:[A-Z0-9_.-]+)\\[A-Z0-9_.@$-]+(?![\w\\])"
)
_WINDOWS_SID = re.compile(r"(?i)(?<![\w-])S-1-(?:\d+-){1,14}\d+(?![\w-])")
_UNIX_HOME_PATH = re.compile(r"(?i)(?<![\w/])/(?:home|users)/[^/\s\"'<>]+(?:/[^\r\n\t\"'<>]*)?")
_EVENT_ACCOUNT_FIELD = re.compile(
    r"(?i)(\b(?:user(?:name)?|account(?:name)?|subjectusername|targetusername)\s*[=:]\s*)"
    r"[^,;|<>\r\n]*?(?=(?:\s+[A-Z][A-Z0-9_.-]*\s*[=:])|[,;|]|$)"
)
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?(?![\d.])")
_BRACKETED_IPV6 = re.compile(r"\[[0-9A-Fa-f:.%]+\](?::\d{1,5})?")
_BARE_IPV6 = re.compile(
    r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}"
    r"(?:%[\w.-]+)?(?![0-9A-Fa-f:])"
)

_USER_KEYS = {"user", "username", "user_name", "owner", "account", "analyst"}
_PATH_KEYS = {
    "path",
    "file_path",
    "executable",
    "home",
    "home_directory",
    "watch_path",
}
_ARGUMENT_KEYS = {
    "args",
    "arguments",
    "cmdline",
    "command_line",
    "process_arguments",
}
_ADDRESS_KEYS = {
    "address",
    "ip",
    "local_address",
    "remote_address",
    "endpoint",
    "local_endpoint",
    "remote_endpoint",
    "remote_endpoints",
}
_STRICT_IDENTIFIER_KEYS = {
    "pid",
    "parent_pid",
    "process_name",
    "hostname",
    "host",
}


def _redact_ips(text: str) -> str:
    def replace_ipv4(match: re.Match[str]) -> str:
        raw = match.group(0)
        candidate = raw.rsplit(":", 1)[0] if raw.count(":") == 1 else raw
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            return raw
        return "[REDACTED_IP]"

    def replace_ipv6(match: re.Match[str]) -> str:
        raw = match.group(0)
        candidate = raw
        if raw.startswith("["):
            candidate = raw[1 : raw.index("]")]
        candidate = candidate.split("%", 1)[0]
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            return raw
        return "[REDACTED_IP]"

    text = _BRACKETED_IPV6.sub(replace_ipv6, text)
    text = _IPV4.sub(replace_ipv4, text)
    return _BARE_IPV6.sub(replace_ipv6, text)


def redact_text(value: str) -> str:
    text = _EMAIL.sub("[REDACTED_EMAIL]", value)
    text = _WINDOWS_QUOTED_PATH.sub("[REDACTED_PATH]", text)
    text = _WINDOWS_SPACED_PATH.sub("[REDACTED_PATH]", text)
    text = _WINDOWS_PATH.sub("[REDACTED_PATH]", text)
    text = _WINDOWS_ACCOUNT.sub("[REDACTED_USER]", text)
    text = _WINDOWS_SID.sub("[REDACTED_SID]", text)
    text = _UNIX_HOME_PATH.sub("[REDACTED_PATH]", text)
    text = _EVENT_ACCOUNT_FIELD.sub(r"\1[REDACTED_USER]", text)
    local_identifiers = {
        candidate
        for candidate in (
            os.environ.get("USERNAME"),
            os.environ.get("USER"),
            os.path.basename(os.environ.get("USERPROFILE", "")),
            os.path.basename(os.environ.get("HOME", "")),
        )
        if candidate and len(candidate) >= 3
    }
    for identifier in local_identifiers:
        text = re.sub(
            rf"(?i)(?<![\w]){re.escape(identifier)}(?![\w])",
            "[REDACTED_USER]",
            text,
        )
    return _redact_ips(text)


def redact_structure(value: Any, level: RedactionLevel = "standard") -> Any:
    """Return a recursively redacted copy without mutating the source structure."""
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for original_key, item in value.items():
            key = str(original_key)
            normalized = key.casefold()
            if normalized in _USER_KEYS and item not in (None, "", [], {}):
                redacted[key] = "[REDACTED]"
            elif normalized in _PATH_KEYS and item not in (None, "", [], {}):
                redacted[key] = "[REDACTED]"
            elif normalized in _ARGUMENT_KEYS and item not in (None, "", [], {}):
                redacted[key] = "[REDACTED_ARGUMENTS]"
            elif normalized in _ADDRESS_KEYS and item not in (None, "", [], {}):
                redacted[key] = (
                    ["[REDACTED]"] if isinstance(item, list) else "[REDACTED]"
                )
            elif (
                level == "strict"
                and normalized in _STRICT_IDENTIFIER_KEYS
                and item not in (None, "", [], {})
            ):
                redacted[key] = "[REDACTED_IDENTIFIER]"
            else:
                redacted[key] = redact_structure(item, level)
        return redacted
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_structure(item, level) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return copy.deepcopy(value)
