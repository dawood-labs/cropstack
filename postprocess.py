"""Shared post-processing: morphological sieve cleanup and raster -> vector export.

Both functions were copy-pasted twice per notebook (once for the NDVI/RF output, once
for the static/XGBoost output) across all three original notebooks. Consolidated here
as one parameterised version each, with the memory profile tightened:

* the sieve held roughly a dozen full-raster arrays alive at peak (~14 bytes/pixel,
  i.e. several GB for a district at 10 m). Intermediates are now freed the moment they
  are consumed, class membership uses a 256-entry lookup table instead of `np.isin`
  temporaries, and clump reverting uses a label lookup table rather than `np.isin` over
  an int32 label raster. Peak drops to roughly 8 bytes/pixel.
* vectorisation no longer wraps every polygon in a throwaway dict, drops
  sub-threshold polygons before the expensive clip, and skips the redundant dissolve
  (see `dissolve_polygons`).
"""
from __future__ import annotations

import gc
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence, Union

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import shapes, sieve
from scipy.ndimage import binary_dilation, generate_binary_structure, label
from shapely.geometry import MultiPolygon, shape

ACRES_PER_SQ_METRE = 0.000247105
# Pakistan-wide AOIs; matches the original notebooks' hardcoded area CRS.
AREA_CRS_EPSG = 32642


def _class_membership_mask(data: np.ndarray, class_values: Sequence[int]) -> np.ndarray:
    """Boolean "is this pixel one of these classes" mask.

    For uint8 rasters this is a single 256-entry lookup -- one pass, no per-class
    temporary arrays like `np.isin` would build.
    """
    if data.dtype == np.uint8:
        lookup = np.zeros(256, dtype=bool)
        valid = [value for value in class_values if 0 <= int(value) <= 255]
        lookup[np.asarray(valid, dtype=np.uint8)] = True
        return lookup[data]
    return np.isin(data, class_values)


def _describe_source_raster(raster_path: Union[str, Path]) -> dict:
    """Fingerprints the raster a vector product was built from."""
    stats = Path(raster_path).stat()
    return {
        "source_raster": str(Path(raster_path).resolve()),
        "size_bytes": stats.st_size,
        "mtime_ns": stats.st_mtime_ns,
    }


