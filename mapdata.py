"""Load baseline maps, using an interpreter-local cache made during image build."""

import gzip
import json
import marshal
from pathlib import Path


def load(path):
    path = Path(path)
    compiled = path.with_suffix(".marshal")
    if compiled.is_file():
        with compiled.open("rb") as stream:
            return marshal.load(stream)
    with gzip.open(path, "rb") as stream:
        return json.load(stream)


if __name__ == "__main__":
    for path in Path("maps").glob("*.json.gz"):
        with gzip.open(path, "rb") as stream:
            data = json.load(stream)
        with path.with_suffix(".marshal").open("wb") as stream:
            marshal.dump(data, stream)
