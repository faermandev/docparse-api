from pathlib import Path


class LocalFileStorage:
    """Stores uploaded files on the local filesystem under a root directory."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def save(self, key: str, data: bytes) -> None:
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def read(self, key: str) -> bytes:
        """Return the stored bytes. Raises FileNotFoundError if the key does not exist."""
        return self._path_for(key).read_bytes()

    def delete(self, key: str) -> None:
        self._path_for(key).unlink(missing_ok=True)

    def _path_for(self, key: str) -> Path:
        path = (self._root / key).resolve()
        if not path.is_relative_to(self._root):
            raise ValueError(f"Storage key escapes the storage root: {key!r}")
        return path
