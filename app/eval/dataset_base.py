"""EvalDataset base — the unified abstraction every eval surface uses.

Per PRINCIPLES.md §16a (revised 2026-05-03): canaries / edge_cases /
workflow_regression / true_north are all INSTANCES of the same
EvalDataset abstraction. They differ in:

  * what they evaluate (target_kind: rules / llm / workflow / safety_gate)
  * what their case shape is (case_model: subclass of pydantic BaseModel)
  * whether they have a soft cap (default_cap: int or None)
  * how aggressive their fail mode is (fail_mode: hard_fail / soft_fail)

Common to all:

  * load() / count() — read JSONL from disk
  * append(case) — write one case (honors cap; calls on_evict if at cap)
  * run(evaluator) — walk every case + aggregate into DatasetReport
  * validate_meets_min_count() — for the skill manifest's hard contract

Subclasses live in ``app/eval/datasets.py`` (CanaryDataset,
EdgeCaseDataset, WorkflowDataset, TrueNorthDataset). Each new
workflow can subclass WorkflowDataset with its own case_model.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, ClassVar, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

LOGGER = logging.getLogger(__name__)


FailMode = Literal["hard_fail", "soft_fail"]
TargetKind = Literal["rules", "llm", "workflow", "safety_gate"]


# ─── result + report types ────────────────────────────────────────────


class CaseResult(BaseModel):
    """Outcome of running ONE case through its system-under-test."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    passed: bool
    actual: dict[str, Any] = Field(default_factory=dict)
    expected: dict[str, Any] = Field(default_factory=dict)
    fail_reason: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DatasetReport(BaseModel):
    """Aggregate outcome from running an entire dataset."""

    model_config = ConfigDict(extra="forbid")

    dataset_kind: str
    target_kind: str
    workflow_id: str
    total: int
    passed: int
    failed: int
    fail_mode: FailMode
    case_results: list[CaseResult] = Field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 1.0

    @property
    def ok(self) -> bool:
        """True iff the dataset's fail_mode considers this run a pass.
        hard_fail: zero failures required. soft_fail: any pass rate OK
        (the caller compares to a baseline elsewhere)."""

        if self.fail_mode == "hard_fail":
            return self.failed == 0
        return True  # soft_fail — the caller decides on P/R drop


# ─── EvalDataset base ─────────────────────────────────────────────────


