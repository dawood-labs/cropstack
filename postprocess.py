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
import shutil
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence, Union

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.features import shapes, sieve
from scipy.ndimage import binary_dilation, generate_binary_structure, label
from shapely.geometry import shape

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


def apply_strict_directional_sieve(
    input_raster_path: Union[str, Path],
    target_classes: List[int],
    min_pixel_size: int = 15,
    connectivity: int = 4,
    nodata_val: int = 255,
) -> str:
    """Sieves blobs smaller than `min_pixel_size`, then reverts any merge of a
    non-target clump into a target class unless that clump is fully encapsulated by
    target pixels -- touching NoData or another class aborts the merge.

    Pass the raster's real background value as `nodata_val` (255 for the NDVI/RF
    classification map, the static background label for the static classification).
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

    np.copyto(sieved, np.uint8(nodata_val), where=(data == nodata_val))
    del data
    gc.collect()

    print("Writing topologically-enforced output to disk...")
    # Force GTiff: `profile` came from the input, whose driver could be VRT or another
    # format that cannot be written through.
    profile.update(driver="GTiff", compress="lzw", tiled=True,
                   blockxsize=256, blockysize=256, bigtiff="YES")
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(sieved, 1)
    del sieved
    gc.collect()

    print(f"SUCCESS: sieved raster saved to: {out_path}")
    return str(out_path)


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

    if Path(gpkg_output).exists() and (not save_shp_zip or Path(zip_output).exists()):
        print(f"[Skipped] Vectorised outputs already exist for: {output_basename}")
        return gpkg_output

    if isinstance(target_labels, int):
        target_labels = [target_labels]

    print(f"1. Vectorising target classes {target_labels}...")
    with rasterio.open(input_raster_path) as src:
        image = src.read(1)
        transform = src.transform
        raster_crs = src.crs

    target_mask = _class_membership_mask(image, target_labels)
    if not target_mask.any():
        print(f"  [Warning] No pixels found for classes {target_labels}. Aborting.")
        return None

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
            print("  [Warning] No features above the area threshold. Aborting.")
            return None

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
        print("  [Warning] No features intersect the boundary. Aborting.")
        return None

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
        print("  [Warning] No features remained after area filtering. Aborting.")
        return None

    singlepart_gdf["predicted"] = relabel_as
    singlepart_gdf = singlepart_gdf[["predicted", "area_acres", "geometry"]]
    print(f"   Retained {len(singlepart_gdf):,} features, "
          f"{singlepart_gdf['area_acres'].sum():,.2f} acres, label {relabel_as}")

    print("5. Exporting...")
    singlepart_gdf.to_file(gpkg_output, driver="GPKG")
    print(f"   -> Saved GPKG: {gpkg_output}")

    if save_shp_zip:
        with tempfile.TemporaryDirectory() as tmpdir:
            singlepart_gdf.to_file(Path(tmpdir) / f"{output_basename}.shp", driver="ESRI Shapefile")
            shutil.make_archive(base_name=str(out_base_path), format="zip", root_dir=tmpdir)
        print(f"   -> Saved ZIP (Shapefile): {zip_output}")

    del singlepart_gdf
    gc.collect()
    return gpkg_output