def _read_source_record(record_path: Path, key: Optional[str] = None):
    if not record_path.exists():
        return None
    try:
        record = json.loads(record_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return record.get(key) if key else record


def _write_source_record(record_path: Path, payload: dict) -> None:
    try:
        record_path.write_text(json.dumps(payload, indent=2))
    except OSError as exc:  # bookkeeping must never fail a finished product
        print(f"  [Warning] Could not record the source raster: {exc}")


def apply_strict_directional_sieve(
    input_raster_path: Union[str, Path],
    target_classes: List[int],
    min_pixel_size: int = 15,
    connectivity: int = 4,
    nodata_val: Optional[int] = 255,
) -> str:
    """Sieves blobs smaller than `min_pixel_size`, then reverts any merge of a
    non-target clump into a target class unless that clump is fully encapsulated by
    target pixels -- touching NoData or another class aborts the merge.

    `nodata_val` names pixels that hold no data and must be excluded from sieving --
    255 for the NDVI/RF map, whose unclassified pixels really are absent. Pass **None**
    when every pixel carries a real class, as in the static classification: naming a
    legitimate class as nodata masks it out of `rasterio.features.sieve`, leaving small
    target blobs with no valid neighbour to merge into, so the sieve silently does
    nothing at all.
    """
    in_path = Path(input_raster_path)
    out_path = in_path.parent / f"{in_path.stem}_sieved_p{min_pixel_size}{in_path.suffix}"

    if out_path.exists():
        print(f"[Skipped] Sieved raster already exists at: {out_path}")
        return str(out_path)

    print(f"Loading categorical map for strict sieving: {in_path.name}")
    with rasterio.open(in_path) as src:
        profile = src.profile
        data = src.read(1)
        if data.dtype != np.uint8:
            data = data.astype(np.uint8)
            profile.update(dtype=rasterio.uint8)

    print(f"Phase 1: base sieve filter (removing blobs < {min_pixel_size} px)...")
    if nodata_val is None:
        # Every pixel is a real class, so the whole raster participates.
        sieved = sieve(data, size=min_pixel_size, connectivity=connectivity)
    else:
        valid_mask = data != nodata_val
        sieved = sieve(data, size=min_pixel_size, connectivity=connectivity, mask=valid_mask)
        del valid_mask

    print("Phase 2: enforcing strict topological encapsulation...")
    structure = generate_binary_structure(2, 1 if connectivity == 4 else 2)

    target_originally = _class_membership_mask(data, target_classes)
    not_target = ~target_originally
    del target_originally

    # Pixels the sieve flipped from non-target into a target class.
    changed = not_target & _class_membership_mask(sieved, target_classes)

    # "Bad" neighbours: originally non-target pixels that the sieve did NOT flip. A
    # changed clump touching one of these was merged across a real class boundary.
    bad_neighbours = not_target & ~changed
    del not_target

    touching_bad = binary_dilation(bad_neighbours, structure=structure)
    del bad_neighbours
    touching_bad &= changed

    clump_ids, clump_count = label(changed, structure=structure)
    del changed

    if clump_count:
        bad_clump_ids = np.unique(clump_ids[touching_bad])
        bad_clump_ids = bad_clump_ids[bad_clump_ids != 0]
        del touching_bad

        if bad_clump_ids.size:
            print(f"  -> Reverting {bad_clump_ids.size} illegal edge-merges...")
            # Label lookup table: O(clump_count) memory and a single pass, versus
            # np.isin building sort buffers over the whole int32 label raster.
            is_bad_clump = np.zeros(clump_count + 1, dtype=bool)
            is_bad_clump[bad_clump_ids] = True
            revert = is_bad_clump[clump_ids]
            del clump_ids, is_bad_clump
            np.copyto(sieved, data, where=revert)
            del revert
        else:
            del clump_ids
    else:
        del touching_bad, clump_ids

    if nodata_val is not None:
        np.copyto(sieved, np.uint8(nodata_val), where=(data == nodata_val))
    del data
    gc.collect()

    print("Writing topologically-enforced output to disk...")
    # Force GTiff: `profile` came from the input, whose driver could be VRT or another
    # format that cannot be written through.
    profile.update(driver="GTiff", compress="lzw", tiled=True,
                   blockxsize=256, blockysize=256, bigtiff="YES")
    staging_path = Path(str(out_path) + ".tmp.tif")
    try:
        with rasterio.open(staging_path, "w", **profile) as dst:
            dst.write(sieved, 1)
    except BaseException:
        staging_path.unlink(missing_ok=True)
        raise
    os.replace(staging_path, out_path)
    del sieved
    gc.collect()

    print(f"SUCCESS: sieved raster saved to: {out_path}")
    return str(out_path)


def _export_vector_layer(gdf, gpkg_output, zip_output, out_base_path, output_basename,
                         save_shp_zip: bool) -> None:
    """Writes the GPKG (and optional zipped Shapefile). Geometries are cast to
    MultiPolygon: a layer holding a mix of Polygon and MultiPolygon is rejected on
    append, and mixed-type layers confuse some GIS clients even on a fresh write."""
    gdf = gdf.copy()
    if not gdf.empty:
        gdf["geometry"] = [
            geom if geom is None or geom.geom_type == "MultiPolygon" else MultiPolygon([geom])
            for geom in gdf.geometry
        ]
    gdf.to_file(gpkg_output, driver="GPKG")
    print(f"   -> Saved GPKG: {gpkg_output}")

    if save_shp_zip:
        with tempfile.TemporaryDirectory() as tmpdir:
            gdf.to_file(Path(tmpdir) / f"{output_basename}.shp", driver="ESRI Shapefile")
            shutil.make_archive(base_name=str(out_base_path), format="zip", root_dir=tmpdir)
        print(f"   -> Saved ZIP (Shapefile): {zip_output}")


def _export_empty_layer(crs, gpkg_output, zip_output, out_base_path, output_basename,
                        save_shp_zip: bool, reason: str) -> str:
    """A district with genuinely no crop is a valid answer, not a failure. Returning None
    made every caller -- above all batch runs -- treat it as a crash. Write the layer with
    the real schema and no rows, so downstream stages read it like any other."""
    print(f"  -> Writing an empty layer with the full schema ({reason}).")
    empty = gpd.GeoDataFrame(
        {"predicted": pd.Series(dtype="int64"),
         "area_acres": pd.Series(dtype="float64"),
         "geometry": gpd.GeoSeries([], crs=crs)},
        geometry="geometry", crs=crs,
    )
    _export_vector_layer(empty, gpkg_output, zip_output, out_base_path, output_basename, save_shp_zip)
    return gpkg_output


def vectorize_process_and_export(
    input_raster_path: Union[str, Path],
    boundary_shp_path: Union[str, Path],
    output_dir: Union[str, Path],
    output_basename: str,
    target_labels: Union[int, List[int]],
    relabel_as: int = 3015,
    min_area_acres: float = 0.5,
    save_shp_zip: bool = True,
    dissolve_polygons: bool = False,
    write_empty_outputs: bool = True,
) -> Optional[str]:
    """Vectorise -> clip to AOI boundary -> explode to singlepart -> filter by area ->
    relabel -> export GPKG (+ optional zipped Shapefile).

    `dissolve_polygons` is off by default: `rasterio.features.shapes` already returns
    maximal connected regions, so the original notebooks' dissolve-then-explode round
    trip was a very expensive near no-op. Set it True to restore the old behaviour.
    """
    out_dir_path = Path(output_dir) / "final_output"
    out_dir_path.mkdir(parents=True, exist_ok=True)
    out_base_path = out_dir_path / output_basename
    gpkg_output = f"{out_base_path}.gpkg"
    zip_output = f"{out_base_path}.zip"
    source_record_path = out_base_path.with_suffix(".source.json")

    # Resuming must not hand back polygons built from a *different* raster. A filename
    # check alone cannot tell: the output name depends only on the AOI, crop and year, so
    # a re-run that recomputes the static stage would return the previous run's product.
    # Skip only when the recorded source raster is byte-for-byte the one we were handed.
    current_source = _describe_source_raster(input_raster_path)
    outputs_present = Path(gpkg_output).exists() and (not save_shp_zip or Path(zip_output).exists())
    if outputs_present:
        if _read_source_record(source_record_path) == current_source:
            print(f"[Skipped] Vectorised outputs already exist for: {output_basename}")
            return gpkg_output
        print(
            f"[Rebuilding] {output_basename} exists but was built from a different raster "
            f"({_read_source_record(source_record_path, 'source_raster') or 'unrecorded'}); "
            f"regenerating from {Path(input_raster_path).name}."
        )

    if isinstance(target_labels, int):
        target_labels = [target_labels]

    print(f"1. Vectorising target classes {target_labels}...")
    with rasterio.open(input_raster_path) as src:
        image = src.read(1)
        transform = src.transform
        raster_crs = src.crs
        raster_nodata = src.nodata

    def _finish_empty(reason: str) -> str:
        path = _export_empty_layer(raster_crs, gpkg_output, zip_output, out_base_path,
                                   output_basename, save_shp_zip, reason)
        _write_source_record(source_record_path, current_source)
        return path

    target_mask = _class_membership_mask(image, target_labels)
    if not target_mask.any():
        # An all-nodata raster is a failed acquisition wearing the same clothes as a
        # crop-free district. Only the second deserves an empty layer.
        if raster_nodata is not None and not (image != raster_nodata).any():
            raise RuntimeError(
                f"{Path(input_raster_path).name} is nodata in every pixel -- nothing was "
                "classified here. Writing an empty result would report an acquisition "
                "failure as a district with no crop."
            )
        print(f"  [Warning] No pixels found for classes {target_labels}.")
        if not write_empty_outputs:
            return None
        return _finish_empty(f"no pixels of classes {target_labels}")

    geometries = [shape(geom) for geom, _ in shapes(image, mask=target_mask, transform=transform)]
    del image, target_mask
    gc.collect()

    gdf = gpd.GeoDataFrame(geometry=geometries, crs=raster_crs)
    del geometries
    print(f"  -> Extracted {len(gdf):,} raw polygon features.")

    # `shapes` output is rectilinear and valid by construction; only repair the rare
    # exception rather than paying make_valid on every polygon.
    invalid = ~gdf.geometry.is_valid
    if invalid.any():
        print(f"  -> Repairing {int(invalid.sum()):,} invalid geometries...")
        gdf.loc[invalid, "geometry"] = gdf.loc[invalid, "geometry"].make_valid()
    gdf = gdf[gdf.geom_type.isin(["Polygon", "MultiPolygon"])]

    if not dissolve_polygons and min_area_acres > 0:
        # Clipping can only shrink a polygon, so anything already under the threshold
        # can never survive it -- drop those now instead of clipping them first.
        # (Skipped when dissolving, where polygons may still merge and grow.)
        pre_clip_acres = gdf.geometry.to_crs(epsg=AREA_CRS_EPSG).area * ACRES_PER_SQ_METRE
        before = len(gdf)
        gdf = gdf[pre_clip_acres >= min_area_acres]
        print(f"  -> Pre-filter dropped {before - len(gdf):,} sub-threshold polygons before clipping.")
        if gdf.empty:
            print("  [Warning] No features above the area threshold.")
            if not write_empty_outputs:
                return None
            return _finish_empty("no features above the area threshold")

    print("2. Loading boundary for clipping...")
    boundary_gdf = gpd.read_file(boundary_shp_path)
    if boundary_gdf.empty:
        print("  [Error] Boundary shapefile is empty. Aborting.")
        return None

    print("3. Clip to boundary, explode to singlepart...")
    clipped_gdf = gpd.clip(gdf, boundary_gdf.to_crs(raster_crs))
    del gdf
    gc.collect()
    if clipped_gdf.empty:
        print("  [Warning] No features intersect the boundary.")
        if not write_empty_outputs:
            return None
        return _finish_empty("no features intersect the boundary")

    if dissolve_polygons:
        clipped_gdf = clipped_gdf.dissolve()

    singlepart_gdf = clipped_gdf.explode(index_parts=False).reset_index(drop=True)
    del clipped_gdf
    singlepart_gdf = singlepart_gdf[
        (singlepart_gdf.geom_type == "Polygon") & (~singlepart_gdf.is_empty)
    ]

    print(f"4. Computing area (EPSG:{AREA_CRS_EPSG}) and filtering (>= {min_area_acres} acres)...")
    singlepart_gdf["area_acres"] = (
        singlepart_gdf.geometry.to_crs(epsg=AREA_CRS_EPSG).area * ACRES_PER_SQ_METRE
    )
    singlepart_gdf = singlepart_gdf[singlepart_gdf["area_acres"] >= min_area_acres]
    if singlepart_gdf.empty:
        print("  [Warning] No features remained after area filtering.")
        if not write_empty_outputs:
            return None
        return _finish_empty("no features above the area threshold after clipping")

    singlepart_gdf["predicted"] = relabel_as
    singlepart_gdf = singlepart_gdf[["predicted", "area_acres", "geometry"]]
    print(f"   Retained {len(singlepart_gdf):,} features, "
          f"{singlepart_gdf['area_acres'].sum():,.2f} acres, label {relabel_as}")

    print("5. Exporting...")
    _export_vector_layer(singlepart_gdf, gpkg_output, zip_output, out_base_path,
                         output_basename, save_shp_zip)

    _write_source_record(source_record_path, current_source)

    del singlepart_gdf
    gc.collect()
    return gpkg_output
