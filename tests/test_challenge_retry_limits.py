import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKER_PATH = ROOT / "src" / "workers" / "arena_worker.py"
ORCHESTRATOR_PATH = ROOT / "src" / "orchestrator" / "run_orchestrator.py"


def _load_module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _find_class(module: ast.Module, name: str) -> ast.ClassDef:
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"Class {name} not found")


def _find_function(container: ast.AST, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    body = getattr(container, "body", [])
    for node in body:
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"Function {name} not found")


def _default_name(func: ast.AsyncFunctionDef | ast.FunctionDef, arg_name: str) -> str:
    args = func.args.args
    defaults = func.args.defaults
    default_map = {
        arg.arg: default
        for arg, default in zip(args[-len(defaults):], defaults)
    }
    default = default_map[arg_name]
    if isinstance(default, ast.Name):
        return default.id
    if isinstance(default, ast.Constant):
        return str(default.value)
    raise AssertionError(f"Unexpected default node for {arg_name}: {ast.dump(default)}")


class ChallengeRetryLimitTests(unittest.TestCase):
    def test_worker_retry_defaults_share_the_five_attempt_limit(self) -> None:
        module = _load_module(WORKER_PATH)

        retry_limit = None
        for node in module.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "CHALLENGE_RETRY_LIMIT":
                        retry_limit = ast.literal_eval(node.value)
        self.assertEqual(retry_limit, 5)

        worker_class = _find_class(module, "ArenaWorker")
        self.assertEqual(
            _default_name(_find_function(worker_class, "_handle_challenge"), "max_retries"),
            "CHALLENGE_RETRY_LIMIT",
        )
        self.assertEqual(
            _default_name(_find_function(worker_class, "prepare_prompt"), "retry_on_challenge"),
            "CHALLENGE_RETRY_LIMIT",
        )
        self.assertEqual(
            _default_name(
                _find_function(worker_class, "submit_prepared_prompt"),
                "retry_on_challenge",
            ),
            "CHALLENGE_RETRY_LIMIT",
        )
        self.assertEqual(
            _default_name(_find_function(worker_class, "submit_prompt"), "retry_on_challenge"),
            "CHALLENGE_RETRY_LIMIT",
        )
        self.assertEqual(
            _default_name(_find_function(worker_class, "poll_for_completion"), "_recovery_retries"),
            "CHALLENGE_RETRY_LIMIT",
        )

    def test_worker_challenge_recovery_call_sites_use_shared_limit(self) -> None:
        module = _load_module(WORKER_PATH)
        worker_class = _find_class(module, "ArenaWorker")

        handle_challenge_limits = []
        for node in ast.walk(worker_class):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute) or node.func.attr != "_handle_challenge":
                continue
            for kw in node.keywords:
                if kw.arg != "max_retries":
                    continue
                self.assertIsInstance(kw.value, ast.Name)
                handle_challenge_limits.append(kw.value.id)

        self.assertGreaterEqual(len(handle_challenge_limits), 2)
        self.assertTrue(
            all(name == "CHALLENGE_RETRY_LIMIT" for name in handle_challenge_limits)
        )

    def test_orchestrator_recovery_uses_shared_limit(self) -> None:
        module = _load_module(ORCHESTRATOR_PATH)

        imported_limit = False
        for node in module.body:
            if isinstance(node, ast.ImportFrom) and node.module == "src.workers.arena_worker":
                imported_limit = any(
                    alias.name == "CHALLENGE_RETRY_LIMIT" for alias in node.names
                )
        self.assertTrue(imported_limit)

        orchestrator_class = _find_class(module, "RunOrchestrator")
        attempt_recovery = _find_function(orchestrator_class, "_attempt_recovery")

        assigned_limit = None
        for node in attempt_recovery.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "max_context_retries":
                    self.assertIsInstance(node.value, ast.Name)
                    assigned_limit = node.value.id
        self.assertEqual(assigned_limit, "CHALLENGE_RETRY_LIMIT")


if __name__ == "__main__":
    unittest.main()
