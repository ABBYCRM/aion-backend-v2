"""
Scenario store — loads policy CSVs for AION scenario.match.

Authoritative data lives under data/scenarios/ (or $AION_DATA_DIR/scenarios).
No LLM. No invention. Rows are fixed policy.
"""
from __future__ import annotations

import csv
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger("aion.skills.scenario_store")

# Pack name → filename
PACK_FILES: Dict[str, str] = {
    "github": "github_scenarios.csv",
    "openclaw": "openclaw_scenarios.csv",
    "composio": "composio_scenarios.csv",
    "firecrawl_steel": "firecrawl_steel_scenarios.csv",
    "render": "render_scenarios.csv",
}

# Columns present across packs (optional ones may be empty)
KNOWN_COLUMNS = (
    "id",
    "category",
    "skill",
    "service",
    "trigger",
    "condition",
    "if_action",
    "else_action",
    "severity",
    "source_doc",
)


@dataclass(frozen=True)
class ScenarioRow:
    id: str
    pack: str
    category: str
    trigger: str
    condition: str
    if_action: str
    else_action: str
    severity: str
    source_doc: str
    skill: str = ""
    service: str = ""
    # Pre-tokenized for fast overlap (lowercase tokens)
    trigger_tokens: frozenset = field(default_factory=frozenset, compare=False)
    condition_tokens: frozenset = field(default_factory=frozenset, compare=False)

    def to_dict(self, score: float | None = None) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "id": self.id,
            "pack": self.pack,
            "category": self.category,
            "skill": self.skill or None,
            "service": self.service or None,
            "trigger": self.trigger,
            "condition": self.condition,
            "if_action": self.if_action,
            "else_action": self.else_action,
            "severity": self.severity,
            "source_doc": self.source_doc,
        }
        if score is not None:
            d["score"] = round(float(score), 4)
        return d


_STOPWORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "to", "of", "in", "on", "for", "is", "are",
        "was", "were", "be", "been", "being", "with", "as", "by", "at", "from",
        "that", "this", "it", "its", "into", "via", "per", "not", "no", "yes",
        "if", "then", "else", "when", "while", "agent", "operator", "user",
    }
)


def tokenize(text: str) -> frozenset:
    if not text:
        return frozenset()
    raw = text.lower()
    # keep alnum and simple separators; split on non-alnum
    buf: List[str] = []
    tok: List[str] = []
    for ch in raw:
        if ch.isalnum() or ch in ("_", "-", "/"):
            tok.append(ch)
        else:
            if tok:
                buf.append("".join(tok))
                tok = []
    if tok:
        buf.append("".join(tok))
    out = {t for t in buf if len(t) > 1 and t not in _STOPWORDS}
    return frozenset(out)


def _default_data_dir() -> Path:
    """Resolve the scenario CSVs directory.

    Order:
      1. \/scenarios  (preferred in production)
      2. \            (legacy)
      3. package-relative data/scenarios (dev / tests)
      4. cwd / data / scenarios
      5. /app/data/scenarios
      6. cwd / data / scenarios  (default)

    We fall back from 1→3 if the env-set dir has no scenario CSVs.
    This keeps dev/test paths working when AION_DATA_DIR is set
    (e.g. tests set AION_DATA_DIR=/tmp/aion-test-data but the CSVs
    live in the repo at data/scenarios/).
    """
    env = os.environ.get("AION_DATA_DIR") or os.environ.get("AION_SCENARIO_DIR")
    env_candidates: list[Path] = []
    if env:
        p = Path(env)
        env_candidates.append(p / "scenarios")
        env_candidates.append(p)
    # Package-relative and standard locations
    here = Path(__file__).resolve()
    std_candidates = [
        here.parents[3] / "data" / "scenarios",  # repo root
        here.parents[2] / "data" / "scenarios",
        Path.cwd() / "data" / "scenarios",
        Path("/app/data/scenarios"),
    ]
    # First, prefer an env-set dir that actually has the CSVs
    for c in env_candidates:
        if c.is_dir() and any(c.glob("*_scenarios.csv")):
            return c
    # Otherwise, fall back to a standard location that has the CSVs
    for c in std_candidates:
        if c.is_dir() and any(c.glob("*_scenarios.csv")):
            return c
    # If nothing has CSVs yet, return the first env candidate (or cwd default)
    if env_candidates:
        return env_candidates[0]
    for c in std_candidates:
        if c.is_dir():
            return c
    return Path.cwd() / "data" / "scenarios"


