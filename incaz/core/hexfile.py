"""HEX file handling (Intel HEX) - the 'dataset' side of a project.

A :class:`MemoryImage` wraps an intelhex object and provides byte-level
read/write plus segment enumeration, used for offline calibration and for
flash programming.
"""

from __future__ import annotations

import logging
from pathlib import Path

from intelhex import IntelHex

log = logging.getLogger(__name__)


class MemoryImage:
    """In-memory ECU image loaded from / saved to an Intel HEX file."""

    def __init__(self, path: str | Path | None = None):
        self.ihex = IntelHex()
        self.path: Path | None = None
        self.dirty = False
        if path is not None:
            self.load(path)

    def load(self, path: str | Path) -> None:
        path = Path(path)
        suffix = path.suffix.lower()
        if suffix in (".hex", ".ihx", ".ihex", ".h86", ".a43"):
            self.ihex = IntelHex(str(path))
        elif suffix in (".bin",):
            self.ihex = IntelHex()
            self.ihex.loadbin(str(path))
        else:
            # try Intel HEX as a default
            self.ihex = IntelHex(str(path))
        self.path = path
        self.dirty = False
        log.info("Loaded %s: %d segments, %d bytes",
                 path.name, len(self.segments()), len(self.ihex))

    def save(self, path: str | Path | None = None) -> Path:
        path = Path(path) if path is not None else self.path
        if path is None:
            raise ValueError("No path given for HEX save")
        self.ihex.write_hex_file(str(path))
        self.path = path
        self.dirty = False
        return path

    # ------------------------------------------------------------------ access
    # NOTE: never use `addr in self.ihex` - IntelHex has no __contains__, and
    # its __getitem__ answers any address, so `in` would loop forever.
    def segments(self) -> list[tuple[int, int]]:
        """List of (start_address, size) tuples."""
        return [(start, stop - start) for start, stop in self.ihex.segments()]

    def contains(self, address: int, size: int = 1) -> bool:
        end = address + size
        return any(start <= address and end <= stop
                   for start, stop in self.ihex.segments())

    def read(self, address: int, size: int) -> bytes:
        return bytes(self.ihex.gets(address, size))

    def read_padded(self, address: int, size: int, pad: int = 0xFF) -> bytes:
        old_padding = self.ihex.padding
        self.ihex.padding = pad
        try:
            return bytes(self.ihex.tobinstr(start=address, size=size))
        finally:
            self.ihex.padding = old_padding

    def write(self, address: int, data: bytes) -> None:
        self.ihex.puts(address, bytes(data))
        self.dirty = True

    def __len__(self) -> int:
        return len(self.ihex)
