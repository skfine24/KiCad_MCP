import re
from pathlib import Path

_TOKEN_RE = re.compile(r'"(?:[^"\\]|\\.)*"|[()]|[^\s()]+')


def _unquote(token: str) -> str:
    body = token[1:-1]
    return body.replace('\\"', '"').replace("\\\\", "\\")


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
