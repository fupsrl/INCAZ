"""Hardware (XCP on Ethernet) configuration - INCA's 'workspace hardware'."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class HardwareConfig:
    protocol: str = "UDP"          # UDP | TCP
    host: str = "127.0.0.1"
    port: int = 5555
    ipv6: bool = False
    seed_n_key_dll: str = ""
    daq_mode: str = "POLLING"      # POLLING | DAQ
    poll_rate_hz: float = 10.0
    default_event: int = 0         # DAQ event channel used when a raster has no event
    enable_timestamps: bool = False
    connect_timeout: float = 2.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "HardwareConfig":
        cfg = cls()
        for k, v in (d or {}).items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        return cfg

    def pyxcp_config(self) -> dict:
        eth = {
            "host": self.host,
            "port": int(self.port),
            "protocol": self.protocol,
            "ipv6": bool(self.ipv6),
        }
        general = {}
        if self.seed_n_key_dll:
            general["seed_n_key_dll"] = self.seed_n_key_dll
        cfg = {"Transport": {"Eth": eth}}
        if general:
            cfg["General"] = general
        return cfg
