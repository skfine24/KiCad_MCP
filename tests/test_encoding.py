"""Encoding guard: every source, test and doc file must be UTF-8 without BOM."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOP_LEVEL = ("README.md", "log.md", "pyproject.toml")


def _iter_files():
    for folder in ("src", "tests"):
        for path in (ROOT / folder).rglob("*.py"):
            if "__pycache__" not in path.parts:
                yield path
    for name in TOP_LEVEL:
        yield ROOT / name


def test_all_text_files_are_utf8_without_bom():
    failures = []
    for path in _iter_files():
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            failures.append(f"{path}: UTF-8 BOM")
            continue
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            failures.append(f"{path}: {exc}")
    assert not failures, "\n".join(failures)
