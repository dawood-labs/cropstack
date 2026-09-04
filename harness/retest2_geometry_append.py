"""RETEST_2: geometry-type check and the two-district append that used to fail."""
from __future__ import annotations
import json, sys, tempfile
from pathlib import Path
import geopandas as gpd, pandas as pd, pyogrio

ROOT = Path("/home/jovyan/FAO/optimized_code_testing")


def describe(path):
    p = Path(path)
    if not p.exists():
        return {"path": str(p), "exists": False}
    info = pyogrio.read_info(p)
    gdf = gpd.read_file(p)
    types = sorted(set(gdf.geometry.geom_type)) if len(gdf) else []
    return {"path": str(p), "exists": True, "rows": int(len(gdf)),
            "declared_geometry_type": info.get("geometry_type"),
            "actual_geom_types": types, "columns": list(gdf.columns),
            "dtypes": {c: str(t) for c, t in gdf.dtypes.items()},
            "crs": str(gdf.crs), "acres": round(float(gdf["area_acres"].sum()), 2)
            if "area_acres" in gdf and len(gdf) else 0.0}


def append_test(a, b, label):
    """Write A, then append B into the SAME layer -- the operation the field log said failed."""
    out = {"label": label, "a": str(a), "b": str(b)}
    try:
        ga, gb = gpd.read_file(a), gpd.read_file(b)
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "combined.gpkg"
            ga.to_file(target, layer="crop", driver="GPKG")
            gb.to_file(target, layer="crop", driver="GPKG", mode="a")
            back = gpd.read_file(target, layer="crop")
            out.update(status="ok", rows_a=len(ga), rows_b=len(gb), rows_combined=len(back),
                       combined_geom_types=sorted(set(back.geometry.geom_type)) if len(back) else [])
    except BaseException as exc:
        out.update(status="raised", error=f"{type(exc).__name__}: {exc}")
    return out


if __name__ == "__main__":
    targets = sys.argv[1:]
    report = {"layers": [describe(t) for t in targets]}
    real = [t for t in targets if Path(t).exists() and len(gpd.read_file(t))]
    if len(real) >= 2:
        report["append_two_districts"] = append_test(real[0], real[1], "two non-empty results")
    empties = [t for t in targets if Path(t).exists() and not len(gpd.read_file(t))]
    if real and empties:
        report["append_empty_into_real"] = append_test(real[0], empties[0], "real + empty result")
    print(json.dumps(report, indent=2, default=str))
    (ROOT / "metrics" / "retest2_geometry_append.json").write_text(json.dumps(report, indent=2, default=str))
