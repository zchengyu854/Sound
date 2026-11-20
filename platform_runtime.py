"""Process startup and data-directory runtime helpers."""
from __future__ import annotations

import os
import sys
import sysconfig
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


DEPTH_MODES = ("cached", "compiled")
STREAMING_FRAME_RANGE = (1, 16)
_cuda_dll_directories = []


def prepare_windows_cuda():
    if os.name != "nt" or _cuda_dll_directories:
        return
    nvidia = Path(sysconfig.get_path("platlib")) / "nvidia"
    directories = list(nvidia.glob("*/bin"))
    cuda_path = os.getenv("CUDA_PATH")
    if cuda_path:
        directories.append(Path(cuda_path) / "bin")
    for directory in directories:
        if directory.is_dir():
            _cuda_dll_directories.append(os.add_dll_directory(str(directory)))


def lock_data_directory(lease):
    if os.name == "nt":
        lease.seek(0)
        msvcrt.locking(lease.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)


def streaming_frames(value=None):
    """Chunk size in codec frames for streaming synthesis (BREEZE_STREAMING_FRAMES).

    Larger chunks mean fewer callbacks but later first audio; smaller chunks start
    sooner. Invalid or out-of-range values fall back to the library default.
    """
    from breezetts2_mac_fast import STREAMING_FRAMES
    raw = os.getenv("BREEZE_STREAMING_FRAMES") if value is None else value
    if raw is None or str(raw).strip() == "":
        return STREAMING_FRAMES
    try:
        frames = int(str(raw).strip())
    except (TypeError, ValueError):
        return STREAMING_FRAMES
    low, high = STREAMING_FRAME_RANGE
    return frames if low <= frames <= high else STREAMING_FRAMES


def depth_mode(value=None):
    """Depth-decoder mode for this process; anything unknown falls back to cached.

    `cached` reuses the intra-frame KV cache. `compiled` also compiles the whole
    frame graph: the first call pays a compilation cost, so warm numbers must be
    measured separately. Read from BREEZE_DEPTH_MODE when `value` is None.
    """
    chosen = os.getenv("BREEZE_DEPTH_MODE", "cached") if value is None else value
    chosen = str(chosen).strip().lower()
    return chosen if chosen in DEPTH_MODES else "cached"
