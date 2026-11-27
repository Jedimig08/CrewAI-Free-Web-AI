from __future__ import annotations

import ast
import re
from collections import defaultdict
from dataclasses import dataclass

from explainer.repo import FileInfo

IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
JS_DEFINITION = re.compile(
    r"^(?:export\s+(?:default\s+)?)?(?:async\s+)?"
    r"(?:function\s+([A-Za-z_$][\w$]*)"
    r"|(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*="
    r"|class\s+([A-Za-z_$][\w$]*))",
    re.MULTILINE,
)


@dataclass(frozen=True)
class SymbolDef:
    name: str
    file: str
    line: int
    kind: str


class SymbolIndex:
    def __init__(self) -> None:
        self.definitions: dict[str, SymbolDef] = {}
        self.uses: dict[str, set[str]] = defaultdict(set)

    @classmethod
    def build(cls, files: list[FileInfo], max_defs: int = 2000) -> SymbolIndex:
        index = cls()
        for item in files:
            for symbol in _python_definitions(item):
                index._add(symbol, max_defs)
            if item.path.endswith((".js", ".jsx", ".ts", ".tsx")):
                for symbol in _js_definitions(item):
                    index._add(symbol, max_defs)
        return index

    def _add(self, symbol: SymbolDef, max_defs: int) -> None:
        if len(self.definitions) >= max_defs:
            return
        existing = self.definitions.get(symbol.name)
        if existing is None:
            self.definitions[symbol.name] = symbol
        elif existing.kind == "import" and symbol.kind != "import":
            self.definitions[symbol.name] = symbol
        elif existing.kind != "import" and symbol.kind == "import":
            return
        elif (symbol.file, symbol.line) < (existing.file, existing.line):
            self.definitions[symbol.name] = symbol

    def record_usage(self, file: str, text: str) -> None:
        for name in set(IDENTIFIER.findall(text)):
            if name in self.definitions:
                self.uses[name].add(file)

    def lookup(self, name: str) -> SymbolDef | None:
        return self.definitions.get(name)

    def context_for(self, text: str, max_chars: int = 3000) -> str:
        lines: list[str] = []
        seen: set[str] = set()
        for name in IDENTIFIER.findall(text):
            if name in seen or name not in self.definitions:
                continue
            seen.add(name)
            symbol = self.definitions[name]
            lines.append(f"{name} -> {symbol.file}:{symbol.line} ({symbol.kind})")
            if sum(len(line) for line in lines) >= max_chars:
                break
        if not lines:
            return "No matching definitions found in the repository index."
        return "Known definitions used by this chunk:\n" + "\n".join(lines)

    def summary(self, max_chars: int = 4000) -> str:
        lines: list[str] = []
        for symbol in sorted(
            self.definitions.values(), key=lambda item: (item.file, item.line, item.name)
        ):
            lines.append(f"{symbol.name} ({symbol.kind}) - {symbol.file}:{symbol.line}")
            if sum(len(line) for line in lines) >= max_chars:
                break
        if not lines:
            return "No definitions found."
        return "Repository symbol index:\n" + "\n".join(lines)


def _python_definitions(item: FileInfo) -> list[SymbolDef]:
    try:
        tree = ast.parse(item.text)
    except SyntaxError:
        return []

    found: list[SymbolDef] = []

    def collect(nodes: list[ast.stmt]) -> None:
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append(SymbolDef(node.name, item.path, node.lineno, "function"))
            elif isinstance(node, ast.ClassDef):
                found.append(SymbolDef(node.name, item.path, node.lineno, "class"))
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        found.append(SymbolDef(target.id, item.path, node.lineno, "variable"))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                found.append(SymbolDef(node.target.id, item.path, node.lineno, "variable"))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.asname or alias.name.split(".")[0]
                    found.append(SymbolDef(name, item.path, node.lineno, "import"))
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    name = alias.asname or alias.name
                    found.append(SymbolDef(name, item.path, node.lineno, "import"))
            elif isinstance(node, (ast.If, ast.Try)):
                collect(node.body)
                collect(node.orelse)
                if isinstance(node, ast.Try):
                    for handler in node.handlers:
                        collect(handler.body)
                    collect(node.finalbody)

    collect(tree.body)
    return found


def _js_definitions(item: FileInfo) -> list[SymbolDef]:
    found: list[SymbolDef] = []
    for number, line in enumerate(item.text.splitlines(), start=1):
        match = JS_DEFINITION.match(line)
        if not match:
            continue
        name = next((group for group in match.groups() if group), None)
        if name:
            found.append(SymbolDef(name, item.path, number, "function"))
    return found
