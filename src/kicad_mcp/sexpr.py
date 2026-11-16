import re
from pathlib import Path

_TOKEN_RE = re.compile(r'"(?:[^"\\]|\\.)*"|[()]|[^\s()]+')


class Quoted(str):
    """A token that was quoted in the source file.

    Keeping this marker lets ``dump`` re-quote exactly the tokens KiCad wrote
    as strings instead of guessing from the value.
    """


def _unquote(token: str) -> Quoted:
    body = token[1:-1]
    return Quoted(body.replace('\\"', '"').replace("\\\\", "\\"))


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def parse(text: str) -> list:
    tokens = tokenize(text)
    stack: list[list] = [[]]
    for token in tokens:
        if token == "(":
            node: list = []
            stack[-1].append(node)
            stack.append(node)
        elif token == ")":
            if len(stack) == 1:
                raise ValueError("unbalanced parentheses")
            stack.pop()
        else:
            if token.startswith('"'):
                stack[-1].append(_unquote(token))
            else:
                stack[-1].append(token)
    if len(stack) != 1:
        raise ValueError("unbalanced parentheses")
    return stack[0]


def load_file(path: str | Path) -> list:
    tree = parse(Path(path).read_text(encoding="utf-8"))
    if not tree:
        raise ValueError(f"empty file: {path}")
    return tree[0]


def _atom(node) -> str:
    if isinstance(node, Quoted):
        return _quote(str(node))
    return str(node)


def dump(node, level: int = 0) -> str:
    """Serialize a parsed tree to KiCad-style tab-indented S-expressions."""
    if not isinstance(node, list):
        return _atom(node)
    if not node:
        return "()"
    head = _atom(node[0])
    if all(not isinstance(child, list) for child in node):
        inline = "(" + " ".join([head, *(_atom(child) for child in node[1:])]) + ")"
        if len(inline) <= 100:
            return inline
    child_indent = "\t" * (level + 1)
    lines = ["(" + head]
    for child in node[1:]:
        lines.append(child_indent + dump(child, level + 1))
    lines.append("\t" * level + ")")
    return "\n".join(lines)


def dump_file(node, path: str | Path, newline: str = "\n") -> None:
    Path(path).write_text(dump(node) + newline, encoding="utf-8", newline="\n")


def get_first(node: list, key: str) -> list | None:
    for child in node:
        if isinstance(child, list) and child and child[0] == key:
            return child
    return None


def get_all(node: list, key: str) -> list[list]:
    return [child for child in node if isinstance(child, list) and child and child[0] == key]


def get_value(node: list, key: str) -> str | None:
    child = get_first(node, key)
    if child and len(child) > 1:
        return str(child[1])
    return None


def walk(node):
    """Yield every list node, including the root."""
    if isinstance(node, list):
        yield node
        for child in node:
            yield from walk(child)


def clone(node):
    """Deep copy a tree while preserving Quoted markers."""
    if isinstance(node, list):
        return [clone(child) for child in node]
    if isinstance(node, Quoted):
        return Quoted(str(node))
    return node


def remove_child(parent: list, child) -> bool:
    for index, item in enumerate(parent):
        if item is child:
            del parent[index]
            return True
    return False


def replace_child(parent: list, old, new) -> bool:
    for index, item in enumerate(parent):
        if item is old:
            parent[index] = new
            return True
    return False
