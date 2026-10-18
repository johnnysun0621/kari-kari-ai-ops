"""守門：cogs 裡的類別不能讀從來沒設定過的 self 屬性。

2026-10-18 掃出兩個這一型的 bug，都是按鈕一按就 AttributeError、被 Discord 顯示成「交互失敗」：
- SubmitTaskView「返回我的任務」讀 self.cog，但 __init__ 沒存（8/15 起）
- TeacherSelectView「返回 Project」讀 self.available_artists，但 __init__ 收了參數沒存

做法：對每個類別，收集方法裡讀的 self.X，扣掉「本類別或 repo 內父類別有指派／定義」
以及「外部父類別（discord.ui.View 等）本來就有」的名稱，剩下的就是讀不到的屬性。
"""
import ast
import importlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COG_FILES = sorted((ROOT / "cogs").glob("*.py"))


def _class_defs():
    for path in COG_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = f"cogs.{path.stem}"
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                yield module, node


def _defined_names(node: ast.ClassDef) -> set[str]:
    names: set[str] = set()
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(item.name)
        elif isinstance(item, ast.Assign):
            names |= {t.id for t in item.targets if isinstance(t, ast.Name)}
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            names.add(item.target.id)
    for sub in ast.walk(node):
        targets = []
        if isinstance(sub, ast.Assign):
            targets = sub.targets
        elif isinstance(sub, (ast.AnnAssign, ast.AugAssign)):
            targets = [sub.target]
        for target in targets:
            for t in ast.walk(target):
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                    names.add(t.attr)
        # setattr(self, "x", ...)
        if (
            isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "setattr"
            and sub.args and isinstance(sub.args[0], ast.Name) and sub.args[0].id == "self"
            and len(sub.args) > 1 and isinstance(sub.args[1], ast.Constant)
        ):
            names.add(sub.args[1].value)
    return names


def _read_names(node: ast.ClassDef) -> dict[str, int]:
    reads: dict[str, int] = {}
    for item in node.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(item):
            if (
                isinstance(sub, ast.Attribute) and isinstance(sub.ctx, ast.Load)
                and isinstance(sub.value, ast.Name) and sub.value.id == "self"
            ):
                reads.setdefault(sub.attr, sub.lineno)
    return reads


def _available_names(module: str, class_name: str) -> set[str]:
    cls = getattr(importlib.import_module(module), class_name, None)
    if cls is None:  # 巢狀或條件式定義的類別，不在這個守門範圍
        return set()
    names: set[str] = set()
    for base in cls.__mro__:
        names |= set(vars(base))
        names |= set(getattr(base, "__slots__", ()) or ())
    return names


def test_cog_classes_never_read_unset_self_attributes():
    defs = list(_class_defs())
    defined_by_class = {(m, n.name): _defined_names(n) for m, n in defs}
    problems = []
    for module, node in defs:
        cls = getattr(importlib.import_module(module), node.name, None)
        if cls is None:
            continue
        available = _available_names(module, node.name)
        # repo 內父類別在 __init__ 裡指派的屬性（vars(cls) 看不到實例屬性）
        for base in cls.__mro__:
            for (m, name), names in defined_by_class.items():
                if base.__module__ == m and base.__name__ == name:
                    available |= names
        for attr, lineno in _read_names(node).items():
            if attr not in available and not hasattr(cls, attr):
                problems.append(f"{module}.{node.name} 第 {lineno} 行讀 self.{attr}，但從來沒有設定")
    assert not problems, "\n".join(problems)
