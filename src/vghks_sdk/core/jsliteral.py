"""A deliberately tiny JavaScript literal reader.

This module never executes JavaScript.  It recognizes only quoted string
literals, numeric literals, explicitly supplied identifiers, `+` concatenation,
and balanced constructor argument lists.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from urllib.parse import quote

from .errors import ParseError

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_NUMBER_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


@dataclass(frozen=True, slots=True)
class ConstructorCall:
    name: str
    arguments: tuple[str, ...]
    start: int
    end: int


def decode_js_string(literal: str) -> str | None:
    literal = literal.strip()
    if len(literal) < 2 or literal[0] not in {'"', "'"} or literal[-1] != literal[0]:
        return None
    body = literal[1:-1]
    out: list[str] = []
    index = 0
    simple = {
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "b": "\b",
        "f": "\f",
        "v": "\v",
        "0": "\0",
        "\\": "\\",
        "'": "'",
        '"': '"',
        "/": "/",
    }
    while index < len(body):
        char = body[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        index += 1
        if index >= len(body):
            out.append("\\")
            break
        escaped = body[index]
        if escaped in simple:
            out.append(simple[escaped])
            index += 1
        elif escaped == "x" and index + 2 < len(body):
            token = body[index + 1 : index + 3]
            try:
                out.append(chr(int(token, 16)))
                index += 3
            except ValueError:
                out.append("x")
                index += 1
        elif escaped == "u" and index + 4 < len(body):
            token = body[index + 1 : index + 5]
            try:
                out.append(chr(int(token, 16)))
                index += 5
            except ValueError:
                out.append("u")
                index += 1
        elif escaped in {"\n", "\r"}:
            index += 1
        else:
            out.append(escaped)
            index += 1
    return "".join(out)


def split_top_level(source: str, delimiter: str) -> tuple[str, ...]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    escaped = False
    for index, char in enumerate(source):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        elif char == delimiter and depth == 0:
            parts.append(source[start:index].strip())
            start = index + 1
    parts.append(source[start:].strip())
    return tuple(parts)


def evaluate_expression(expression: str, variables: Mapping[str, str]) -> str | None:
    """Evaluate a tiny, side-effect-free subset of string expressions."""

    expression = expression.strip()
    while expression.startswith("(") and expression.endswith(")"):
        inner = expression[1:-1].strip()
        if _balanced(inner):
            expression = inner
        else:
            break
    terms = split_top_level(expression, "+")
    output: list[str] = []
    for term in terms:
        term = term.strip()
        decoded = decode_js_string(term)
        if decoded is not None:
            output.append(decoded)
        elif (encoded := _evaluate_encode_uri_component(term, variables)) is not None:
            output.append(encoded)
        elif (substring := _evaluate_substring(term, variables)) is not None:
            output.append(substring)
        elif _IDENTIFIER_RE.fullmatch(term) and term in variables:
            output.append(variables[term])
        elif _NUMBER_RE.fullmatch(term):
            output.append(term)
        elif term in {"true", "false", "null"}:
            output.append("" if term == "null" else term)
        else:
            return None
    return "".join(output)


def evaluated_string_assignments(
    source: str,
    allowed_names: set[str],
    initial: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Evaluate ordered assignments for an explicit identifier allow-list.

    Once an allow-listed assignment is encountered, its enclosing branches
    and expression must be statically understood.  Failing closed prevents an
    earlier value from being reused when an unknown branch or function would
    have replaced it in the browser.
    """

    cleaned = strip_js_comments(source)
    if not allowed_names:
        return {}
    names = "|".join(re.escape(name) for name in sorted(allowed_names, key=len, reverse=True))
    marker = re.compile(rf"(?:\bvar\s+)?\b(?P<name>{names})\s*(?P<op>\+=|=(?!=))")
    values: dict[str, str] = dict(initial or {})
    branches = _conditional_branches(cleaned)
    for match in marker.finditer(cleaned):
        branch_active = True
        for condition, truthy, start, end in branches:
            if not start <= match.start() < end:
                continue
            result = evaluate_condition(condition, values)
            if result is None:
                raise ParseError(
                    "JavaScript assignment branch was not in the static allow-list",
                    code="JS_BRANCH_UNSUPPORTED",
                )
            if result is not truthy:
                branch_active = False
                break
        if not branch_active:
            continue
        end = _statement_end(cleaned, match.end())
        if end is None:
            raise ParseError(
                "JavaScript assignment was not terminated",
                code="JS_ASSIGNMENT_INVALID",
            )
        expression = cleaned[match.end() : end].strip()
        evaluated = evaluate_expression(expression, values)
        if evaluated is None:
            raise ParseError(
                "JavaScript assignment expression was not in the static allow-list",
                code="JS_EXPRESSION_UNSUPPORTED",
            )
        name = match.group("name")
        values[name] = values.get(name, "") + evaluated if match.group("op") == "+=" else evaluated
    return values


