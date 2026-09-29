"""AOI input handling: accept any reasonable path, in any vector format, local or GCS.

All of these resolve to one local file that geopandas can open:

    r"C:\\data\\aoi\\aoi_11.shp"                     # raw string (recommended on Windows)
    "C:\\data\\aoi\\aoi_11.shp"                      # plain string -- see the escape note below
    "/home/jovyan/FAO/cane/aoi_11.gpkg"
    Path("/home/jovyan/FAO/cane/aoi.geojson")
    "gs://bucket/aois/aoi_11.shp"                   # shapefile sidecars fetched automatically
    "gs://bucket/aois/districts.gpkg"

Formats: `.shp` (with sidecars), `.gpkg`, `.geojson` / `.json`, `.kml`, `.fgb`,
`.parquet`, or a `.zip` containing any of them.

**The Windows escape trap.** A Windows path in a *non-raw* Python string is corrupted at
parse time, before any library sees it: `"C:\\data\\fao_aoi.shp"` contains `\\f`, which
Python turns into a formfeed character. `\\t`, `\\n`, `\\r`, `\\b`, `\\v`, `\\a` and
`\\0` do the same. `repair_mangled_windows_path` detects those characters and puts the
backslash back, so such a path still works -- but prefix the string with `r` to avoid
relying on the repair.
"""
from __future__ import annotations

import logging
import os
import re
import zipfile
from pathlib import Path
from typing import List, Optional, Union

logger = logging.getLogger(__name__)

DEFAULT_AOI_CACHE_DIR = os.path.expanduser("~/.cache/fao_pipeline/aoi")


def local_utm_epsg(lon: float, lat: float) -> int:
    """The UTM zone whose central meridian is closest to this point.

    Used wherever this pipeline needs to reproject into metres to compute an area or a
    pixel count. There is no one UTM zone that is correct for every AOI -- Punjab alone
    straddles zones 42N and 43N near 72 deg E -- so callers pass a real lon/lat (usually
    the AOI's own centroid) rather than a CRS chosen once for a single country or region.
    """
    zone = int((lon + 180) / 6) + 1
    return (32600 if lat >= 0 else 32700) + zone

# Everything ESRI may scatter next to a .shp. The first three are mandatory; the rest
# are fetched when present because losing a .prj silently breaks reprojection.
SHAPEFILE_SIDECAR_EXTENSIONS = [
    ".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn", ".sbx", ".fbn", ".fbx",
    ".ain", ".aih", ".ixs", ".mxs", ".atx", ".shp.xml",
]

VECTOR_EXTENSIONS = {".shp", ".gpkg", ".geojson", ".json", ".kml", ".kmz", ".fgb", ".parquet", ".gml", ".tab"}

# Characters Python produces from a backslash escape, mapped back to the letter that
# produced them.
_ESCAPE_REVERSAL = {
    "\x07": "a", "\x08": "b", "\t": "t", "\n": "n",
    "\x0b": "v", "\x0c": "f", "\r": "r", "\x00": "0",
}


def repair_mangled_windows_path(text: str) -> str:
    """Puts back backslashes that Python's string parser ate (`\\f` -> formfeed, etc.).

    Returns the input unchanged when it holds no such characters.
    """
    if not any(character in text for character in _ESCAPE_REVERSAL):
        return text
    return "".join(
        "\\" + _ESCAPE_REVERSAL[character] if character in _ESCAPE_REVERSAL else character
        for character in text
    )


def translate_path_spellings(text: str) -> List[str]:
    """Returns the equivalent ways this path may be spelled on the running platform.

    The same file has different names depending on where the code runs: a Windows
    `C:\\data\\aoi.shp` is `/mnt/c/data/aoi.shp` under WSL and `/c/data/aoi.shp` under
    Git Bash / MSYS. Notebooks are routinely written on Windows and executed under WSL
    (or the reverse), so a path that looks wrong is usually just spelled for the other
    side. The original spelling always comes first.
    """
    candidates = [text]

    windows_drive = re.match(r"^([A-Za-z]):[\\/](.*)$", text)
    if windows_drive:
        drive, remainder = windows_drive.group(1).lower(), windows_drive.group(2).replace("\\", "/")
        candidates.append(f"/mnt/{drive}/{remainder}")  # WSL
        candidates.append(f"/{drive}/{remainder}")      # Git Bash / MSYS / Cygwin

    posix_mount = re.match(r"^/(?:mnt/)?([a-zA-Z])/(.*)$", text)
    if posix_mount:
        drive, remainder = posix_mount.group(1).upper(), posix_mount.group(2).replace("/", "\\")
        candidates.append(f"{drive}:\\{remainder}")     # Windows

    seen, unique = set(), []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def resolve_local_path(text: str) -> Optional[Path]:
    """Finds the file behind a path that may be mis-spelled for this platform, or
    mangled by Python's backslash escapes. Returns None when nothing matches."""
    for variant in (text, repair_mangled_windows_path(text)):
        for candidate in translate_path_spellings(variant):
            expanded = Path(os.path.expanduser(candidate))
            if expanded.exists():
                return expanded
    return None


