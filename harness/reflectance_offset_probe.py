"""Do the two static backends deliver the same reflectance scale?

GEE's COPERNICUS/S2_SR_HARMONIZED applies the processing-baseline-04.00 BOA_ADD_OFFSET
(-1000) so post-Jan-2022 scenes match the older radiometry. The Planetary Computer's
sentinel-2-l2a serves the raw baseline-04.00 values. If the pipeline reads MPC without
applying the offset, every STAC static band is ~1000 DN higher than the GEE equivalent
for the same date -- and the XGBoost model sees systematically different features.
"""
import numpy as np

BBOX = [73.40, 30.90, 73.42, 30.92]   # small patch inside the test AOI
DATE = "2025-11-10"

# ---------------------------------------------------------------- STAC / MPC
import planetary_computer
import pystac_client
import odc.stac

catalog = pystac_client.Client.open(
    "https://planetarycomputer.microsoft.com/api/stac/v1",
    modifier=planetary_computer.sign_inplace,
)
items = list(catalog.search(collections=["sentinel-2-l2a"], bbox=BBOX,
                            datetime=f"{DATE}/{DATE}").items())
print(f"MPC items on {DATE}: {len(items)}")
item = items[0]
print("  id:", item.id)
print("  processing baseline:", item.properties.get("s2:processing_baseline"))
for asset in ("B04", "B08"):
    extra = item.assets[asset].extra_fields.get("raster:bands")
    print(f"  {asset} raster:bands metadata: {extra}")

data = odc.stac.load([item], bands=["B04", "B08"], bbox=BBOX, resolution=0.0000898,
                     crs="EPSG:4326", chunks={})
mpc_red = np.asarray(data["B04"].isel(time=0).values, dtype=np.float64)
mpc_nir = np.asarray(data["B08"].isel(time=0).values, dtype=np.float64)
mpc_red = mpc_red[np.isfinite(mpc_red) & (mpc_red > 0)]
mpc_nir = mpc_nir[np.isfinite(mpc_nir) & (mpc_nir > 0)]
print(f"\nMPC  B04 mean={mpc_red.mean():8.1f}  median={np.median(mpc_red):8.1f}  n={mpc_red.size}")
print(f"MPC  B08 mean={mpc_nir.mean():8.1f}  median={np.median(mpc_nir):8.1f}  n={mpc_nir.size}")

# ---------------------------------------------------------------- GEE
import ee
from google.oauth2 import service_account

KEY = "/home/jovyan/FAO/optimized_code_testing/gcs_data_downloader_ee_farmdar.json"
creds = service_account.Credentials.from_service_account_file(KEY).with_scopes([
    "https://www.googleapis.com/auth/earthengine",
    "https://www.googleapis.com/auth/cloud-platform"])
ee.Initialize(credentials=creds, project="ee-farmdar",
              opt_url="https://earthengine-highvolume.googleapis.com")

region = ee.Geometry.Rectangle(BBOX)
for collection_id in ("COPERNICUS/S2_SR_HARMONIZED", "COPERNICUS/S2_SR"):
    try:
        coll = (ee.ImageCollection(collection_id).filterBounds(region)
                .filterDate(DATE, "2025-11-11"))
        n = coll.size().getInfo()
        if not n:
            print(f"\nGEE {collection_id}: no scenes")
            continue
        stats = coll.mosaic().select(["B4", "B8"]).reduceRegion(
            reducer=ee.Reducer.mean(), geometry=region, scale=10, maxPixels=1e9).getInfo()
        print(f"\nGEE  {collection_id} ({n} scenes)")
        print(f"     B4 mean={stats.get('B4'):8.1f}   B8 mean={stats.get('B8'):8.1f}")
    except Exception as exc:
        print(f"\nGEE {collection_id}: {type(exc).__name__}: {exc}")

print("\nA persistent ~1000 DN gap (MPC higher) is the unapplied BOA_ADD_OFFSET.")
