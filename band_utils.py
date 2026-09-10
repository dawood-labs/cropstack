"""Canonical NDVI band-stack parsing, source-agnostic.

GEE-exported NDVI stacks name bands "B4_2025_01_08" / "B8_2025_01_08"; STAC-fetched
stacks (sentinel.py) name them "red_2025_01_08" / "nir_2025_01_08". Both encode exactly
the same thing (red/NIR reflectance for one date window), so the RF inference worker
just needs one parser that understands both prefixes -- no file rewriting required, and
no dependency on which acquisition backend produced the tile.

This replaces notebook 1's `parse_raw_bands` and notebook 2's `parse_stac_bands`, which
were otherwise identical except for the band-name prefix they recognized.
"""
from __future__ import annotations

from typing import List, Sequence, Tuple

_RED_PREFIXES = ("red_", "B4_")
_NIR_PREFIXES = ("nir_", "B8_")


def parse_band_stack(descriptions: Sequence[str]) -> Tuple[List[int], List[int], List[str]]:
    """Returns (red_band_indices, nir_band_indices, dates) from a multi-band tile's
    band descriptions, regardless of whether they came from GEE or STAC acquisition."""
    red_idx: List[int] = []
    nir_idx: List[int] = []
    dates: List[str] = []

    for i, d in enumerate(descriptions):
        if not d:
            continue
        if d.startswith(_RED_PREFIXES):
            red_idx.append(i)
            parts = d.split("_")
            dates.append(f"{parts[1]}-{parts[2]}-{parts[3]}")
        elif d.startswith(_NIR_PREFIXES):
            nir_idx.append(i)

    assert len(red_idx) == len(nir_idx), (
        f"Mismatch between Red ({len(red_idx)}) and NIR ({len(nir_idx)}) band counts."
    )
    return red_idx, nir_idx, dates