def string_assignments(source: str, allowed_names: set[str]) -> dict[str, str]:
    """Read simple string assignments for an explicit identifier allow-list."""

    result: dict[str, str] = {}
    for name in allowed_names:
        pattern = re.compile(
            rf"(?:\bvar\s+)?\b{re.escape(name)}\s*=\s*"
            r"(?P<value>(?P<quote>['\"])(?:\\.|(?!\2).)*\2)\s*;",
            re.DOTALL,
        )
        matches = list(pattern.finditer(source))
        if not matches:
            continue
        value = decode_js_string(matches[-1].group("value"))
        if value is not None:
            result[name] = value
    return result


def iter_constructor_calls(source: str, name: str) -> Iterator[ConstructorCall]:
    marker = re.compile(rf"\bnew\s+{re.escape(name)}\s*\(")
    for match in marker.finditer(source):
        open_index = source.find("(", match.start())
        close_index = _matching_parenthesis(source, open_index)
        if close_index is None:
            raise ParseError(
                "JavaScript constructor call was not terminated",
                code="JS_CONSTRUCTOR_INVALID",
            )
        arguments = split_top_level(source[open_index + 1 : close_index], ",")
        yield ConstructorCall(name, arguments, match.start(), close_index + 1)


def iter_active_constructor_calls(
    source: str,
    name: str,
    variables: Mapping[str, str] | None = None,
) -> Iterator[ConstructorCall]:
    """Yield constructors from active constant branches only.

    A branch enclosing a requested constructor must be statically decidable;
    otherwise parsing fails closed instead of guessing which server branch ran.
    """

    cleaned = strip_js_comments(source)
    branches = _conditional_branches(cleaned)
    environment = variables or {}
    for call in iter_constructor_calls(cleaned, name):
        active = True
        for condition, truthy, start, end in branches:
            if not start <= call.start < end:
                continue
            result = evaluate_condition(condition, environment)
            if result is None:
                raise ParseError(
                    "JavaScript branch was not in the static allow-list",
                    code="JS_BRANCH_UNSUPPORTED",
                )
            if result is not truthy:
                active = False
                break
        if active:
            yield call


def static_document_writes(
    source: str,
    variables: Mapping[str, str] | None = None,
    *,
    allow_unknown_branches: bool = False,
) -> str:
    """Render only static ``document.write`` calls without executing script."""

    cleaned = strip_js_comments(source)
    branches = _conditional_branches(cleaned)
    environment = variables or {}
    output: list[str] = []
    marker = re.compile(r"\bdocument\s*\.\s*write\s*\(")
    for match in marker.finditer(cleaned):
        open_index = cleaned.find("(", match.start())
        close_index = _matching_parenthesis(cleaned, open_index)
        if close_index is None:
            raise ParseError("unterminated document.write", code="JS_DOCUMENT_WRITE_INVALID")
        active = True
        for condition, truthy, start, end in branches:
            if start <= match.start() < end:
                result = evaluate_condition(condition, environment)
                if result is None:
                    if not allow_unknown_branches:
                        raise ParseError(
                            "document.write branch was not statically decidable",
                            code="JS_BRANCH_UNSUPPORTED",
                        )
                    # Numeric-result pages emit identical static cell text in
                    # both branches and vary only an abnormal CSS class.  In
                    # that explicitly requested mode, select the first branch
                    # while still refusing to evaluate dynamic expressions.
                    result = True
                if result is not truthy:
                    active = False
                    break
        if not active:
            continue
        expression = cleaned[open_index + 1 : close_index]
        value = evaluate_expression(expression, environment)
        if value is None:
            value = _static_literal_fragments(expression)
        output.append(value)
    return "".join(output)


