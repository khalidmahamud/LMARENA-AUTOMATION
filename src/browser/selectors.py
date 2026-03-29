from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from src.core.exceptions import SelectorConfigError


class SelectorRegistry:
    """Typed accessor for Arena DOM selectors loaded from YAML.

    Singleton — loaded once at startup via ``load()``, then accessed
    everywhere via ``instance()``.  Supports dotted-key lookups
    (e.g. ``get("response_panel.left")``) and a ``health_check()``
    method that verifies critical selectors on a live page.
    """

    _instance: Optional[SelectorRegistry] = None

    def __init__(self, selectors: Dict[str, Any]) -> None:
        self._selectors = selectors

    @classmethod
    def load(cls, yaml_path: str = "config/selectors.yaml") -> SelectorRegistry:
        """Load selectors from YAML. Called once at startup."""
        import yaml

        path = Path(yaml_path)
        if not path.exists():
            raise SelectorConfigError(f"Selector config not found: {yaml_path}")
        raw = yaml.safe_load(path.read_text())
        if not isinstance(raw, dict):
            raise SelectorConfigError("Selector config must be a YAML mapping")
        cls._instance = cls(raw)
        return cls._instance

    @classmethod
    def instance(cls) -> SelectorRegistry:
        """Return the loaded singleton. Raises if ``load()`` was not called."""
        if cls._instance is None:
            raise SelectorConfigError(
                "SelectorRegistry not loaded. Call .load() first."
            )
        return cls._instance

    def get(self, key: str) -> str:
        """Resolve a dotted key to a CSS selector string.

        Examples::

            registry.get("prompt_textarea")       # top-level
            registry.get("response_panel.left")    # nested
        """
        parts = key.split(".")
        node: Any = self._selectors
        for part in parts:
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                raise KeyError(f"Selector not found: {key}")
        if not isinstance(node, str):
            raise KeyError(f"Selector '{key}' resolved to non-string: {type(node)}")
        return node

    async def health_check(self, page: Any) -> Dict[str, bool]:
        """Verify that critical selectors exist on the current page.

        Returns a mapping of ``selector_key → found (True/False)``.
        """
        critical_keys = [
            "prompt_textarea",
            "submit_button",
            "response_panel.left",
            "response_panel.right",
        ]
        results: Dict[str, bool] = {}
        for key in critical_keys:
            try:
                selector = self.get(key)
                element = await page.query_selector(selector)
                results[key] = element is not None
            except KeyError:
                results[key] = False
        return results
