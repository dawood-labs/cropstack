"""Shared GCS download helpers used by every GEE-sourced path -- NDVI grid stacks,
static composites, and model files -- whether they were auto-exported by this pipeline
or exported by hand from the GEE Code Editor.

Consolidates notebook 1's `download_and_mosaic_gcs_chunks` and notebook 3's
`download_gcs_object`, which did the same thing: list blobs matching a URI prefix,
detect GEE's sharded export naming (``<basename><10 digits>-<10 digits>.tif``),
download concurrently, mosaic the shards. Every GEE-originated download in the pipeline
goes through `download_gcs_object`, so sharding is handled uniformly everywhere.
"""
from __future__ import annotations

import concurrent.futures
import logging
import re
import tempfile
import threading
from pathlib import Path
from typing import Union

from google.cloud import storage
from google.oauth2 import service_account
from tqdm import tqdm

import raster_io

logging.getLogger("google.cloud.storage.blob").setLevel(logging.WARNING)
logging.getLogger("google.resumable_media").setLevel(logging.WARNING)
logging.getLogger("google.cloud.storage").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Matches both "basename.tif" and GEE's sharded export naming "basename0000000000-0000018944.tif".
_SHARD_SUFFIX = r"(?:\d{10}-\d{10})?\.tif$"

_CLIENT_CACHE: dict = {}


def gcs_client(key_path: Union[str, Path]) -> storage.Client:
    """Returns a cached GCS client for a given service-account key, so repeated calls
    (e.g. one per model or per grid tile) don't re-parse credentials each time."""
    key = str(key_path)
    client = _CLIENT_CACHE.get(key)
    if client is None:
        credentials = service_account.Credentials.from_service_account_file(key)
        client = storage.Client(credentials=credentials)
        _CLIENT_CACHE[key] = client
    return client


def _parse_uri(uri: str) -> tuple:
    path = uri.replace("gs://", "")
    if "/" not in path:
        raise ValueError(f"Malformed GCS URI (expected gs://bucket/path): {uri}")
    bucket_name, blob_path = path.split("/", 1)
    return bucket_name, blob_path


def _download_blob_concurrently(blob, local_path: Path, max_workers: int, chunk_size: int) -> Path:
    """Downloads one GCS blob via parallel byte-range requests; skips the download when
    the local file already exists and matches the remote size."""
    blob.reload()
    file_size = blob.size
    file_name = Path(blob.name).name

    if local_path.exists() and local_path.stat().st_size == file_size:
        logger.info(f"Already downloaded: {local_path} ({file_size / (1024 * 1024):.2f} MB)")
        return local_path

    byte_ranges = []
    start_byte = 0
    while start_byte < file_size:
        end_byte = min(start_byte + chunk_size - 1, file_size - 1)
        byte_ranges.append((start_byte, end_byte))
        start_byte += chunk_size

    write_lock = threading.Lock()
    local_path.parent.mkdir(parents=True, exist_ok=True)

    def _fetch_range(start: int, end: int) -> int:
        payload = blob.download_as_bytes(start=start, end=end)
        with write_lock:
            output_file.seek(start)
            output_file.write(payload)
        return len(payload)

    logger.info(f"Downloading {file_name} ({file_size / (1024 * 1024):.2f} MB, {max_workers} threads)")
    try:
        with open(local_path, "wb") as output_file:
            with tqdm(total=file_size, unit="B", unit_scale=True, desc=file_name, leave=False) as progress:
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                    futures = [pool.submit(_fetch_range, start, end) for start, end in byte_ranges]
                    for future in concurrent.futures.as_completed(futures):
                        progress.update(future.result())
    except Exception as exc:
        if local_path.exists():
            local_path.unlink()  # never leave a truncated file behind for a later run to trust
        raise RuntimeError(f"Download failed for {file_name}, partial file removed. Reason: {exc}")

    return local_path


def download_gcs_file(
    uri: str,
    destination: Union[str, Path],
    key_path: Union[str, Path],
    max_workers: int = 4,
    chunk_size: int = 8 * 1024 * 1024,
) -> int:
    """Downloads one specific blob to one specific path (no shard detection, no
    mosaicking). Used for model files. Returns the file size in bytes."""
    bucket_name, blob_path = _parse_uri(uri)
    blob = gcs_client(key_path).bucket(bucket_name).blob(blob_path)
    if not blob.exists():
        raise FileNotFoundError(f"GCS object not found: {uri}")
    destination = Path(destination)
    _download_blob_concurrently(blob, destination, max_workers, chunk_size)
    return destination.stat().st_size


def download_gcs_object(
    uri: str,
    out_dir: Union[str, Path],
    key_path: Union[str, Path],
    max_workers: int = 4,
    chunk_size: int = 8 * 1024 * 1024,
) -> Path:
    """Downloads (and, when GEE sharded the export, mosaics) a GCS GeoTIFF locally.

    `uri` may name a single file or the un-sharded base name of a GEE export that got
    split into chunks -- both are detected automatically and yield one local GeoTIFF.
    Resume-safe: an already-complete local file is reused rather than re-downloaded.
    """
    out_dir_path = Path(out_dir)
    out_dir_path.mkdir(parents=True, exist_ok=True)

    client = gcs_client(key_path)
    bucket_name, blob_prefix = _parse_uri(uri)
    if blob_prefix.endswith(".tif"):
        blob_prefix = blob_prefix[:-4]

    bucket = client.bucket(bucket_name)
    base_name = Path(blob_prefix).name
    shard_pattern = re.compile(rf"^{re.escape(base_name)}{_SHARD_SUFFIX}")
    target_blobs = [b for b in bucket.list_blobs(prefix=blob_prefix) if shard_pattern.match(Path(b.name).name)]

    if not target_blobs:
        raise FileNotFoundError(f"No .tif (or GEE shard) objects found for prefix: gs://{bucket_name}/{blob_prefix}")

    final_local_path = out_dir_path / f"{base_name}.tif"

    if len(target_blobs) == 1:
        if final_local_path.exists() and final_local_path.stat().st_size == target_blobs[0].size:
            logger.info(f"Already downloaded: {final_local_path}")
            return final_local_path
        _download_blob_concurrently(target_blobs[0], final_local_path, max_workers, chunk_size)
        return final_local_path

    if final_local_path.exists():
        logger.info(f"Mosaic already exists locally: {final_local_path}")
        return final_local_path

    logger.info(f"Detected {len(target_blobs)} GEE shards; downloading and mosaicking.")
    with tempfile.TemporaryDirectory() as temp_dir:
        shard_paths = []
        for blob in target_blobs:
            shard_path = Path(temp_dir) / Path(blob.name).name
            _download_blob_concurrently(blob, shard_path, max_workers, chunk_size)
            shard_paths.append(shard_path)

        # Streamed via VRT rather than loaded whole -- a sharded district export can be
        # many GB, which an in-memory merge would not survive.
        raster_io.mosaic_geotiffs(shard_paths, final_local_path)

    return final_local_path