def normalize_path_text(source: Union[str, Path]) -> str:
    """Accepts a str or Path and strips the noise people paste in: surrounding
    whitespace, wrapping quotes, and a stray `r` prefix from a copied raw string."""
    if isinstance(source, Path):
        return str(source)
    text = str(source).strip()
    if len(text) >= 2 and text[0] == "r" and text[1] in "\"'":
        text = text[1:]
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    return text.strip()


def _extract_zip(zip_path: Path, cache_dir: Path) -> Path:
    """Unpacks a zipped vector dataset and returns the readable file inside it."""
    target_dir = cache_dir / f"{zip_path.stem}_unzipped"
    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(target_dir)

    candidates = [
        path for path in sorted(target_dir.rglob("*"))
        if path.suffix.lower() in VECTOR_EXTENSIONS
    ]
    if not candidates:
        raise FileNotFoundError(f"No vector file found inside {zip_path}")
    # Prefer a shapefile if the archive holds several things (e.g. .shp plus metadata).
    shapefiles = [path for path in candidates if path.suffix.lower() == ".shp"]
    return shapefiles[0] if shapefiles else candidates[0]


def _convert_parquet(parquet_path: Path, cache_dir: Path) -> Path:
    """Rewrites a GeoParquet AOI as a GeoPackage in the cache.

    GeoParquet needs `gpd.read_parquet`; every consumer in this pipeline (the grid split,
    the crop mask, the boundary clip) calls `gpd.read_file`, which cannot open it.
    Converting once here makes the format work end to end instead of only at the door.
    """
    import geopandas as gpd

    cache_dir.mkdir(parents=True, exist_ok=True)
    converted = cache_dir / f"{parquet_path.stem}_from_parquet.gpkg"
    if converted.exists() and converted.stat().st_mtime >= parquet_path.stat().st_mtime:
        return converted

    frame = gpd.read_parquet(parquet_path)
    if frame.empty:
        raise ValueError(f"GeoParquet AOI is empty: {parquet_path}")
    frame.to_file(converted, driver="GPKG")
    logger.info(f"Converted GeoParquet AOI -> {converted}")
    return converted


def _download_from_gcs(uri: str, cache_dir: Path, gcs_key_path: Optional[str]) -> Path:
    """Downloads an AOI from GCS, including every shapefile sidecar when the URI names
    a `.shp`. Cached by bucket layout, so it is fetched once per machine."""
    if not gcs_key_path:
        raise ValueError(
            f"Reading the AOI from {uri} needs GCS credentials -- set aoi_gcs_key_path, "
            "or the pipeline's gee_service_account_key."
        )

    import gcs_io

    bucket_name, blob_path = uri.replace("gs://", "").split("/", 1)
    bucket = gcs_io.gcs_client(gcs_key_path).bucket(bucket_name)

    local_dir = cache_dir / bucket_name / str(Path(blob_path).parent)
    local_dir.mkdir(parents=True, exist_ok=True)
    local_path = local_dir / Path(blob_path).name

    if Path(blob_path).suffix.lower() == ".shp":
        blob_stem = blob_path[: -len(".shp")]
        downloaded: List[str] = []
        for extension in SHAPEFILE_SIDECAR_EXTENSIONS:
            blob = bucket.blob(blob_stem + extension)
            if not blob.exists():
                continue
            sidecar_path = local_dir / (Path(blob_stem).name + extension)
            if not sidecar_path.exists() or sidecar_path.stat().st_size != blob.size:
                blob.download_to_filename(str(sidecar_path))
            downloaded.append(extension)

        if ".shp" not in downloaded:
            raise FileNotFoundError(f"AOI shapefile not found on GCS: {uri}")
        missing_required = {".shx", ".dbf"} - set(downloaded)
        if missing_required:
            raise FileNotFoundError(
                f"{uri} is missing required shapefile component(s) {sorted(missing_required)} "
                "-- upload the full sidecar set."
            )
        if ".prj" not in downloaded:
            logger.warning(f"{uri} has no .prj; the AOI's CRS will have to be assumed.")
        logger.info(f"Downloaded AOI shapefile ({len(downloaded)} components) -> {local_path}")
        return local_path

    blob = bucket.blob(blob_path)
    if not blob.exists():
        raise FileNotFoundError(f"AOI not found on GCS: {uri}")
    if not local_path.exists() or local_path.stat().st_size != blob.size:
        blob.download_to_filename(str(local_path))
        logger.info(f"Downloaded AOI -> {local_path}")
    return local_path