class ScenarioStore:
    """Thread-safe in-memory store of all scenario packs."""

    def __init__(self, data_dir: Optional[Path | str] = None) -> None:
        self.data_dir = Path(data_dir) if data_dir else _default_data_dir()
        self._lock = threading.RLock()
        self._rows: List[ScenarioRow] = []
        self._by_pack: Dict[str, List[ScenarioRow]] = {}
        self._loaded = False
        self._load_errors: List[str] = []

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def row_count(self) -> int:
        return len(self._rows)

    @property
    def packs(self) -> List[str]:
        return sorted(self._by_pack.keys())

    @property
    def load_errors(self) -> List[str]:
        return list(self._load_errors)

    def ensure_loaded(self) -> None:
        with self._lock:
            if not self._loaded:
                self._load_all()

    def reload(self) -> Dict[str, Any]:
        with self._lock:
            self._rows = []
            self._by_pack = {}
            self._loaded = False
            self._load_errors = []
            self._load_all()
            return self.stats()

    def stats(self) -> Dict[str, Any]:
        self.ensure_loaded()
        by_pack = {p: len(rs) for p, rs in self._by_pack.items()}
        by_sev: Dict[str, int] = {}
        for r in self._rows:
            by_sev[r.severity] = by_sev.get(r.severity, 0) + 1
        return {
            "data_dir": str(self.data_dir),
            "loaded": self._loaded,
            "total_rows": len(self._rows),
            "packs": by_pack,
            "severity": by_sev,
            "errors": self._load_errors,
        }

    def _load_all(self) -> None:
        # First, if the configured data_dir is missing most of the expected
        # pack files, try the standard fallback locations. This keeps
        # dev/test happy when AION_DATA_DIR points at a different dir
        # (e.g. /tmp/aion-test-data for the RAG store) while the CSVs
        # live in the package-relative data/scenarios.
        self.data_dir = self._resolve_data_dir(self.data_dir)
        if not self.data_dir.is_dir():
            msg = f"scenario data_dir missing: {self.data_dir}"
            logger.error(msg)
            self._load_errors.append(msg)
            self._loaded = True  # avoid tight reload loops; empty store
            return

        for pack, filename in PACK_FILES.items():
            path = self.data_dir / filename
            if not path.is_file():
                msg = f"missing pack file: {path}"
                logger.warning(msg)
                self._load_errors.append(msg)
                continue
            try:
                rows = self._read_csv(pack, path)
                self._by_pack[pack] = rows
                self._rows.extend(rows)
                logger.info("loaded pack=%s rows=%d path=%s", pack, len(rows), path)
            except Exception as e:
                msg = f"failed pack={pack} path={path}: {e}"
                logger.exception(msg)
                self._load_errors.append(msg)

        self._loaded = True

    def _resolve_data_dir(self, primary: Path) -> Path:
        """If the primary dir has fewer than 2 pack files, scan the standard
        fallbacks and prefer one that has all 5. Keeps dev/test from being
        stuck on an empty AION_DATA_DIR."""
        try:
            present = sum(1 for f in PACK_FILES.values() if (primary / f).is_file())
        except OSError:
            present = 0
        if present >= len(PACK_FILES):
            return primary
        # Try the standard fallback locations
        here = Path(__file__).resolve()
        fallbacks = [
            here.parents[3] / "data" / "scenarios",
            here.parents[2] / "data" / "scenarios",
            Path.cwd() / "data" / "scenarios",
            Path("/app/data/scenarios"),
        ]
        for c in fallbacks:
            if c == primary:
                continue
            if not c.is_dir():
                continue
            try:
                count = sum(1 for f in PACK_FILES.values() if (c / f).is_file())
            except OSError:
                continue
            if count >= len(PACK_FILES):
                logger.info("scenario_store: falling back to %s (has %d/%d packs)", c, count, len(PACK_FILES))
                return c
        return primary

    def _read_csv(self, pack: str, path: Path) -> List[ScenarioRow]:
        rows: List[ScenarioRow] = []
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for i, raw in enumerate(reader):
                rid = (raw.get("id") or "").strip()
                trigger = (raw.get("trigger") or "").strip()
                if not rid or not trigger:
                    continue
                condition = (raw.get("condition") or "").strip()
                row = ScenarioRow(
                    id=rid,
                    pack=pack,
                    category=(raw.get("category") or "").strip(),
                    skill=(raw.get("skill") or "").strip(),
                    service=(raw.get("service") or "").strip(),
                    trigger=trigger,
                    condition=condition,
                    if_action=(raw.get("if_action") or "").strip(),
                    else_action=(raw.get("else_action") or "").strip(),
                    severity=(raw.get("severity") or "medium").strip().lower(),
                    source_doc=(raw.get("source_doc") or "").strip(),
                    trigger_tokens=tokenize(trigger),
                    condition_tokens=tokenize(condition),
                )
                rows.append(row)
        return rows

    def iter_rows(
        self,
        pack: Optional[str] = None,
        category: Optional[str] = None,
        skill: Optional[str] = None,
        service: Optional[str] = None,
        severity_min: Optional[str] = None,
    ) -> Iterable[ScenarioRow]:
        self.ensure_loaded()
        sev_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        min_sev = sev_order.get((severity_min or "").lower(), None)

        if pack and pack != "all":
            source = self._by_pack.get(pack, [])
        else:
            source = self._rows

        for r in source:
            if category and r.category != category:
                continue
            if skill and r.skill and r.skill != skill:
                continue
            if service and r.service and r.service != service:
                continue
            if min_sev is not None:
                if sev_order.get(r.severity, 1) < min_sev:
                    continue
            yield r


