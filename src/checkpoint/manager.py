from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)


class RunCheckpoint(BaseModel):
    """Persisted state of a run, enabling resume after interruption."""

    checkpoint_version: int = 1
    run_id: str
    original_request: dict
    all_prompts: List[str]
    completed_prompt_indices: List[int]
    next_batch_index: int
    total_batches: int
    window_results: List[dict]
    original_started_at: str
    last_checkpoint_at: str
    status: str  # "in_progress" | "completed"


class CheckpointManager:
    """Manages checkpoint files for run persistence and resume."""

    def __init__(self, output_dir: str = "outputs") -> None:
        self._output_dir = Path(output_dir)

    def save(self, checkpoint: RunCheckpoint) -> Path:
        """Atomically write a checkpoint file (write .tmp, then rename)."""
        self._output_dir.mkdir(parents=True, exist_ok=True)
        target = self._output_dir / f"checkpoint_{checkpoint.run_id}.json"
        tmp = self._output_dir / f"checkpoint_{checkpoint.run_id}.json.tmp"

        data = checkpoint.model_dump(mode="json")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())

        os.replace(str(tmp), str(target))
        logger.debug("Checkpoint saved: %s", target)
        return target

    def load(self, run_id: str) -> Optional[RunCheckpoint]:
        """Load a checkpoint by run_id. Returns None if missing or corrupt."""
        path = self._output_dir / f"checkpoint_{run_id}.json"
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return RunCheckpoint(**data)
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.warning("Corrupt checkpoint %s: %s", run_id, exc)
            corrupt_path = path.with_suffix(".json.corrupt")
            try:
                path.rename(corrupt_path)
            except OSError:
                pass
            return None

    def list_resumable(self) -> List[RunCheckpoint]:
        """Return all checkpoints with status 'in_progress'."""
        if not self._output_dir.exists():
            return []
        results: List[RunCheckpoint] = []
        for path in sorted(self._output_dir.glob("checkpoint_*.json")):
            if path.suffix != ".json":
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                cp = RunCheckpoint(**data)
                if cp.status == "in_progress":
                    results.append(cp)
            except (json.JSONDecodeError, ValidationError, OSError) as exc:
                logger.warning("Skipping unreadable checkpoint %s: %s", path.name, exc)
        return results

    def mark_completed(self, run_id: str) -> None:
        """Update a checkpoint's status to 'completed'."""
        cp = self.load(run_id)
        if cp:
            cp.status = "completed"
            self.save(cp)

    def delete(self, run_id: str) -> None:
        """Remove a checkpoint file."""
        path = self._output_dir / f"checkpoint_{run_id}.json"
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Failed to delete checkpoint %s: %s", run_id, exc)
