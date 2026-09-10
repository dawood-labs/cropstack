"""Per-worker RSS cost of each static XGBoost model.

`classify_static_image` spawns `cpu_count()-1` workers and each loads its OWN copy of
the model (by design -- nothing large is pickled per task). So peak memory for the
static stage is roughly worker_count x this number.
"""
import os, sys

def rss_mb():
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    return 0.0

if len(sys.argv) > 1:
    path = sys.argv[1]
    base = rss_mb()
    import xgboost as xgb
    after_import = rss_mb()
    model = xgb.XGBClassifier()
    model.load_model(path)
    loaded = rss_mb()
    import numpy as np
    n_feat = model.n_features_in_
    X = np.zeros((2048 * 2048 // 10, n_feat), dtype=np.float32)   # a realistic window
    model.predict(X)
    after_predict = rss_mb()
    print(f"  disk {os.path.getsize(path)/1e6:>7.1f} MB | import {after_import-base:>5.0f} MB"
          f" | +model {loaded-after_import:>7.0f} MB | +predict {after_predict-loaded:>6.0f} MB"
          f" | WORKER TOTAL {after_predict:>7.0f} MB"
          f" | feats {n_feat} trees {model.get_booster().num_boosted_rounds()}")
    sys.exit(0)

CACHE = os.path.expanduser("~/.cache/fao_pipeline/models/farmdar_data_catalog")
MODELS = {
    "wheat":     f"{CACHE}/FAO_Wheat_Model_Files/FAO_Wheat_Static_IMG_Model/FAO_Wheat_XGB_Model.json",
    "cane":      f"{CACHE}/fao_cane_model_file/fao_cane_xgb_model.json",
    "spr_maize": f"{CACHE}/FAO_SPR_MAIZE_MODELS/FAO_Spr_Maize_Static_IMG_Model/FAO_Spr_Maize_XGB_Static_IMG_Model.json",
}
for crop, path in MODELS.items():
    print(f"{crop}:")
    os.system(f"{sys.executable} {__file__} {path}")
