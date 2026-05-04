from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    def known(self) -> bool:
        return (
            self.input_tokens is not None
            or self.output_tokens is not None
            or self.total_tokens is not None
        )


def _parse_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "")
        return int(cleaned) if cleaned.isdigit() else None
    return None


def _first_int(mapping: dict[str, Any], keys: set[str]) -> int | None:
    normalized = {
        key.lower().replace("-", "_"): value for key, value in mapping.items()
    }
    for key in keys:
        value = _parse_int(normalized.get(key))
        if value is not None:
            return value
    return None


def _usage_from_mapping(mapping: dict[str, Any]) -> TokenUsage | None:
    input_tokens = _first_int(mapping, {"input_tokens", "prompt_tokens"})
    output_tokens = _first_int(
        mapping, {"output_tokens", "completion_tokens", "response_tokens"}
    )
    total_tokens = _first_int(mapping, {"total_tokens"})
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    usage = TokenUsage(input_tokens, output_tokens, total_tokens)
    return usage if usage.known() else None


def _json_usages(value: Any) -> list[TokenUsage]:
    usages: list[TokenUsage] = []
    if isinstance(value, dict):
        usage = _usage_from_mapping(value)
        if usage:
            usages.append(usage)
        for child in value.values():
            usages.extend(_json_usages(child))
    elif isinstance(value, list):
        for child in value:
            usages.extend(_json_usages(child))
    return usages


def _last_pattern_int(text: str, patterns: list[str]) -> int | None:
    matches: list[str] = []
    for pattern in patterns:
        matches.extend(
            match.group(1) for match in re.finditer(pattern, text, flags=re.IGNORECASE)
        )
    return _parse_int(matches[-1]) if matches else None


def _usage_from_text(text: str) -> TokenUsage | None:
    input_tokens = _last_pattern_int(
        text,
        [
            r"\b(?:input|prompt)[ _-]?tokens?\b\s*[:=]\s*(\d[\d,]*)",
            r"\b(?:input|prompt)\b\s*[:=]\s*(\d[\d,]*)\s*tokens?\b",
            r"\b(\d[\d,]*)[ \t]+(?:input|prompt)[ _-]?tokens?\b",
        ],
    )
    output_tokens = _last_pattern_int(
        text,
        [
            r"\b(?:output|completion|response)[ _-]?tokens?\b\s*[:=]\s*(\d[\d,]*)",
            r"\b(?:output|completion|response)\b\s*[:=]\s*(\d[\d,]*)\s*tokens?\b",
            r"\b(\d[\d,]*)[ \t]+(?:output|completion|response)[ _-]?tokens?\b",
        ],
    )
    total_tokens = _last_pattern_int(
        text,
        [
            r"\btotal[ _-]?tokens?\b\s*[:=]\s*(\d[\d,]*)",
            r"\btotal\b\s*[:=]\s*(\d[\d,]*)\s*tokens?\b",
            r"\b(\d[\d,]*)[ \t]+total[ _-]?tokens?\b",
            r"\btokens[ \t]+used\b\s*(?:\r?\n)+\s*(\d[\d,]*)",
        ],
    )
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    usage = TokenUsage(input_tokens, output_tokens, total_tokens)
    return usage if usage.known() else None


def parse_token_usage(text: str) -> TokenUsage | None:
    usages: list[TokenUsage] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        usages.extend(_json_usages(event))
    if usages:
        return usages[-1]
    return _usage_from_text(text)


def sum_token_usages(usages: list[TokenUsage]) -> TokenUsage | None:
    if not usages:
        return None
    input_tokens = sum(
        usage.input_tokens for usage in usages if usage.input_tokens is not None
    )
    output_tokens = sum(
        usage.output_tokens for usage in usages if usage.output_tokens is not None
    )
    total_tokens = sum(
        usage.total_tokens
        if usage.total_tokens is not None
        else (usage.input_tokens or 0) + (usage.output_tokens or 0)
        for usage in usages
    )
    return TokenUsage(
        input_tokens=input_tokens
        if any(usage.input_tokens is not None for usage in usages)
        else None,
        output_tokens=output_tokens
        if any(usage.output_tokens is not None for usage in usages)
        else None,
        total_tokens=total_tokens
        if any(
            usage.total_tokens is not None
            or usage.input_tokens is not None
            or usage.output_tokens is not None
            for usage in usages
        )
        else None,
    )


def format_token_usage(usage: TokenUsage | None) -> str:
    if usage is None or not usage.known():
        return "unavailable"
    parts: list[str] = []
    if usage.input_tokens is not None:
        parts.append(f"input={usage.input_tokens}")
    if usage.output_tokens is not None:
        parts.append(f"output={usage.output_tokens}")
    if usage.total_tokens is not None:
        parts.append(f"total={usage.total_tokens}")
    return " ".join(parts)
