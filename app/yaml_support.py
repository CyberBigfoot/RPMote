"""Comment-preserving YAML formatting and validation, without executing tags."""

import datetime
import io
import re
from collections.abc import Mapping

from app.remote_files import MAX_EDIT_BYTES


class YamlError(ValueError):
    pass


def _limit_input(text):
    # Bound decoded characters here; TextDocument checks bytes in the file's
    # actual encoding. A valid UTF-16 file can exceed this size as UTF-8.
    if len(text) > MAX_EDIT_BYTES:
        raise YamlError("The YAML editor supports files up to 2 MiB. Download this file to edit it locally.")


def _parser():
    from ruamel.yaml import YAML
    parser = YAML(typ="rt", pure=True)
    parser.preserve_quotes = True
    parser.max_depth = 100
    return parser


def _error(exc):
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None) or (str(exc).splitlines() or [type(exc).__name__])[0]
    location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
    return YamlError(f"YAML error{location}: {problem}")


def _line_context(line, quote=None):
    """Find comments and quoted scalar continuations before changing whitespace."""
    index = 0
    while index < len(line):
        char = line[index]
        if quote == '"' and char == "\\":
            index += 2
            continue
        if quote:
            if char == quote:
                if quote == "'" and index + 1 < len(line) and line[index + 1] == "'":
                    index += 2
                    continue
                quote = None
        elif char in "\"'" and (index == 0 or line[index - 1] in " \t:[{,-"):
            quote = char
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index].rstrip(), quote
        index += 1
    return line.rstrip(), quote


def normalize_indentation_tabs(text, width=2):
    """Repair structural indentation, preserving tabs in values and scalar bodies."""
    output, quote, scalar_indent, scalar_step, scalar_column = [], None, None, width, None
    for line in text.split("\n"):
        prefix = re.match(r"[ \t]*", line).group()
        spaces = len(prefix.expandtabs(width))
        in_scalar = scalar_indent is not None and (not line.strip() or spaces > scalar_indent)
        if in_scalar:
            # Tabs after the body's structural indent are literal content.
            # Expand only the tabs needed to reach that indent.
            leading_spaces = len(line) - len(line.lstrip(" "))
            if scalar_column is None and line.strip():
                scalar_column = leading_spaces if leading_spaces > scalar_indent else scalar_indent + scalar_step
            required = scalar_column or (leading_spaces if leading_spaces > scalar_indent else scalar_indent + scalar_step)
            column, consumed = 0, 0
            for char in prefix:
                if column >= required:
                    break
                column += width - column % width if char == "\t" else 1
                consumed += 1
            line = prefix[:consumed].expandtabs(width) + line[consumed:]
        else:
            scalar_indent = None
            scalar_column = None
            if quote is None:
                line = prefix.expandtabs(width) + line[len(prefix):]
            content, quote = _line_context(line, quote)
            indicator = re.search(r"(?:^|:\s+|-\s+)[|>]([+-]?[1-9]?[+-]?)$", content.strip())
            if quote is None and indicator:
                sequence_mapping = re.match(r"-\s+.+:\s+[|>]", content.strip())
                scalar_indent = spaces + (len(re.match(r"-\s+", content.strip()).group()) if sequence_mapping else 0)
                digit = re.search(r"[1-9]", indicator.group(1))
                scalar_step = int(digit.group()) if digit else width
                if digit:
                    scalar_column = scalar_indent + scalar_step
        output.append(line)
    return "\n".join(output)


def validate_yaml(text):
    _limit_input(text)
    try:
        # Construct every document so duplicate keys and malformed merge keys
        # are detected too. Round-trip mode treats unknown tags as data.
        list(_parser().load_all(text))
    except Exception as exc:
        raise _error(exc) from exc
    return text


def prepare_yaml(text, width=2):
    _limit_input(text)
    try:
        validate_yaml(text)
        return text
    except YamlError:
        pass
    prepared = normalize_indentation_tabs(text, width)
    validate_yaml(prepared)
    return prepared


def _comments(text):
    from ruamel.yaml.tokens import CommentToken
    comments = []
    def collect(value):
        if isinstance(value, CommentToken):
            comments.extend(line.strip() for line in value.value.splitlines() if line.strip())
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect(item)
    for token in _parser().scan(text):
        collect(getattr(token, "comment", None))
    return comments


def _structure(text):
    """Compare tags, values, key order and alias graphs before applying a format."""
    from ruamel.yaml.nodes import ScalarNode, MappingNode
    parser, documents = _parser(), []
    for root in parser.compose_all(text):
        seen = {}
        def visit(node):
            if node is None:
                return None
            identity = id(node)
            if identity in seen:
                return "alias", seen[identity]
            seen[identity] = len(seen)
            if isinstance(node, ScalarNode):
                value = node.value
                if node.tag in ("tag:yaml.org,2002:null", "tag:yaml.org,2002:bool",
                                "tag:yaml.org,2002:int", "tag:yaml.org,2002:float",
                                "tag:yaml.org,2002:binary", "tag:yaml.org,2002:timestamp"):
                    value = parser.constructor.construct_object(node)
                    if isinstance(value, float):
                        value = value.hex()
                    elif isinstance(value, (datetime.date, datetime.datetime)):
                        value = value.isoformat()
                content = value
            elif isinstance(node, MappingNode):
                content = tuple((visit(key), visit(value)) for key, value in node.value)
            else:
                content = tuple(visit(item) for item in node.value)
            return node.tag, getattr(node, "anchor", None), content
        documents.append(visit(root))
    return documents


def _keep_named_anchors(documents):
    stack, seen = list(documents), set()
    while stack:
        value = stack.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        anchor = getattr(value, "anchor", None)
        if anchor is not None and anchor.value:
            anchor.always_dump = True
        if isinstance(value, Mapping):
            for key, item in value.items():
                stack.extend((key, item))
            stack.extend(getattr(value, "merge", ()))
        elif isinstance(value, (list, tuple, set)):
            stack.extend(value)


def format_yaml(text, width=2):
    """Normalize indentation only when round-tripping preserves YAML contents."""
    from ruamel.yaml.events import DocumentStartEvent, DocumentEndEvent
    text = prepare_yaml(text, width)
    try:
        events = list(_parser().parse(text))
        starts = [event for event in events if isinstance(event, DocumentStartEvent)]
        ends = [event for event in events if isinstance(event, DocumentEndEvent)]
        reader = _parser()
        documents = list(reader.load_all(text))
        # The loader has no container on which to retain comments for empty
        # documents. Leave those streams intact rather than losing their text.
        if not documents or any(document is None for document in documents):
            return text
        _keep_named_anchors(documents)
        output = io.StringIO()
        if starts[0].explicit:
            prefix = text[:starts[0].start_mark.index]
            output.write("".join(line for line in prefix.splitlines(keepends=True)
                                 if not line.strip() or line.lstrip().startswith("#")))
        for document, start, end in zip(documents, starts, ends):
            writer = _parser()
            writer.indent(mapping=width, sequence=width + 2, offset=width)
            writer.width = max(4096, len(text) + 1)
            writer.explicit_start, writer.explicit_end = start.explicit, end.explicit
            writer.version, writer.tags = start.version, start.tags
            writer.dump(document, output)
        formatted = output.getvalue()
        validate_yaml(formatted)
        if _structure(text) != _structure(formatted) or _comments(text) != _comments(formatted):
            raise YamlError("YAML formatting would change a value, tag, alias or comment. Your draft was kept unchanged.")
        return formatted
    except YamlError:
        raise
    except Exception as exc:
        raise _error(exc) from exc

