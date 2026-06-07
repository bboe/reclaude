"""Enforce the repo's everything-sorted conventions (see CLAUDE.md)."""

import ast
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def _assert_sorted(items: list[str], /, *, context: str) -> None:
    assert items == sorted(items), f"{context} not sorted: {items}"


def _python_trees() -> list[tuple[str, ast.Module]]:
    paths = [
        *sorted((REPO_ROOT / "src").rglob("*.py")),
        *sorted((REPO_ROOT / "tests").glob("*.py")),
    ]
    return [
        (str(path.relative_to(REPO_ROOT)), ast.parse(path.read_text(encoding="utf-8")))
        for path in paths
    ]


def _walk_yaml(node: object, /, *, context: str) -> None:
    if isinstance(node, dict):
        keys = ["on" if key is True else str(key) for key in node]
        _assert_sorted(keys, context=context)
        for key, value in node.items():
            if key != "steps":  # execution order is semantic
                _walk_yaml(value, context=f"{context}.{key}")
    elif isinstance(node, list):
        if all(isinstance(item, str) for item in node):
            _assert_sorted(node, context=context)
        for index, item in enumerate(node):
            _walk_yaml(item, context=f"{context}[{index}]")


def test_call_keyword_arguments_sorted() -> None:
    for path, tree in _python_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                kwargs = [kw.arg for kw in node.keywords if kw.arg is not None]
                _assert_sorted(kwargs, context=f"{path}:{node.lineno} call kwargs")


def test_class_fields_sorted() -> None:
    for path, tree in _python_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                fields = [
                    stmt.target.id
                    for stmt in node.body
                    if isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)
                ]
                _assert_sorted(fields, context=f"{path} fields of {node.name}")


def test_dict_literal_keys_sorted() -> None:
    for path, tree in _python_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys = [
                    key.value
                    for key in node.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                ]
                if len(keys) == len(node.keys):
                    _assert_sorted(keys, context=f"{path}:{node.lineno} dict keys")


def test_gitignore_sorted() -> None:
    lines = [
        line
        for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line
    ]
    _assert_sorted(lines, context=".gitignore")


def test_keyword_only_parameters_sorted() -> None:
    for path, tree in _python_trees():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kwonly = [arg.arg for arg in node.args.kwonlyargs]
                context = f"{path}:{node.lineno} kw-only params of {node.name}"
                _assert_sorted(kwonly, context=context)


def test_module_constants_sorted() -> None:
    for path, tree in _python_trees():
        constants = [
            target.id
            for node in tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name) and target.id.upper() == target.id
        ]
        _assert_sorted(constants, context=f"{path} module constants")


def test_module_definitions_sorted() -> None:
    for path, tree in _python_trees():
        defs = [
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        _assert_sorted(defs, context=f"{path} module-level defs")


def test_parameters_positional_only_or_keyword_only() -> None:
    """Every param is behind / or * (lambdas can't express /, so they're exempt).

    At most one positional-only param: "100% obvious from the function name"
    can only ever describe a single argument.
    """
    for path, tree in _python_trees():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                plain = [arg.arg for arg in node.args.args]
                posonly = [arg.arg for arg in node.args.posonlyargs]
                context = f"{path}:{node.lineno} {node.name}"
                assert not plain, f"{context} has positional-or-keyword params: {plain}"
                assert len(posonly) <= 1, (
                    f"{context} has multiple positional-only params: {posonly}"
                )


def test_pyproject_keys_sorted() -> None:
    tables: dict[str, list[str]] = {"": []}
    arrays: list[tuple[str, list[str]]] = []
    pending: list[str] = []
    current = ""
    for line in (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        header = re.fullmatch(r"\[(.+)\]", line)
        entry = re.match(r'^\s+"([^"]*)",', line)
        if header:
            current = header[1]
            tables[current] = []
        elif key := re.match(r'^([A-Za-z0-9_-]+|"[^"]+") =', line):
            tables[current].append(key[1].strip('"'))
            inline = re.search(r"= \[([^\]{]+)\]\s*$", line)  # skip inline tables
            if inline:
                arrays.append((f"inline {key[1]}", re.findall(r'"([^"]*)"', inline[1])))
        if entry:
            pending.append(entry[1])
        elif pending:
            arrays.append((f"array in [{current}]", pending))
            pending = []
    _assert_sorted([name for name in tables if name], context="pyproject tables")
    for name, keys in tables.items():
        _assert_sorted(keys, context=f"pyproject [{name}] keys")
    for context, values in arrays:
        _assert_sorted(values, context=f"pyproject {context}")


def test_yaml_mapping_keys_sorted() -> None:
    for path in (
        ".github/dependabot.yml",
        ".github/workflows/ci.yml",
        ".github/workflows/publish.yml",
        ".pre-commit-config.yaml",
    ):
        document = yaml.safe_load((REPO_ROOT / path).read_text(encoding="utf-8"))
        _walk_yaml(document, context=path)
