from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .io import atomic_write_json, load_config, now_iso, resolve_inside, sha256_file


def analyze_audio(root: Path, source: Path, output: Path) -> dict[str, Any]:
    source = source.expanduser().resolve(strict=True)
    output = resolve_inside(output, root)
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required for analyze-audio")
    timeout = int(
        (load_config(root).get("external_tools") or {}).get(
            "ffprobe_timeout_seconds", 30
        )
    )
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration,format_name,size,bit_rate:stream=index,codec_name,codec_type,sample_rate,channels,bits_per_sample",
        "-of",
        "json",
        str(source),
    ]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "ffprobe failed")
    try:
        probe = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ffprobe returned malformed JSON") from exc
    report = {
        "schema_version": "0.1",
        "analyzed_at": now_iso(),
        "source": str(source),
        "source_sha256": sha256_file(source),
        "tool": "ffprobe",
        "measurement_only": True,
        "quality_conclusion": None,
        "probe": probe,
    }
    atomic_write_json(output, report)
    return report