def _validate_and_clean_geometry(resolved: Path, cache_dir: Path):
    """Rejects an AOI with nothing to grid or acquire against, repairs invalid geometry,
    and drops anything that isn't a polygon.

    `gee_client.split_aoi_into_grid` already applies these checks, but only on the GEE
    grid-split path. The STAC path -- the default, and the one this pipeline's own README
    recommends -- handed the AOI straight to `farmdar.sentinel` unchecked: an empty AOI
    crashed deep inside tile-grid math (`ValueError: cannot convert float NaN to integer`),
    and a points-only AOI (no polygon at all) passed `validate()` and proceeded straight to
    real Sentinel-2 acquisition. Moving the same cleaning rules here means both backends
    inherit them, whichever one reads the AOI first.

    Returns `(path_to_use, crs)`. `path_to_use` is `resolved` unchanged when nothing needed
    repairing, or a cleaned copy in `cache_dir` when it did -- so every later `gpd.read_file`
    on `cfg.aoi_path` (there are several, scattered across the pipeline) sees the same
    cleaned geometry without having to change any of those call sites.
    """
    import geopandas as gpd

    try:
        aoi = gpd.read_file(resolved)
    except Exception as exc:
        raise ValueError(f"Could not read AOI {resolved}: {exc}")

    if aoi.empty:
        raise ValueError(f"AOI contains no features: {resolved}")

    changed = False

    invalid = ~aoi.geometry.is_valid
    if invalid.any():
        logger.warning(
            f"Repairing {int(invalid.sum())} invalid AOI geometr(ies) in {resolved.name} "
            "before use."
        )
        aoi.loc[invalid, "geometry"] = aoi.loc[invalid, "geometry"].make_valid()
        changed = True

    polygonal = aoi.geom_type.isin(["Polygon", "MultiPolygon"])
    if not polygonal.all():
        logger.warning(
            f"Dropping {int((~polygonal).sum())} non-polygon geometr(ies) "
            f"(point/line/other) from {resolved.name} before use."
        )
        aoi = aoi[polygonal]
        changed = True

    if aoi.empty:
        raise ValueError(f"AOI has no polygon geometry after cleaning: {resolved}")

    if not changed:
        return resolved, aoi.crs

    cache_dir.mkdir(parents=True, exist_ok=True)
    cleaned_path = cache_dir / f"{resolved.stem}_cleaned.gpkg"
    aoi.to_file(cleaned_path, driver="GPKG")
    logger.info(f"Wrote a repaired copy of the AOI -> {cleaned_path}")
    return cleaned_path, aoi.crs


def resolve_aoi(
    source: Union[str, Path],
    cache_dir: Union[str, Path] = DEFAULT_AOI_CACHE_DIR,
    gcs_key_path: Optional[str] = None,
    verify_readable: bool = True,
) -> Path:
    """Turns any accepted AOI reference into a local file path geopandas can open."""
    text = normalize_path_text(source)
    if not text:
        raise ValueError("AOI path is empty.")

    cache_dir = Path(os.path.expanduser(str(cache_dir)))

    if text.startswith("gs://"):
        resolved = _download_from_gcs(text, cache_dir, gcs_key_path)
    else:
        found = resolve_local_path(text)
        if found is None:
            hints = []
            if repair_mangled_windows_path(text) != text:
                hints.append(
                    "This path contains control characters, so a backslash escape "
                    "(\\f, \\t, \\n, ...) was consumed by Python -- prefix the string "
                    "with r, as in r\"C:\\path\\to\\aoi.shp\"."
                )
            alternatives = translate_path_spellings(text)[1:]
            if alternatives:
                hints.append("Also tried: " + ", ".join(alternatives))
            raise FileNotFoundError(
                f"AOI not found: {text}." + ("  " + "  ".join(hints) if hints else "")
            )

        if str(found) != os.path.expanduser(text):
            logger.info(f"AOI path resolved for this platform: {text} -> {found}")
        resolved = found

    if resolved.suffix.lower() == ".zip":
        resolved = _extract_zip(resolved, cache_dir)
        logger.info(f"Extracted zipped AOI -> {resolved}")

    if resolved.suffix.lower() == ".parquet":
        resolved = _convert_parquet(resolved, cache_dir)

    if resolved.suffix.lower() == ".shp":
        for required in (".shx", ".dbf"):
            if not resolved.with_suffix(required).exists():
                raise FileNotFoundError(
                    f"{resolved.name} is missing its {required} sidecar -- a shapefile is "
                    "not a single file. Copy the whole set, or use a .gpkg/.geojson."
                )

    if resolved.suffix.lower() not in VECTOR_EXTENSIONS:
        logger.warning(f"Unrecognised AOI extension '{resolved.suffix}'; trying to read it anyway.")

    if verify_readable:
        resolved, crs = _validate_and_clean_geometry(resolved, cache_dir)
        if crs is None:
            logger.warning(f"AOI {resolved.name} has no CRS defined; EPSG:4326 will be assumed.")

    return resolved