# Process-wide singleton
_STORE: Optional[ScenarioStore] = None
_STORE_LOCK = threading.Lock()


def get_store(data_dir: Optional[Path | str] = None) -> ScenarioStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = ScenarioStore(data_dir=data_dir)
        return _STORE

# Back-compat aliases for v1 callers/tests
def resolve_scenarios_dir() -> Path:
    """Resolve the scenarios dir with a smart fallback. Returns the
    first dir (env-set or standard) that has all 5 pack files.
    If none has all 5, returns the env-set dir (or first standard
    location) so callers see the path the store will use."""
    env = os.environ.get("AION_DATA_DIR") or os.environ.get("AION_SCENARIO_DIR")
    env_candidates: list[Path] = []
    if env:
        env_candidates.append(Path(env) / "scenarios")
        env_candidates.append(Path(env))
    here = Path(__file__).resolve()
    std_candidates = [
        here.parents[3] / "data" / "scenarios",
        here.parents[2] / "data" / "scenarios",
        Path.cwd() / "data" / "scenarios",
        Path("/app/data/scenarios"),
    ]
    # Prefer a dir with all 5 packs
    for c in env_candidates + std_candidates:
        if c.is_dir() and all((c / f).is_file() for f in PACK_FILES.values()):
            return c
    # Fall back to anything that exists
    for c in env_candidates + std_candidates:
        if c.is_dir():
            return c
    return Path.cwd() / "data" / "scenarios"


PACKS: Dict[str, str] = PACK_FILES
