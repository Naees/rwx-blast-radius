"""Read the per-test line and file dependencies recorded at the pinned revision."""

from functools import lru_cache
from pathlib import Path

from mapdata import load as load_map


class Dependencies:
    def __init__(self, path=Path(__file__).parent / "maps/deps.json.gz"):
        data = load_map(path)
        self.directory = Path(path).parent
        self.names = data["tests"]
        self.tests = frozenset(self.names)
        self.incomplete = frozenset(data["incomplete"])
        self.files = data["files"]
        self.reads = data["reads"]
        self.source_reads = data.get("source_reads", {})
        self.pool = [int(mask, 16) for mask in data["pool"]]

    @lru_cache(maxsize=8192)
    def _expand(self, mask):
        names = []
        while mask:
            bit = mask & -mask
            names.append(self.names[bit.bit_length() - 1])
            mask ^= bit
        return frozenset(names)

    def has_line(self, path, number):
        return str(number) in self.files.get(path, {})

    def line(self, path, number):
        index = self.files.get(path, {}).get(str(number))
        return self._expand(0 if index is None else self.pool[index])

    @lru_cache(maxsize=None)
    def file(self, path):
        mask = 0
        for index in set(self.files.get(path, {}).values()):
            mask |= self.pool[index]
        return self._expand(mask)

    def fixture(self, path):
        index = self.reads.get(path)
        return self._expand(0 if index is None else self.pool[index])

    def source_readers(self, path):
        """Tests that read Python source as data, excluding import/loader traffic."""
        index = self.source_reads.get(path)
        return self._expand(0 if index is None else self.pool[index])

    @lru_cache(maxsize=1)
    def _initializers(self):
        return load_map(self.directory / "initializers.json.gz")

    def import_initializers(self, path, lines):
        """Module initializers that reached these lines inside a function body."""
        changed = set(lines)
        return {owner for owner, numbers in self._initializers().get(path, {}).items()
                if not changed.isdisjoint(numbers)}
