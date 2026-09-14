"""translate-ja-v5の関数説明規約を検査する。"""

from __future__ import annotations

import ast
import re
from pathlib import Path


def test_all_functions_have_complete_japanese_docstrings() -> None:
    """全Python関数が引数と戻り値を含むdocstringを持つことを検証する。

    Returns:
        なし。
    """

    project = Path(__file__).resolve().parents[1]
    failures: list[str] = []
    for path in sorted(project.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            docstring = ast.get_docstring(node) or ""
            parameters = [
                argument.arg
                for argument in (
                    *node.args.posonlyargs,
                    *node.args.args,
                    *node.args.kwonlyargs,
                    *([node.args.vararg] if node.args.vararg else []),
                    *([node.args.kwarg] if node.args.kwarg else []),
                )
                if argument.arg not in {"self", "cls"}
            ]
            if not docstring:
                failures.append(
                    f"{path.relative_to(project)}:{node.lineno} {node.name}: docstringなし"
                )
            elif not re.search(r"[ぁ-んァ-ヶ一-龠]", docstring):
                failures.append(
                    f"{path.relative_to(project)}:{node.lineno} {node.name}: 日本語説明なし"
                )
            elif parameters and "Args:" not in docstring:
                failures.append(
                    f"{path.relative_to(project)}:{node.lineno} {node.name}: Argsなし"
                )
            if docstring and "Returns:" not in docstring and "Yields:" not in docstring:
                failures.append(
                    f"{path.relative_to(project)}:{node.lineno} {node.name}: Returns/Yieldsなし"
                )

    # 一件ずつ止めず一覧化し、規約違反を一度の修正で解消できるようにする。
    assert not failures, "関数docstring規約違反:\n" + "\n".join(failures)
