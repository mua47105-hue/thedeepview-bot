"""
Persistent state sync via Hugging Face Hub.

HF free Spaces have ephemeral storage — /data is wiped on every Space restart.
This module uploads state.db + seen.json to a private HF dataset repo at the
end of each pipeline run, and downloads them back on startup. This way the
seen-URL set + run history + Gemini quota counter survive Space restarts.

GRACEFUL DEGRADATION:
- If HF_TOKEN or HF_STATE_REPO is not set, all functions are no-ops (logged).
- If HF repo doesn't exist on first upload, we try to create it (private).
- If download fails (file not yet pushed), we start fresh with empty state.
- If upload fails, we log and continue — state will retry on next run.

CONCURRENCY:
- Upload happens AFTER all DB writes in pipeline.run_pipeline(), so the
  snapshot is consistent.
- We upload a COPY of the state files (not the live ones), to avoid
  reading a file mid-write.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from config import cfg
from utils import logger


def _enabled() -> bool:
    """Return True only if both HF_TOKEN and HF_STATE_REPO are configured."""
    if not cfg.hf_token or not cfg.hf_state_repo:
        return False
    return True


def _get_api():
    """Lazy-import HfApi and configure with our token. Returns None if disabled."""
    if not _enabled():
        return None
    try:
        from huggingface_hub import HfApi
        return HfApi(token=cfg.hf_token)
    except ImportError:
        logger.warning(
            "huggingface_hub not installed; persistent state sync disabled. "
            "Run: pip install huggingface_hub"
        )
        return None
    except Exception as e:
        logger.warning(f"Failed to init HfApi: {e}")
        return None


def _ensure_repo_exists(api) -> bool:
    """Create the HF dataset repo if it doesn't exist. Returns True on success."""
    try:
        from huggingface_hub import HfApi
        # create_repo is idempotent — if repo exists, it's a no-op
        api.create_repo(
            repo_id=cfg.hf_state_repo,
            repo_type="dataset",
            private=True,
            exist_ok=True,
        )
        return True
    except Exception as e:
        logger.warning(f"Failed to create/access HF repo {cfg.hf_state_repo}: {e}")
        return False


def download_state() -> None:
    """
    Pull state.db and seen.json from HF dataset repo to local data_dir.
    Called at startup BEFORE the first pipeline run.

    If HF sync is disabled or files don't exist yet, this is a no-op
    (we just start with empty local state).
    """
    if not _enabled():
        logger.info("HF state sync disabled (HF_TOKEN/HF_STATE_REPO not set); using ephemeral storage")
        return

    api = _get_api()
    if api is None:
        return

    if not _ensure_repo_exists(api):
        return

    from huggingface_hub import hf_hub_download

    for filename in ("state.db", "seen.json"):
        try:
            local_path = hf_hub_download(
                repo_id=cfg.hf_state_repo,
                repo_type="dataset",
                filename=filename,
                token=cfg.hf_token,
            )
            dest = cfg.data_dir / filename
            shutil.copy2(local_path, dest)
            logger.info(f"[state-sync] restored {filename} from HF ({dest.stat().st_size} bytes)")
        except Exception as e:
            # File probably doesn't exist yet on first run — that's fine
            err_str = str(e).lower()
            if "entry not found" in err_str or "404" in err_str or "does not exist" in err_str:
                logger.info(f"[state-sync] {filename} not yet on HF (first run); starting fresh")
            else:
                logger.warning(f"[state-sync] failed to download {filename}: {e}")


def upload_state() -> bool:
    """
    Push state.db and seen.json from local data_dir to HF dataset repo.
    Called at the END of each pipeline run (after all DB writes).

    Returns True on success, False on failure (or if disabled).
    """
    if not _enabled():
        return False

    api = _get_api()
    if api is None:
        return False

    if not _ensure_repo_exists(api):
        return False

    success = True
    for filename in ("state.db", "seen.json"):
        local_path = cfg.data_dir / filename
        if not local_path.exists():
            logger.info(f"[state-sync] {filename} does not exist locally yet; skipping upload")
            continue

        # Copy to a temp file before uploading, so we don't read a file mid-write
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f"{filename}.", suffix=".tmp", delete=False
            ) as tmp:
                tmp_path = Path(tmp.name)
            shutil.copy2(local_path, tmp_path)

            api.upload_file(
                path_or_fileobj=str(tmp_path),
                path_in_repo=filename,
                repo_id=cfg.hf_state_repo,
                repo_type="dataset",
                token=cfg.hf_token,
            )
            logger.info(
                f"[state-sync] uploaded {filename} to HF "
                f"({tmp_path.stat().st_size} bytes)"
            )
        except Exception as e:
            logger.warning(f"[state-sync] failed to upload {filename}: {e}")
            success = False
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    return success
