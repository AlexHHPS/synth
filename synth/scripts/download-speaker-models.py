"""Download only the publicly licensed Community-1 conversions at a pinned revision."""
import hashlib
import json
from pathlib import Path
import ssl
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
REVISION = "df2625ac79a7ac6b65ad868fee6d80f320da4232"
REPO = "FluidInference/speaker-diarization-coreml"
DESTINATION = ROOT / "synth/.runtime/models/speakers-community1"
ALLOWED = {"Segmentation.mlmodelc", "FBank.mlmodelc", "Embedding.mlmodelc", "PldaRho.mlmodelc",
           "plda-parameters.json", "xvector-transform.json", "NOTICE.md", "README.md", "LICENSE", "LICENSE.md", "provenance.json"}


def main():
    context = ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    metadata = json.load(urllib.request.urlopen(f"https://huggingface.co/api/models/{REPO}/revision/{REVISION}?blobs=true", context=context, timeout=30))
    assert metadata["sha"] == REVISION and not metadata.get("gated")
    manifest = {"repo": REPO, "revision": REVISION, "license": "scoped-cc-by-4.0", "files": []}
    files = [entry for entry in metadata["siblings"] if entry["rfilename"].split("/")[0] in ALLOWED]
    print(f"Downloading {len(files)} pinned model/license files", flush=True)
    for entry in files:
        relative = entry["rfilename"]
        path = DESTINATION / relative
        assert path.resolve().is_relative_to(DESTINATION.resolve())
        path.parent.mkdir(parents=True, exist_ok=True)
        expected = entry.get("lfs", {}).get("sha256")
        if not path.exists():
            temporary = path.with_name(path.name + ".part")
            with urllib.request.urlopen(f"https://huggingface.co/{REPO}/resolve/{REVISION}/{relative}", context=context, timeout=90) as response, temporary.open("wb") as out:
                while block := response.read(1024 * 1024):
                    out.write(block)
            temporary.rename(path)
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if expected and actual != expected:
            raise ValueError("model_hash_mismatch:" + relative)
        manifest["files"].append({"path": relative, "sha256": actual, "size": path.stat().st_size})
        print("Verified " + relative, flush=True)
    (DESTINATION / "synth-model-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("SPEAKER_MODELS_PINNED_OK", flush=True)


if __name__ == "__main__":
    main()
