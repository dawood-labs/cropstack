"""The BOA_ADD_OFFSET only exists from processing baseline 04.00 (Jan 2022 onward).
If that is the cause, MPC and GEE-HARMONIZED must agree on a 2016 scene and diverge by
1000 DN on a 2025 one -- and the NDVI the models actually consume must diverge with it.
"""
import numpy as np
import odc.stac
import planetary_computer
import pystac_client
import ee
from google.oauth2 import service_account

BBOX = [73.40, 30.90, 73.42, 30.92]
BBOX_2016 = [73.3384, 30.853, 73.5179, 30.9988]  # 2016 scenes only partly cover the patch
KEY = "/home/jovyan/FAO/optimized_code_testing/gcs_data_downloader_ee_farmdar.json"

creds = service_account.Credentials.from_service_account_file(KEY).with_scopes([
    "https://www.googleapis.com/auth/earthengine",
    "https://www.googleapis.com/auth/cloud-platform"])
ee.Initialize(credentials=creds, project="ee-farmdar",
              opt_url="https://earthengine-highvolume.googleapis.com")
catalog = pystac_client.Client.open(
    "https://planetarycomputer.microsoft.com/api/stac/v1",
    modifier=planetary_computer.sign_inplace)
region = ee.Geometry.Rectangle(BBOX)


def compare(date, next_date, bbox=None):
    global BBOX
    saved = BBOX
    if bbox: BBOX = bbox
    items = list(catalog.search(collections=["sentinel-2-l2a"], bbox=BBOX,
                                datetime=f"{date}/{next_date}").items())
    if not items:
        print(f"\n{date}: no MPC item")
        return
    item = items[0]
    baseline = item.properties.get("s2:processing_baseline")
    data = odc.stac.load([item], bands=["B04", "B08"], bbox=BBOX,
                         resolution=0.0000898, crs="EPSG:4326", chunks={})
    red = np.asarray(data["B04"].isel(time=0).values, float)
    nir = np.asarray(data["B08"].isel(time=0).values, float)
    good = np.isfinite(red) & np.isfinite(nir) & (red > 0) & (nir > 0)
    red, nir = red[good], nir[good]
    mpc_red, mpc_nir = red.mean(), nir.mean()
    mpc_ndvi = float(np.mean((nir - red) / (nir + red)))

    coll = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterBounds(region).filterDate(date, next_date))
    if not coll.size().getInfo():
        print(f"\n{date}: no GEE scene")
        return
    image = coll.mosaic()
    ndvi_img = image.normalizedDifference(["B8", "B4"]).rename("NDVI")
    stats = image.select(["B4", "B8"]).addBands(ndvi_img).reduceRegion(
        reducer=ee.Reducer.mean(), geometry=region, scale=10, maxPixels=1e9).getInfo()
    gee_red, gee_nir, gee_ndvi = stats["B4"], stats["B8"], stats["NDVI"]

    print(f"\n=== {date}  (MPC processing baseline {baseline}) ===")
    print(f"  {'':10}{'MPC':>10}{'GEE_harm':>11}{'delta':>10}")
    print(f"  {'B04/B4':10}{mpc_red:>10.1f}{gee_red:>11.1f}{mpc_red-gee_red:>+10.1f}")
    print(f"  {'B08/B8':10}{mpc_nir:>10.1f}{gee_nir:>11.1f}{mpc_nir-gee_nir:>+10.1f}")
    print(f"  {'NDVI':10}{mpc_ndvi:>10.4f}{gee_ndvi:>11.4f}{mpc_ndvi-gee_ndvi:>+10.4f}"
          f"   ({100*(mpc_ndvi-gee_ndvi)/gee_ndvi:+.1f}% relative)")


compare("2016-11-14", "2016-11-15", BBOX_2016)
compare("2016-10-15", "2016-10-16", BBOX_2016)
