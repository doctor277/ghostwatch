from __future__ import annotations


def safe_text(value: object) -> str:
    """Escape lone surrogates, which Windows names allow but UTF-8 consumers reject."""
    return str(value).encode("utf-8", "backslashreplace").decode("utf-8")