def strip_js_comments(source: str) -> str:
    """Remove line/block comments while preserving strings and offsets."""

    output = list(source)
    index = 0
    quote_char: str | None = None
    escaped = False
    while index < len(source):
        char = source[index]
        if quote_char is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote_char:
                quote_char = None
            index += 1
            continue
        if char in {'"', "'"}:
            quote_char = char
            index += 1
            continue
        if source.startswith("//", index):
            end = source.find("\n", index + 2)
            end = len(source) if end < 0 else end
            for position in range(index, end):
                output[position] = " "
            index = end
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end < 0:
                raise ParseError("unterminated JavaScript comment", code="JS_COMMENT_INVALID")
            for position in range(index, end + 2):
                if output[position] not in "\r\n":
                    output[position] = " "
            index = end + 2
            continue
        index += 1
    return "".join(output)


def evaluate_condition(expression: str, variables: Mapping[str, str]) -> bool | None:
    """Evaluate a small side-effect-free constant boolean subset."""

    expression = expression.strip()
    while expression.startswith("(") and expression.endswith(")"):
        close = _matching_parenthesis(expression, 0)
        if close == len(expression) - 1:
            expression = expression[1:-1].strip()
        else:
            break
    or_parts = split_top_level_token(expression, "||")
    if len(or_parts) > 1:
        results = [evaluate_condition(part, variables) for part in or_parts]
        if any(result is True for result in results):
            return True
        return False if all(result is False for result in results) else None
    and_parts = split_top_level_token(expression, "&&")
    if len(and_parts) > 1:
        results = [evaluate_condition(part, variables) for part in and_parts]
        if any(result is False for result in results):
            return False
        return True if all(result is True for result in results) else None
    if expression.startswith("!") and not expression.startswith("!="):
        nested = evaluate_condition(expression[1:], variables)
        return None if nested is None else not nested
    if expression in {"true", "1"}:
        return True
    if expression in {"false", "0", "null", "undefined", "''", '""'}:
        return False

    comparison = _split_comparison(expression)
    if comparison is not None:
        left_text, operator, right_text = comparison
        left = _evaluate_scalar(left_text, variables)
        right = _evaluate_scalar(right_text, variables)
        if left is _UNKNOWN or right is _UNKNOWN:
            return None
        if operator in {"==", "==="}:
            return left == right
        if operator in {"!=", "!=="}:
            return left != right
        try:
            left_number = float(left)
            right_number = float(right)
        except (TypeError, ValueError):
            return None
        return {
            "<": left_number < right_number,
            "<=": left_number <= right_number,
            ">": left_number > right_number,
            ">=": left_number >= right_number,
        }[operator]
    scalar = _evaluate_scalar(expression, variables)
    if scalar is _UNKNOWN:
        return None
    return bool(scalar)


def split_top_level_token(source: str, delimiter: str) -> tuple[str, ...]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote_char: str | None = None
    escaped = False
    index = 0
    while index < len(source):
        char = source[index]
        if quote_char:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote_char:
                quote_char = None
            index += 1
            continue
        if char in {'"', "'"}:
            quote_char = char
        elif char in "([{":
            depth += 1
        elif char in ")]}" and depth:
            depth -= 1
        elif depth == 0 and source.startswith(delimiter, index):
            parts.append(source[start:index].strip())
            index += len(delimiter)
            start = index
            continue
        index += 1
    parts.append(source[start:].strip())
    return tuple(parts)


