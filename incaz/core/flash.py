"""ECU flash programming via XCP (PGM resource) - INCA's 'ProF' equivalent.

A :class:`ProfConfig` describes *how* to flash (which memory ranges to clear
and program, whether to verify, ...). The :class:`FlashController` executes
the XCP programming sequence:

    PROGRAM_START -> [PROGRAM_CLEAR -> PROGRAM/PROGRAM_NEXT]* -> PROGRAM_RESET
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from .hexfile import MemoryImage
from .xcp_client import XcpClient

log = logging.getLogger(__name__)


@dataclass
class ProfConfig:
    """Flash procedure configuration (ProF)."""

    name: str = "default"
    description: str = ""
    #: [] -> program every segment found in the HEX file;
    #: otherwise list of [start, size] ranges to restrict programming to.
    ranges: list = field(default_factory=list)
    clear_before_program: bool = True
    verify_checksum: bool = True
    reset_after: bool = True
    unlock_pgm: bool = False       # run seed & key before programming
    max_segment_gap: int = 256     # merge HEX segments closer than this

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ProfConfig":
        cfg = cls()
        for k, v in (d or {}).items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        return cfg


class FlashAbort(Exception):
    pass


class FlashController:
    """Runs a ProF flash job. Callbacks are invoked from the XCP worker thread."""

    def __init__(self, client: XcpClient, image: MemoryImage, prof: ProfConfig,
                 progress: Optional[Callable[[int, str], None]] = None,
                 logger: Optional[Callable[[str], None]] = None):
        self.client = client
        self.image = image
        self.prof = prof
        self._progress = progress or (lambda pct, msg: None)
        self._log = logger or (lambda msg: log.info(msg))
        self._abort = False

    def abort(self) -> None:
        self._abort = True

    # ------------------------------------------------------------------ helpers
    def _segments(self) -> list[tuple[int, bytes]]:
        """Programming segments (merged HEX segments, optionally restricted)."""
        segs = self.image.segments()
        merged: list[list[int]] = []
        for start, size in sorted(segs):
            if merged and start - (merged[-1][0] + merged[-1][1]) <= self.prof.max_segment_gap:
                merged[-1][1] = start + size - merged[-1][0]
            else:
                merged.append([start, size])
        if self.prof.ranges:
            restricted = []
            for r_start, r_size in self.prof.ranges:
                for start, size in merged:
                    lo = max(start, r_start)
                    hi = min(start + size, r_start + r_size)
                    if hi > lo:
                        restricted.append([lo, hi - lo])
            merged = restricted
        return [(start, self.image.read_padded(start, size)) for start, size in merged]

    def _check_abort(self) -> None:
        if self._abort:
            raise FlashAbort("Flash aborted by user")

    # ------------------------------------------------------------------ main
    def run(self) -> None:
        """Execute the flash job (blocking; run from a helper thread)."""
        segments = self._segments()
        if not segments:
            raise RuntimeError("Nothing to program: HEX image is empty "
                               "or ProF ranges exclude all segments")
        total = sum(len(d) for _, d in segments)
        self._log(f"ProF '{self.prof.name}': {len(segments)} segment(s), {total} bytes")

        client = self.client

        if self.prof.unlock_pgm:
            self._log("Unlocking PGM resource (seed & key)...")
            client.unlock()

        self._log("PROGRAM_START")
        client.call_sync(lambda: client._master.programStart())

        done = 0
        try:
            for start, data in segments:
                self._check_abort()
                if self.prof.clear_before_program:
                    self._log(f"PROGRAM_CLEAR 0x{start:08X} ({len(data)} bytes)")

                    def _clear(start=start, size=len(data)):
                        client._master.setMta(start, 0)
                        client._master.programClear(0x00, size)
                    client.call_sync(_clear, timeout=120.0)

                self._log(f"PROGRAM 0x{start:08X} ({len(data)} bytes)")

                def _program(start=start, data=data):
                    def cb(pct, base=done, size=len(data)):
                        overall = int((base + size * pct / 100.0) * 100.0 / total)
                        self._progress(overall, f"Programming 0x{start:08X}")
                        self._check_abort()
                    client._master.flash_program(start, data, callback=cb)
                client.call_sync(_program, timeout=600.0)
                done += len(data)
                self._progress(int(done * 100.0 / total), "")

            if self.prof.verify_checksum:
                self._verify(segments)
        except FlashAbort:
            self._log("Aborted - sending PROGRAM_RESET")
            try:
                client.call_sync(lambda: client._master.programReset(
                    wait_for_optional_response=False))
            except Exception:
                pass
            raise

        if self.prof.reset_after:
            self._log("PROGRAM_RESET (ECU restart)")
            try:
                client.call_sync(lambda: client._master.programReset(
                    wait_for_optional_response=False))
            except Exception as exc:
                self._log(f"PROGRAM_RESET response missing (usually fine): {exc}")
            client.disconnect()
        self._progress(100, "Done")
        self._log("Flash finished successfully")

    def _verify(self, segments: list[tuple[int, bytes]]) -> None:
        """Best-effort BUILD_CHECKSUM verification."""
        for start, data in segments:
            self._check_abort()
            try:
                result = self.client.build_checksum(start, len(data))
                self._log(f"Checksum 0x{start:08X}: slave reports "
                          f"{result.checksumType} = 0x{result.checksum:08X}")
            except Exception as exc:
                self._log(f"Checksum verification skipped for 0x{start:08X}: {exc}")
                return
