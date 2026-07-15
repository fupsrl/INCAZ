"""INCA-style calibration database.

A database is a *folder* (``<name>.incazdb``) containing an SQLite index and
the imported files (A2L / HEX copies), mirroring INCA's database concept:

    Database
      +-- Project        (imported A2L)
      |     +-- Dataset  (imported HEX, working data)
      |     +-- Experiment (window layout + variable lists)
      |     +-- Hardware configuration (XCP on Ethernet parameters)
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

DB_SUFFIX = ".incazdb"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    a2l_file TEXT NOT NULL,
    comment TEXT DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS datasets (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    hex_file TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(project_id, name)
);
CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    layout TEXT DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(project_id, name)
);
CREATE TABLE IF NOT EXISTS hardware (
    project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    config TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS prof_configs (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    config TEXT DEFAULT '{}',
    UNIQUE(project_id, name)
);
"""


@dataclass
class ProjectInfo:
    id: int
    name: str
    a2l_file: Path
    comment: str = ""
    datasets: list = field(default_factory=list)      # [(id, name, Path)]
    experiments: list = field(default_factory=list)   # [(id, name)]


class CalDatabase:
    """Folder-based measurement & calibration database."""

    def __init__(self, path: str | Path):
        self.root = Path(path)
        if self.root.suffix != DB_SUFFIX:
            self.root = self.root.with_suffix(DB_SUFFIX)
        self.files_dir = self.root / "files"
        self._con: sqlite3.Connection | None = None

    # ------------------------------------------------------------ lifecycle
    @property
    def name(self) -> str:
        return self.root.stem

    @property
    def is_open(self) -> bool:
        return self._con is not None

    def create(self) -> "CalDatabase":
        self.root.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(exist_ok=True)
        self._connect()
        return self

    def open(self) -> "CalDatabase":
        if not (self.root / "db.sqlite").exists():
            raise FileNotFoundError(f"Not an INCAZ database: {self.root}")
        self.files_dir.mkdir(exist_ok=True)
        self._connect()
        return self

    def _connect(self) -> None:
        self._con = sqlite3.connect(self.root / "db.sqlite")
        self._con.execute("PRAGMA foreign_keys = ON")
        self._con.executescript(_SCHEMA)
        self._con.commit()

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None

    # ------------------------------------------------------------ projects
    def add_project(self, name: str, a2l_path: str | Path, comment: str = "") -> int:
        """Import an A2L file as a new project (the file is copied)."""
        a2l_path = Path(a2l_path)
        dest = self.files_dir / f"{name}{a2l_path.suffix or '.a2l'}"
        shutil.copyfile(a2l_path, dest)
        cur = self._con.execute(
            "INSERT INTO projects(name, a2l_file, comment, created_at) VALUES (?,?,?,?)",
            (name, dest.name, comment, time.time()),
        )
        self._con.commit()
        return cur.lastrowid

    def remove_project(self, project_id: int) -> None:
        row = self._con.execute("SELECT a2l_file FROM projects WHERE id=?", (project_id,)).fetchone()
        for (hex_file,) in self._con.execute(
            "SELECT hex_file FROM datasets WHERE project_id=?", (project_id,)
        ):
            (self.files_dir / hex_file).unlink(missing_ok=True)
        self._con.execute("DELETE FROM projects WHERE id=?", (project_id,))
        self._con.commit()
        if row:
            (self.files_dir / row[0]).unlink(missing_ok=True)

    def projects(self) -> list[ProjectInfo]:
        out = []
        for pid, name, a2l_file, comment in self._con.execute(
            "SELECT id, name, a2l_file, comment FROM projects ORDER BY name"
        ):
            info = ProjectInfo(pid, name, self.files_dir / a2l_file, comment)
            info.datasets = [
                (did, dname, self.files_dir / hexf)
                for did, dname, hexf in self._con.execute(
                    "SELECT id, name, hex_file FROM datasets WHERE project_id=? ORDER BY name", (pid,)
                )
            ]
            info.experiments = [
                (eid, ename)
                for eid, ename in self._con.execute(
                    "SELECT id, name FROM experiments WHERE project_id=? ORDER BY name", (pid,)
                )
            ]
            out.append(info)
        return out

    def project(self, project_id: int) -> ProjectInfo | None:
        for p in self.projects():
            if p.id == project_id:
                return p
        return None

    # ------------------------------------------------------------ datasets
    def add_dataset(self, project_id: int, name: str, hex_path: str | Path) -> int:
        hex_path = Path(hex_path)
        dest = self.files_dir / f"ds_{project_id}_{name}{hex_path.suffix or '.hex'}"
        shutil.copyfile(hex_path, dest)
        cur = self._con.execute(
            "INSERT INTO datasets(project_id, name, hex_file, created_at) VALUES (?,?,?,?)",
            (project_id, name, dest.name, time.time()),
        )
        self._con.commit()
        return cur.lastrowid

    def remove_dataset(self, dataset_id: int) -> None:
        row = self._con.execute("SELECT hex_file FROM datasets WHERE id=?", (dataset_id,)).fetchone()
        self._con.execute("DELETE FROM datasets WHERE id=?", (dataset_id,))
        self._con.commit()
        if row:
            (self.files_dir / row[0]).unlink(missing_ok=True)

    def dataset_path(self, dataset_id: int) -> Path | None:
        row = self._con.execute("SELECT hex_file FROM datasets WHERE id=?", (dataset_id,)).fetchone()
        return self.files_dir / row[0] if row else None

    # ------------------------------------------------------------ experiments
    def save_experiment(self, project_id: int, name: str, layout: dict) -> int:
        now = time.time()
        cur = self._con.execute(
            """INSERT INTO experiments(project_id, name, layout, created_at, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(project_id, name)
               DO UPDATE SET layout=excluded.layout, updated_at=excluded.updated_at""",
            (project_id, name, json.dumps(layout), now, now),
        )
        self._con.commit()
        return cur.lastrowid

    def load_experiment(self, project_id: int, name: str) -> dict:
        row = self._con.execute(
            "SELECT layout FROM experiments WHERE project_id=? AND name=?", (project_id, name)
        ).fetchone()
        return json.loads(row[0]) if row else {}

    def remove_experiment(self, project_id: int, name: str) -> None:
        self._con.execute("DELETE FROM experiments WHERE project_id=? AND name=?", (project_id, name))
        self._con.commit()

    # ------------------------------------------------------------ hardware config
    def save_hardware(self, project_id: int, config: dict) -> None:
        self._con.execute(
            """INSERT INTO hardware(project_id, config) VALUES (?,?)
               ON CONFLICT(project_id) DO UPDATE SET config=excluded.config""",
            (project_id, json.dumps(config)),
        )
        self._con.commit()

    def load_hardware(self, project_id: int) -> dict:
        row = self._con.execute("SELECT config FROM hardware WHERE project_id=?", (project_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    # ------------------------------------------------------------ ProF configs
    def save_prof(self, project_id: int, name: str, config: dict) -> None:
        self._con.execute(
            """INSERT INTO prof_configs(project_id, name, config) VALUES (?,?,?)
               ON CONFLICT(project_id, name) DO UPDATE SET config=excluded.config""",
            (project_id, name, json.dumps(config)),
        )
        self._con.commit()

    def load_profs(self, project_id: int) -> dict[str, dict]:
        return {
            name: json.loads(cfg)
            for name, cfg in self._con.execute(
                "SELECT name, config FROM prof_configs WHERE project_id=?", (project_id,)
            )
        }
