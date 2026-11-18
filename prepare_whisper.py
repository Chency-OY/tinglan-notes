"""Fetch verified Windows x64 whisper.cpp assets for source runs and packaging."""
import hashlib
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEST = ROOT / "whisper"


def fetch(url, name, digest, directory):
    path = directory / name
    urllib.request.urlretrieve(url, path)
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise ValueError(f"SHA-256 mismatch: {name}")
    return path


def main():
    DEST.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        archive = fetch("https://github.com/ggml-org/whisper.cpp/releases/download/v1.8.3/whisper-bin-x64.zip",
                        "runtime.zip", "d824b1e37599f882b396e73f1ee0bfd5d0529f700314c48311dcbd00b803321d", tmp)
        model = fetch("https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base-q5_1.bin",
                      "ggml-base-q5_1.bin", "422f1ae452ade6f30a004d7e5c6a43195e4433bc370bf23fac9cc591f01a8898", tmp)
        with zipfile.ZipFile(archive) as z:
            for name in ("whisper-cli.exe", "whisper.dll", "ggml.dll", "ggml-base.dll", "ggml-cpu.dll"):
                with z.open("Release/" + name) as src, (DEST / name).open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        shutil.copyfile(model, DEST / model.name)
    for notice in (ROOT / "third_party").glob("LICENSE-*.txt"):
        shutil.copyfile(notice, DEST / notice.name)


if __name__ == "__main__":
    main()