class EvalDataset:
    """One eval dataset bound to a specific evaluation target.

    Subclasses set the class-level attrs (case_model, dataset_kind,
    target_kind, default_cap, default_fail_mode) and inherit the
    storage + iteration shape.

    Storage is JSONL on disk — one Pydantic-validated case per line.
    Atomic writes via tmp + rename.
    """

    # Subclass-level attrs — every concrete subclass MUST set these.
    case_model: ClassVar[type[BaseModel]]
    dataset_kind: ClassVar[str]
    target_kind: ClassVar[TargetKind]
    default_cap: ClassVar[Optional[int]] = None
    default_fail_mode: ClassVar[FailMode] = "hard_fail"

    def __init__(
        self,
        *,
        path: Path,
        workflow_id: str,
        cap: Optional[int] = None,
        min_count: int = 1,
        fail_mode: Optional[FailMode] = None,
    ) -> None:
        self.path = path
        self.workflow_id = workflow_id
        self.cap = cap if cap is not None else self.default_cap
        self.min_count = min_count
        self.fail_mode = fail_mode if fail_mode is not None else self.default_fail_mode

    # ── load / count ──────────────────────────────────────────────

    def load(self) -> list[BaseModel]:
        """Read every case from disk. Empty list if file doesn't exist."""

        if not self.path.exists():
            return []
        out: list[BaseModel] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            out.append(self.case_model.model_validate_json(line))
        return out

    def count(self) -> int:
        """Cheap row count without parsing each line."""

        if not self.path.exists():
            return 0
        return sum(
            1 for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )

    def at_cap(self) -> bool:
        return self.cap is not None and self.count() >= self.cap

    def validate_meets_min_count(self) -> Optional[str]:
        """Return error message if dataset has fewer than min_count rows.
        Used by the skill manifest loader's hard contract."""

        n = self.count()
        if n < self.min_count:
            return (
                f"{self.dataset_kind}: {n} rows but min_count={self.min_count}"
            )
        return None

    # ── append (cap-aware) ────────────────────────────────────────

    def append(
        self,
        case: BaseModel,
        on_evict: Optional[Callable[[BaseModel], None]] = None,
    ) -> None:
        """Append a single case. If at cap, evict one first (caller can
        archive via ``on_evict`` — used by True-North promotion).

        Idempotent on case_id: if a case with the same id exists,
        SKIPS (no double-write).
        """

        if not isinstance(case, self.case_model):
            raise TypeError(
                f"{self.dataset_kind}.append expected {self.case_model.__name__}, "
                f"got {type(case).__name__}"
            )

        cases = self.load()

        new_id = self._case_id(case)
        if any(self._case_id(c) == new_id for c in cases):
            LOGGER.info(
                "%s: case %s already present, skipping", self.dataset_kind, new_id,
            )
            return

        if self.cap is not None and len(cases) >= self.cap:
            evicted = self.evict_redundant(cases, case)
            if evicted is not None:
                evicted_id = self._case_id(evicted)
                cases = [c for c in cases if self._case_id(c) != evicted_id]
                if on_evict is not None:
                    try:
                        on_evict(evicted)
                    except Exception as exc:  # noqa: BLE001
                        LOGGER.warning(
                            "%s: on_evict callback failed for %s: %s",
                            self.dataset_kind, evicted_id, exc,
                        )

        cases.append(case)
        self._write_all(cases)

    def evict_redundant(
        self, existing: list[BaseModel], new_case: BaseModel,
    ) -> Optional[BaseModel]:
        """Pick which existing case to evict when at cap. Default
        strategy: oldest (first in file). Subclasses override for
        smarter clustering (e.g., EdgeCaseDataset evicts the row
        most-redundant with `new_case`).
        """

        return existing[0] if existing else None

    # ── run (orchestration over evaluator) ────────────────────────

    def run(
        self,
        evaluator: Callable[[BaseModel], CaseResult],
    ) -> DatasetReport:
        """Walk every case, invoke ``evaluator``, aggregate report.

        ``evaluator`` is workflow-specific — it knows how to feed the
        case through the system-under-test and produce a CaseResult.
        The dataset class provides storage + iteration only.
        """

        cases = self.load()
        results: list[CaseResult] = []
        for case in cases:
            try:
                results.append(evaluator(case))
            except Exception as exc:  # noqa: BLE001
                results.append(CaseResult(
                    case_id=self._case_id(case),
                    passed=False,
                    fail_reason=f"evaluator crashed: {type(exc).__name__}: {exc}",
                ))
        passed = sum(1 for r in results if r.passed)
        return DatasetReport(
            dataset_kind=self.dataset_kind,
            target_kind=self.target_kind,
            workflow_id=self.workflow_id,
            total=len(results),
            passed=passed,
            failed=len(results) - passed,
            fail_mode=self.fail_mode,
            case_results=results,
        )

    # ── helpers ───────────────────────────────────────────────────

    def _case_id(self, case: BaseModel) -> str:
        """Pull a stable id from a case — supports the four current
        case-id field names so subclasses don't have to override."""

        for attr in ("case_id", "edge_case_id", "rule_id", "disagreement_id"):
            if hasattr(case, attr):
                return str(getattr(case, attr))
        return str(id(case))

    def _write_all(self, cases: list[BaseModel]) -> None:
        """Atomic write (tmp + rename) so a crash mid-write doesn't
        leave a half-file the next read chokes on."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for c in cases:
                fh.write(c.model_dump_json() + "\n")
        tmp.replace(self.path)

    # ── repr ──────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(workflow_id={self.workflow_id!r}, "
            f"path={self.path}, cap={self.cap}, count={self.count()})"
        )