def extract_quoted_strings(source: str) -> Iterator[str]:
    index = 0
    while index < len(source):
        if source[index] not in {'"', "'"}:
            index += 1
            continue
        quote = source[index]
        start = index
        index += 1
        escaped = False
        while index < len(source):
            char = source[index]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                literal = source[start : index + 1]
                decoded = decode_js_string(literal)
                if decoded is not None:
                    yield decoded
                index += 1
                break
            index += 1


def _matching_parenthesis(source: str, open_index: int) -> int | None:
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(open_index, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _balanced(source: str) -> bool:
    depth = 0
    quote: str | None = None
    escaped = False
    for char in source:
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and quote is None


_UNKNOWN = object()


def _evaluate_encode_uri_component(
    expression: str,
    variables: Mapping[str, str],
) -> str | None:
    match = re.fullmatch(r"encodeURIComponent\s*\((?P<value>.*)\)", expression, re.DOTALL)
    if match is None:
        return None
    value = evaluate_expression(match.group("value"), variables)
    if value is None:
        return None
    return quote(value, safe="~()*!.'-", encoding="utf-8", errors="strict")


def _evaluate_substring(expression: str, variables: Mapping[str, str]) -> str | None:
    match = re.fullmatch(
        r"(?P<base>(?:[A-Za-z_$][A-Za-z0-9_$]*|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"))"
        r"\s*\.\s*(?P<method>substring|substr)\s*\(\s*(?P<start>\d+)"
        r"(?:\s*,\s*(?P<end>\d+))?\s*\)",
        expression,
        re.DOTALL,
    )
    if match is None:
        return None
    base = evaluate_expression(match.group("base"), variables)
    if base is None:
        return None
    start = int(match.group("start"))
    end_token = match.group("end")
    if match.group("method") == "substr":
        end = start + int(end_token) if end_token is not None else len(base)
    else:
        end = int(end_token) if end_token is not None else len(base)
        if end < start:
            start, end = end, start
    return base[start:end]


def _statement_end(source: str, start: int) -> int | None:
    depth = 0
    quote_char: str | None = None
    escaped = False
    for index in range(start, len(source)):
        char = source[index]
        if quote_char:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote_char:
                quote_char = None
            continue
        if char in {'"', "'"}:
            quote_char = char
        elif char in "([{":
            depth += 1
        elif char in ")]}" and depth:
            depth -= 1
        elif char == ";" and depth == 0:
            return index
    return None


def _matching_brace(source: str, open_index: int) -> int | None:
    depth = 0
    quote_char: str | None = None
    escaped = False
    for index in range(open_index, len(source)):
        char = source[index]
        if quote_char:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote_char:
                quote_char = None
            continue
        if char in {'"', "'"}:
            quote_char = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def _js_statement_boundary(source: str, start: int) -> int | None:
    """Return the end of one statement, including a nested if/else chain."""

    while start < len(source) and source[start].isspace():
        start += 1
    if start >= len(source):
        return None
    if source[start] == "{":
        close = _matching_brace(source, start)
        return None if close is None else close + 1
    if re.match(r"if\s*\(", source[start:]):
        open_paren = source.find("(", start)
        close_paren = _matching_parenthesis(source, open_paren)
        if close_paren is None:
            return None
        after_then = _js_statement_boundary(source, close_paren + 1)
        if after_then is None:
            return None
        cursor = after_then
        while cursor < len(source) and source[cursor].isspace():
            cursor += 1
        if re.match(r"else\b", source[cursor:]):
            return _js_statement_boundary(source, cursor + 4)
        return after_then
    end = _statement_end(source, start)
    return None if end is None else end + 1


