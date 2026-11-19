"""Prepare and run the optional local model for screenshot and frame descriptions."""
import fcntl
import json
import os
from pathlib import Path
import subprocess

from .subprocesses import run_process

MODEL_REPO = "mlx-community/Qwen3-VL-4B-Instruct-4bit"
MODEL_REVISION = "2fd8dacbdb8f1e54b8c005f081ec5bf79c56376b"
RUNTIME_PACKAGE = "mlx-vlm==0.8.0"
MODEL_ID = "qwen3-vl-4b-instruct-4bit"
MODEL_WEIGHT_BYTES = 3_093_767_283
RUNNER = Path(__file__).with_name("vision_runner.py")


def paths(appdata):
    return appdata / "runtime/vision-qwen3-vl-v1", appdata / "models" / MODEL_ID


def ready(appdata):
    runtime, model = paths(appdata)
    try:
        return (
            RUNNER.is_file()
            and (runtime / "bin/python").is_file()
            and (runtime / "lib/python3.12/site-packages/mlx_vlm-0.8.0.dist-info/METADATA").is_file()
            and (model / "model.safetensors").stat().st_size == MODEL_WEIGHT_BYTES
            and all((model / name).is_file() for name in (
                "config.json", "preprocessor_config.json", "tokenizer.json", "tokenizer_config.json",
            ))
        )
    except OSError:
        return False


def setup(appdata):
    with (appdata / ".vision-install.lock").open("a") as gate:
        fcntl.flock(gate, fcntl.LOCK_EX)
        if not ready(appdata):
            install(appdata)
        if not ready(appdata):
            raise ValueError("vision_setup_incomplete")


def install(appdata):
    runtime, model = paths(appdata)
    python = runtime / "bin/python"
    env = {**os.environ, "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1", "HF_HUB_DISABLE_TELEMETRY": "1"}
    if not python.is_file():
        run_process(["uv", "venv", "--python", "3.12", str(runtime)], check=True, timeout=360)
    run_process(["uv", "pip", "install", "--python", str(python), RUNTIME_PACKAGE],
                check=True, timeout=600)
    download = (
        "from huggingface_hub import snapshot_download; import sys; "
        "snapshot_download(sys.argv[1],revision=sys.argv[2],local_dir=sys.argv[3],token=False,"
        "allow_patterns=['*.json','*.jinja','*.txt','*.safetensors'])"
    )
    run_process([str(python), "-I", "-B", "-c", download, MODEL_REPO, MODEL_REVISION, str(model)],
                env=env, check=True, timeout=1800)


def annotate(appdata, frames, lock_fd=None):
    runtime, model = paths(appdata)
    python = runtime / "bin/python"
    if not frames:
        return []
    if not ready(appdata):
        return [None] * len(frames)
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
           "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1", "HF_HUB_DISABLE_TELEMETRY": "1"}
    try:
        result = run_process([str(python), "-I", "-B", str(RUNNER), str(model)],
                             input=json.dumps([[str(p) for p in group] for group in frames]).encode(),
                             capture_output=True, env=env, timeout=min(550, 35 + len(frames) * 28),
                             pass_fds=() if lock_fd is None else (lock_fd,))
        if result.returncode:
            return [None] * len(frames)
        output = json.loads(result.stdout)
        if isinstance(output, list) and len(output) == len(frames):
            return output
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return [None] * len(frames)
