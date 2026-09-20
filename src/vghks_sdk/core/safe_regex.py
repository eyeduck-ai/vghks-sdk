"""Validation and compilation for the deliberately small SOAP regex dialect.

The standard :mod:`re` engine is used only after this module has rejected
constructs that can hide recursion, backreferences, lookarounds, inline flags,
or quantified groups.  Keeping the validator separate also lets the CLI reject
unsafe patterns before credentials are read or an SDK session is created.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import ConfigurationError

MAX_REGEX_LENGTH = 256
MAX_REPEAT_UPPER_BOUND = 500
MAX_QUANTIFIERS = 8
MAX_UNBOUNDED_QUANTIFIERS = 2


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str
    start: int
    end: int


def compile_safe_regex(
    pattern: str,
    *,
    ignore_case: bool = False,
    multiline: bool = False,
    dotall: bool = False,
) -> re.Pattern[str]:
    """Validate and compile a regex without ever including it in an error."""

    if not isinstance(pattern, str) or not pattern:
        raise ConfigurationError("regex pattern must contain 1 to 256 characters")
    if len(pattern) > MAX_REGEX_LENGTH:
        raise ConfigurationError("regex pattern must contain 1 to 256 characters")

    tokens = _tokenize(pattern)
    _validate_quantifiers(pattern, tokens)
    if _minimum_consumed_width(pattern, tokens) < 1:
        raise ConfigurationError("regex pattern must not match an empty string")

    flags = 0
    if ignore_case:
        flags |= re.IGNORECASE
    if multiline:
        flags |= re.MULTILINE
    if dotall:
        flags |= re.DOTALL
    try:
        compiled = re.compile(pattern, flags)
    except (re.error, OverflowError, ValueError) as exc:
        raise ConfigurationError("regex pattern is not valid in the safe dialect") from exc
    if compiled.search("") is not None:
        raise ConfigurationError("regex pattern must not match an empty string")
    return compiled


def _tokenize(pattern: str) -> tuple[_Token, ...]:
    tokens: list[_Token] = []
    group_depth = 0
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "\\":
            end = _consume_escape(pattern, index)
            kind = "ZERO_WIDTH" if pattern[index + 1] in "AbBZ" else "ATOM"
            tokens.append(_Token(kind, index, end))
            index = end
            continue
        if character == "[":
            end = _consume_character_class(pattern, index)
            tokens.append(_Token("ATOM", index, end))
            index = end
            continue
        if character == "(":
            if pattern.startswith("(?:", index):
                end = index + 3
            elif index + 1 < len(pattern) and pattern[index + 1] == "?":
                raise ConfigurationError("regex extension is not allowed in the safe dialect")
            else:
                end = index + 1
            group_depth += 1
            tokens.append(_Token("GROUP_OPEN", index, end))
            index = end
            continue
        if character == ")":
            if group_depth < 1:
                raise ConfigurationError("regex pattern has an unmatched closing group")
            group_depth -= 1
            tokens.append(_Token("GROUP_CLOSE", index, index + 1))
            index += 1
            continue
        if character == "|":
            tokens.append(_Token("ALTERNATION", index, index + 1))
            index += 1
            continue
        if character in "^$":
            tokens.append(_Token("ANCHOR", index, index + 1))
            index += 1
            continue
        if character in "*+?":
            tokens.append(_Token("QUANTIFIER", index, index + 1))
            index += 1
            continue
        if character == "{":
            end = _consume_bounded_repeat(pattern, index)
            tokens.append(_Token("QUANTIFIER", index, end))
            index = end
            continue
        if character in "}]":
            raise ConfigurationError("regex metacharacter must be escaped")
        # Dot and every non-meta character are single searchable atoms.
        tokens.append(_Token("ATOM", index, index + 1))
        index += 1

    if group_depth:
        raise ConfigurationError("regex pattern has an unclosed group")
    return tuple(tokens)


def _consume_escape(pattern: str, start: int) -> int:
    if start + 1 >= len(pattern):
        raise ConfigurationError("regex pattern has an incomplete escape")
    marker = pattern[start + 1]
    if marker.isdigit() or marker in {"g", "k"}:
        raise ConfigurationError("regex backreferences are not allowed")
    if marker in "AbBZdDsSwWnrtfv\\.^$*+?{}[]()|-":
        return start + 2
    if marker == "x":
        return _consume_hex_escape(pattern, start, digits=2)
    if marker == "u":
        return _consume_hex_escape(pattern, start, digits=4)
    if marker == "U":
        return _consume_hex_escape(pattern, start, digits=8)
    raise ConfigurationError("regex escape is not allowed in the safe dialect")


def _consume_hex_escape(pattern: str, start: int, *, digits: int) -> int:
    end = start + 2 + digits
    value = pattern[start + 2 : end]
    if len(value) != digits or any(
        character not in "0123456789abcdefABCDEF" for character in value
    ):
        raise ConfigurationError("regex pattern has an invalid hexadecimal escape")
    return end


def _consume_character_class(pattern: str, start: int) -> int:
    index = start + 1
    if index < len(pattern) and pattern[index] == "^":
        index += 1
    has_content = False
    if index < len(pattern) and pattern[index] == "]":
        index += 1
        has_content = True
    while index < len(pattern):
        character = pattern[index]
        if character == "\\":
            index = _consume_escape(pattern, index)
            has_content = True
            continue
        if character == "]":
            if not has_content:
                raise ConfigurationError("regex character class must not be empty")
            return index + 1
        has_content = True
        index += 1
    raise ConfigurationError("regex pattern has an unclosed character class")


def _consume_bounded_repeat(pattern: str, start: int) -> int:
    closing = pattern.find("}", start + 1)
    if closing < 0:
        raise ConfigurationError("regex pattern has an unclosed repeat")
    body = pattern[start + 1 : closing]
    match = re.fullmatch(r"(\d+)(?:,(\d+))?", body)
    if match is None:
        raise ConfigurationError("regex repeats require a finite upper bound")
    lower = int(match.group(1))
    upper = int(match.group(2) or match.group(1))
    if lower > upper:
        raise ConfigurationError("regex repeat lower bound exceeds its upper bound")
    if upper > MAX_REPEAT_UPPER_BOUND:
        raise ConfigurationError("regex repeat upper bound must not exceed 500")
    return closing + 1


def _validate_quantifiers(pattern: str, tokens: tuple[_Token, ...]) -> None:
    quantifier_count = 0
    unbounded_positions: list[int] = []
    previous: _Token | None = None

    for position, token in enumerate(tokens):
        if token.kind == "QUANTIFIER":
            quantifier_count += 1
            if previous is None or previous.kind != "ATOM":
                if previous is not None and previous.kind == "GROUP_CLOSE":
                    raise ConfigurationError("regex groups cannot be quantified")
                raise ConfigurationError("regex quantifier must follow one atomic token")
            marker = pattern[token.start : token.end]
            if marker in {"*", "+"}:
                unbounded_positions.append(position)
            next_character = pattern[token.end : token.end + 1]
            if next_character in {"?", "+"}:
                raise ConfigurationError("lazy and possessive quantifiers are not allowed")
        previous = token

    if quantifier_count > MAX_QUANTIFIERS:
        raise ConfigurationError("regex pattern contains more than eight quantifiers")
    if len(unbounded_positions) > MAX_UNBOUNDED_QUANTIFIERS:
        raise ConfigurationError("regex pattern contains more than two unbounded quantifiers")
    if len(unbounded_positions) == 2:
        first, second = unbounded_positions
        between = tokens[first + 1 : second]
        # At least one intervening, unquantified atom prevents adjacent runs such
        # as ``a+b+`` while still allowing guarded forms such as ``.*APPLY.*``.
        has_guard = any(
            token.kind == "ATOM"
            and (
                offset + first + 2 >= len(tokens) or tokens[offset + first + 2].kind != "QUANTIFIER"
            )
            for offset, token in enumerate(between)
        )
        if not has_guard:
            raise ConfigurationError("unbounded regex quantifiers must not be adjacent")


def _minimum_consumed_width(pattern: str, tokens: tuple[_Token, ...]) -> int:
    """Return the minimum characters consumed by any complete alternative."""

    def parse_sequence(position: int) -> tuple[int, int]:
        alternatives: list[int] = []
        current_width = 0
        previous_width: int | None = None
        while position < len(tokens):
            token = tokens[position]
            if token.kind == "GROUP_CLOSE":
                alternatives.append(current_width)
                return min(alternatives), position + 1
            if token.kind == "ALTERNATION":
                alternatives.append(current_width)
                current_width = 0
                previous_width = None
                position += 1
                continue
            if token.kind == "GROUP_OPEN":
                term_width, position = parse_sequence(position + 1)
            elif token.kind == "ATOM":
                term_width = 1
                position += 1
            elif token.kind in {"ANCHOR", "ZERO_WIDTH"}:
                term_width = 0
                position += 1
            elif token.kind == "QUANTIFIER":
                if previous_width is None:
                    # The structural validator produces the user-facing error;
                    # this branch only keeps the width analysis total.
                    return 0, len(tokens)
                marker = pattern[token.start : token.end]
                minimum = _repeat_minimum(marker)
                current_width -= previous_width
                previous_width *= minimum
                current_width += previous_width
                position += 1
                continue
            else:  # pragma: no cover - defensive future token type
                return 0, len(tokens)
            current_width += term_width
            previous_width = term_width
        alternatives.append(current_width)
        return min(alternatives), position

    width, _ = parse_sequence(0)
    return width


def _repeat_minimum(marker: str) -> int:
    if marker in {"*", "?"}:
        return 0
    if marker == "+":
        return 1
    body = marker[1:-1]
    return int(body.split(",", 1)[0])
