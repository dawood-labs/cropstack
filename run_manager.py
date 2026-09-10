"""Numbered run folders with per-stage new/resume control.

Modelled on the segmentation pipeline's `resolve_run_state` / `get_*_output_folder`
pattern: every stage writes into `<stage>_run_<n>`, and the mode decides which `n`.

    "new"     -> <stage>_run_<max + 1>   a clean folder, nothing is reused
    "resume"  -> <stage>_run_<max>       continue the latest run, reusing finished work
    3 / "3"   -> <stage>_run_3           a specific run, whether or not it exists

This removes the need to rename an output folder by hand just to re-run an AOI: the
same AOI and year can be run any number of times, each landing in its own folder, and a
crashed run can be picked up exactly where it stopped.

Stages are independent, so you can resume an expensive NDVI stage while forcing a new
static stage -- the usual case when only the static imagery needs redoing.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

logger = logging.getLogger(__name__)

# Stage folder prefixes, numbered so they sort in execution order on disk.
STAGE_NDVI = "1_ndvi"
STAGE_STATIC = "2_static"
STAGE_VECTOR = "3_vector"
ALL_STAGES = (STAGE_NDVI, STAGE_STATIC, STAGE_VECTOR)

RUN_INFO_FILENAME = "run_info.json"

NEW = "new"
RESUME = "resume"
_RESUME_ALIASES = {RESUME, "resume_latest", "latest", "continue"}


def _run_pattern(stage: str) -> re.Pattern:
    # <stage>_run_<n> with an optional trailing tag, e.g. 1_ndvi_run_3_cloudfix
    return re.compile(rf"^{re.escape(stage)}_run_(\d+)(?:_.*)?$")


def existing_runs(parent_dir: Union[str, Path], stage: str) -> Dict[int, Path]:
    """Maps run id -> folder for every run of `stage` already under `parent_dir`."""
    parent_dir = Path(parent_dir)
    if not parent_dir.is_dir():
        return {}

    pattern = _run_pattern(stage)
    found: Dict[int, Path] = {}
    for child in parent_dir.iterdir():
        if not child.is_dir():
            continue
        match = pattern.match(child.name)
        if match:
            found[int(match.group(1))] = child
    return found


def latest_run_id(parent_dir: Union[str, Path], stage: str) -> int:
    """Highest existing run id for a stage, or 0 when there are none."""
    runs = existing_runs(parent_dir, stage)
    return max(runs) if runs else 0


def resolve_stage_dir(
    parent_dir: Union[str, Path],
    stage: str,
    mode: Union[str, int] = RESUME,
    run_tag: Optional[str] = None,
    create: bool = True,
) -> Tuple[Path, int, bool]:
    """Returns (stage directory, run id, reusing_existing).

    `reusing_existing` is True when the directory already held a previous run, which is
    what lets the caller report "resuming" versus "starting fresh".
    """
    parent_dir = Path(parent_dir)
    runs = existing_runs(parent_dir, stage)
    highest = max(runs) if runs else 0

    mode_text = str(mode).strip().lower()
    if mode_text == NEW:
        run_id = highest + 1
    elif mode_text in _RESUME_ALIASES:
        # Nothing to resume yet: start at 1 rather than failing.
        run_id = highest if highest > 0 else 1
    elif mode_text.isdigit():
        run_id = int(mode_text)
    else:
        raise ValueError(
            f"Unknown run mode {mode!r} for stage {stage!r}. "
            f"Use 'new', 'resume', or an explicit run id."
        )

    existing_dir = runs.get(run_id)
    if existing_dir is not None:
        stage_dir = existing_dir
    else:
        suffix = f"_{run_tag}" if run_tag else ""
        stage_dir = parent_dir / f"{stage}_run_{run_id}{suffix}"

    reusing = stage_dir.exists() and any(stage_dir.iterdir()) if stage_dir.exists() else False

    if create:
        stage_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        f"{stage}: {'resuming' if reusing else 'starting'} run {run_id} -> {stage_dir}"
        + ("" if reusing else "  (no previous output to reuse)")
    )
    return stage_dir, run_id, reusing


def write_run_info(stage_dir: Union[str, Path], payload: Dict[str, Any]) -> Path:
    """Records what produced this folder, so a run can be identified months later.

    Appends to a history list rather than overwriting, so a resumed run keeps the record
    of the attempts that came before it.
    """
    info_path = Path(stage_dir) / RUN_INFO_FILENAME
    history = []
    if info_path.exists():
        try:
            previous = json.loads(info_path.read_text())
            history = previous.get("history", [])
        except (json.JSONDecodeError, OSError):
            logger.warning(f"Could not read existing {info_path.name}; starting a fresh record.")

    entry = {"timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), **payload}
    history.append(entry)

    try:
        info_path.write_text(json.dumps({"latest": entry, "history": history}, indent=2, default=str))
    except OSError as exc:  # never fail a run over bookkeeping
        logger.warning(f"Could not write {info_path}: {exc}")
    return info_path


def describe_runs(parent_dir: Union[str, Path]) -> str:
    """Human-readable summary of every run under an output directory."""
    lines = []
    for stage in ALL_STAGES:
        runs = existing_runs(parent_dir, stage)
        if not runs:
            lines.append(f"{stage:10} (none)")
            continue
        for run_id in sorted(runs):
            lines.append(f"{stage:10} run {run_id}: {runs[run_id].name}")
    return "\n".join(lines) if lines else "(no runs yet)"
