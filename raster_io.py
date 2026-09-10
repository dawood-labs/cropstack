"""Memory-bounded raster mosaicking.

`rasterio.merge.merge()` allocates the *entire* destination mosaic in RAM before
writing it, which is fine for a couple of small tiles and catastrophic for a district
at 10 m (a 40-band float32 smoothed stack over a large district runs to tens of GB).

`mosaic_geotiffs` instead builds a GDAL VRT (an XML index, no pixels) and streams it
into a tiled GeoTIFF block by block, so peak memory stays at a few blocks regardless of
the mosaic's size. It falls back to `rasterio.merge` only if GDAL's Python bindings
are unavailable.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List, Optional, Sequence, Union

import rasterio

logger = logging.getLogger(__name__)

GTIFF_CREATION_OPTIONS = [
    "TILED=YES",
    "BLOCKXSIZE=256",
    "BLOCKYSIZE=256",
    "COMPRESS=LZW",
    "BIGTIFF=YES",
    "NUM_THREADS=ALL_CPUS",
]


def _apply_band_metadata(
    output_path: Path, descriptions: Sequence[Optional[str]], band_name: Optional[str], tags: dict,
) -> None:
    """Writes band descriptions/tags onto the finished mosaic. Cheap: metadata only,
    no pixel data is rewritten."""
    try:
        with rasterio.open(output_path, "r+") as dst:
            if band_name and dst.count == 1:
                dst.set_band_description(1, band_name)
            elif descriptions:
                for index, description in enumerate(descriptions[: dst.count], start=1):
                    if description:
                        dst.set_band_description(index, description)
            if tags:
                dst.update_tags(**tags)
    except Exception as exc:  # metadata is nice-to-have; never fail a finished mosaic over it
        logger.warning(f"Could not write band metadata onto {output_path.name}: {exc}")


def _mosaic_with_gdal(paths: List[str], output_path: Path, nodata, resampling: str) -> bool:
    try:
        from osgeo import gdal
    except ImportError:
        return False

    gdal.UseExceptions()
    vrt_path = output_path.with_suffix(".mosaic.vrt")
    try:
        return _run_gdal_mosaic(gdal, paths, output_path, vrt_path, nodata, resampling)
    except Exception as exc:
        # Never let a GDAL quirk end a long run: fall back to the rasterio path instead.
        logger.warning(f"GDAL mosaic failed ({exc}); falling back to rasterio.merge.")
        return False
    finally:
        if vrt_path.exists():
            try:
                os.remove(vrt_path)
            except OSError:
                pass


def _run_gdal_mosaic(gdal, paths: List[str], output_path: Path, vrt_path: Path, nodata, resampling: str) -> bool:
    # A VRT paints later sources over earlier ones, whereas rasterio's merge uses
    # method="first" (earlier file wins). Reversing the list preserves the original
    # pipelines' seam behaviour where tiles overlap.
    build_options = gdal.BuildVRTOptions(
        resampleAlg=resampling,
        srcNodata=nodata if nodata is not None else None,
        VRTNodata=nodata if nodata is not None else None,
    )
    vrt = gdal.BuildVRT(str(vrt_path), list(reversed(paths)), options=build_options)
    if vrt is None:
        return False
    vrt = None  # closing the handle flushes the VRT to disk

    # Translate streams the VRT block by block, so peak memory stays at a few blocks
    # no matter how large the mosaic is.
    translate_options = gdal.TranslateOptions(
        format="GTiff",
        creationOptions=GTIFF_CREATION_OPTIONS,
        noData=nodata if nodata is not None else None,
    )
    gdal.Translate(str(output_path), str(vrt_path), options=translate_options)
    return True


def _mosaic_with_rasterio(paths: List[str], output_path: Path, nodata) -> None:
    """Fallback path. Holds the full mosaic in memory -- only used when GDAL's Python
    bindings are missing."""
    from rasterio.merge import merge

    logger.warning("osgeo.gdal unavailable; falling back to in-memory rasterio.merge.")
    sources = [rasterio.open(p) for p in paths]
    try:
        mosaic, transform = merge(sources, res=sources[0].res, nodata=nodata, method="first")
        meta = sources[0].meta.copy()
        meta.update({
            "driver": "GTiff", "height": mosaic.shape[1], "width": mosaic.shape[2],
            "transform": transform, "nodata": nodata, "compress": "lzw",
            "tiled": True, "blockxsize": 256, "blockysize": 256, "bigtiff": "YES",
        })
        with rasterio.open(output_path, "w", **meta) as dst:
            dst.write(mosaic)
        del mosaic
    finally:
        for source in sources:
            source.close()


def mosaic_geotiffs(
    input_paths: Sequence[Union[str, Path]],
    output_path: Union[str, Path],
    nodata: Optional[float] = None,
    band_name: Optional[str] = None,
    resampling: str = "nearest",
) -> Path:
    """Mosaics `input_paths` into one tiled, LZW-compressed GeoTIFF.

    `nodata` defaults to the first source's nodata value. `band_name` names the single
    band of a categorical mosaic; multi-band mosaics inherit the first source's band
    descriptions.
    """
    paths = [str(p) for p in input_paths]
    if not paths:
        raise ValueError("mosaic_geotiffs called with no input rasters.")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with rasterio.open(paths[0]) as first:
        if nodata is None:
            nodata = first.nodata
        descriptions = list(first.descriptions)
        tags = first.tags()

    logger.info(f"Mosaicking {len(paths)} raster(s) -> {output_path.name}")
    if not _mosaic_with_gdal(paths, output_path, nodata, resampling):
        _mosaic_with_rasterio(paths, output_path, nodata)

    _apply_band_metadata(output_path, descriptions, band_name, tags)
    return output_path
