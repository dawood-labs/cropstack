"""Model resolution and permanent on-disk caching.

A `ModelSource` is turned into a concrete local file path exactly once per machine:

* a local path is returned as-is (manual override, nothing is copied or cached);
* a `gs://` URI is downloaded into the model cache the first time it is seen, and every
  later run reads it straight from the cache without touching the network.

Each model carries its own optional service-account key, so models living in different
buckets under different credentials can be mixed in one run.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Union

from config import DEFAULT_MODEL_CACHE_DIR, ModelSource

logger = logging.getLogger(__name__)

# Guards concurrent first-time downloads within a single process.
_download_lock = threading.Lock()


def cache_path_for(gcs_uri: str, cache_dir: Union[str, Path] = DEFAULT_MODEL_CACHE_DIR) -> Path:
    """Deterministic cache location: <cache_dir>/<bucket>/<blob path>.

    Mirroring the bucket layout keeps the cache self-describing (you can see at a
    glance which model came from where) and collision-free across buckets.
    """
    bucket_and_blob = gcs_uri.replace("gs://", "").strip("/")
    return Path(os.path.expanduser(str(cache_dir))) / bucket_and_blob


def resolve_model(
    source: Union[ModelSource, str, Path, Dict[str, Any]],
    cache_dir: Union[str, Path] = DEFAULT_MODEL_CACHE_DIR,
    default_key_path: Optional[str] = None,
    force_refresh: bool = False,
    label: str = "model",
) -> Path:
    """Returns a local path to the model file, downloading and caching it if needed.

    `default_key_path` is the pipeline's own GEE/GCS key, used only when the model
    itself does not specify `gcs_key_path`.
    """
    source = ModelSource.coerce(source)
    source.validate(label)

    if source.local_path:
        # Same cross-platform spelling problem as AOIs: a Windows path handed to code
        # running under WSL (or the reverse) needs translating before it will resolve.
        import aoi_io

        local = aoi_io.resolve_local_path(source.local_path)
        if local is None:
            raise FileNotFoundError(f"{label}: local model file not found: {source.local_path}")
        logger.info(f"{label}: using local file {local}")
        return local

    cached = cache_path_for(source.gcs_uri, cache_dir)
    meta_path = cached.with_suffix(cached.suffix + ".meta.json")

    if cached.exists() and not force_refresh:
        logger.info(f"{label}: cache hit, using {cached}")
        return cached

    key_path = source.gcs_key_path or default_key_path
    if not key_path:
        raise ValueError(
            f"{label}: downloading {source.gcs_uri} needs a service-account key -- set "
            "the model's gcs_key_path, or the pipeline's gee_service_account_key."
        )
    if not Path(key_path).exists():
        raise FileNotFoundError(f"{label}: service-account key not found: {key_path}")

    import gcs_io  # imported lazily so a purely-local run needs no google-cloud deps

    with _download_lock:
        # Re-check under the lock: another thread may have just populated the cache.
        if cached.exists() and not force_refresh:
            return cached

        cached.parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"{label}: downloading {source.gcs_uri} -> {cached}")
        blob_size = gcs_io.download_gcs_file(source.gcs_uri, cached, key_path)

        meta_path.write_text(json.dumps({
            "gcs_uri": source.gcs_uri,
            "bytes": blob_size,
            "key_path": str(key_path),
        }, indent=2))

    logger.info(f"{label}: cached at {cached} ({blob_size / (1024 * 1024):.1f} MB)")
    return cached


def resolve_pipeline_models(cfg, force_refresh: bool = False) -> Dict[str, Optional[Path]]:
    """Resolves both of a run's models up front, so a missing/unreachable model fails
    before any imagery is acquired rather than an hour into the run."""
    resolved: Dict[str, Optional[Path]] = {
        "ndvi_model": resolve_model(
            cfg.ndvi_model, cfg.model_cache_dir, cfg.gee_service_account_key,
            force_refresh=force_refresh, label="ndvi_model",
        ),
        "static_model": None,
    }
    if cfg.run_static_model and cfg.static_model is not None:
        resolved["static_model"] = resolve_model(
            cfg.static_model, cfg.model_cache_dir, cfg.gee_service_account_key,
            force_refresh=force_refresh, label="static_model",
        )
    return resolved