def _conditional_branches(source: str) -> list[tuple[str, bool, int, int]]:
    branches: list[tuple[str, bool, int, int]] = []
    marker = re.compile(r"\bif\s*\(")
    for match in marker.finditer(source):
        open_paren = source.find("(", match.start())
        close_paren = _matching_parenthesis(source, open_paren)
        if close_paren is None:
            raise ParseError("unterminated JavaScript condition", code="JS_BRANCH_INVALID")
        cursor = close_paren + 1
        while cursor < len(source) and source[cursor].isspace():
            cursor += 1
        if cursor >= len(source):
            continue
        if source[cursor] == "{":
            close_body = _matching_brace(source, cursor)
            if close_body is None:
                raise ParseError("unterminated JavaScript branch", code="JS_BRANCH_INVALID")
            then_start, then_end = cursor + 1, close_body
            after = close_body + 1
        else:
            statement_end = _js_statement_boundary(source, cursor)
            if statement_end is None:
                continue
            then_start, then_end = cursor, statement_end
            after = statement_end
        condition = source[open_paren + 1 : close_paren]
        branches.append((condition, True, then_start, then_end))
        cursor = after
        while cursor < len(source) and source[cursor].isspace():
            cursor += 1
        if not re.match(r"else\b", source[cursor:]):
            continue
        cursor += 4
        while cursor < len(source) and source[cursor].isspace():
            cursor += 1
        if cursor < len(source) and source[cursor] == "{":
            close_else = _matching_brace(source, cursor)
            if close_else is None:
                raise ParseError("unterminated JavaScript else branch", code="JS_BRANCH_INVALID")
            branches.append((condition, False, cursor + 1, close_else))
        else:
            else_end = _js_statement_boundary(source, cursor)
            if else_end is not None:
                branches.append((condition, False, cursor, else_end))
    return branches


def _split_comparison(expression: str) -> tuple[str, str, str] | None:
    for operator in ("!==", "===", "<=", ">=", "!=", "==", "<", ">"):
        parts = split_top_level_token(expression, operator)
        if len(parts) == 2:
            return parts[0], operator, parts[1]
    return None


def _evaluate_scalar(expression: str, variables: Mapping[str, str]) -> object:
    expression = expression.strip()
    if expression in {"null", "undefined"}:
        return None
    if expression == "true":
        return True
    if expression == "false":
        return False
    wrapper = re.fullmatch(
        r"(?:parseInt|parseFloat|Number|strTrim)\s*\((?P<value>.*)\)",
        expression,
        re.DOTALL,
    )
    if wrapper is not None:
        value = _evaluate_scalar(wrapper.group("value"), variables)
        if value is _UNKNOWN:
            return _UNKNOWN
        try:
            if expression.startswith("strTrim"):
                return str(value).strip()
            return float(str(value).strip())
        except ValueError:
            return _UNKNOWN
    length = re.fullmatch(r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)\.length", expression)
    if length is not None and length.group("name") in variables:
        return len(variables[length.group("name")])
    index_of = re.fullmatch(
        r"(?P<base>[A-Za-z_$][A-Za-z0-9_$]*)\.indexOf\((?P<needle>.*)\)",
        expression,
        re.DOTALL,
    )
    if index_of is not None and index_of.group("base") in variables:
        needle = evaluate_expression(index_of.group("needle"), variables)
        return variables[index_of.group("base")].find(needle) if needle is not None else _UNKNOWN
    value = evaluate_expression(expression, variables)
    if value is None:
        return _UNKNOWN
    if _NUMBER_RE.fullmatch(value):
        return float(value)
    return value


def _static_literal_fragments(expression: str) -> str:
    output: list[str] = []
    saw_literal = False
    for term in split_top_level(expression, "+"):
        decoded = decode_js_string(term.strip())
        if decoded is not None:
            output.append(decoded)
            saw_literal = True
            continue
        raise ParseError(
            "document.write expression was not in the static allow-list",
            code="JS_DOCUMENT_WRITE_UNSUPPORTED",
        )
    if not saw_literal:
        raise ParseError(
            "document.write did not contain static content",
            code="JS_DOCUMENT_WRITE_UNSUPPORTED",
        )
    return "".join(output)
