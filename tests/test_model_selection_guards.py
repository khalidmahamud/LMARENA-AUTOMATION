import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKER_PATH = ROOT / "src" / "workers" / "arena_worker.py"


def _load_module() -> ast.Module:
    return ast.parse(WORKER_PATH.read_text(encoding="utf-8"))


def _find_worker_class(module: ast.Module) -> ast.ClassDef:
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == "ArenaWorker":
            return node
    raise AssertionError("ArenaWorker class not found")


def _find_method(worker_class: ast.ClassDef, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in worker_class.body:
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"ArenaWorker.{name} not found")


class ModelSelectionGuardTests(unittest.TestCase):
    def test_select_model_checks_current_labels_before_opening_picker(self) -> None:
        module = _load_module()
        worker_class = _find_worker_class(module)
        select_model = _find_method(worker_class, "_select_model")

        called_methods = set()
        for node in ast.walk(select_model):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                called_methods.add(node.func.attr)

        self.assertIn("_get_current_model_labels", called_methods)
        self.assertIn("_model_names_match", called_methods)

    def test_worker_defines_helpers_for_model_selection_guards(self) -> None:
        module = _load_module()
        worker_class = _find_worker_class(module)

        helper_names = {
            node.name
            for node in worker_class.body
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        }
        self.assertIn("_get_current_model_labels", helper_names)
        self.assertIn("_normalize_model_name", helper_names)
        self.assertIn("_model_names_match", helper_names)


if __name__ == "__main__":
    unittest.main()
