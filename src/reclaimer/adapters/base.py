"""Read-only adapter contracts for the current milestone."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from reclaimer.core.models import Resource
from reclaimer.evidence.models import CommandEvidence, EvidenceRecord, LoopbackEvidence
from reclaimer.evidence.store import EvidenceStore
from reclaimer.platform.windows.process import BoundedProcessResult

if TYPE_CHECKING:
    from reclaimer.adapters.command import QueryCommand


class ProbeStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    adapter_id: str
    status: ProbeStatus
    version: str | None = None
    executable: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class AdapterIssue:
    code: str
    message: str
    fatal: bool = False


@dataclass(frozen=True, slots=True)
class ObservedCommand:
    result: BoundedProcessResult
    evidence: CommandEvidence


@dataclass(frozen=True, slots=True)
class InventoryResult:
    adapter_id: str
    probe: ProbeResult
    resources: tuple[Resource, ...] = ()
    issues: tuple[AdapterIssue, ...] = ()
    evidence: tuple[EvidenceRecord, ...] = ()

    def __post_init__(self) -> None:
        if self.probe.adapter_id != self.adapter_id:
            raise ValueError("probe adapter_id does not match inventory result")
        if any(resource.adapter_id != self.adapter_id for resource in self.resources):
            raise ValueError("resource adapter_id does not match inventory result")
        if any(resource.actionable for resource in self.resources):
            raise ValueError("inventory adapters cannot emit actionable resources")
        if any(item.adapter_id != self.adapter_id for item in self.evidence):
            raise ValueError("evidence adapter_id does not match inventory result")


QueryRunner = Callable[["QueryCommand"], BoundedProcessResult]


@dataclass(slots=True)
class AdapterContext:
    scan_id: str
    evidence_store: EvidenceStore
    runner: QueryRunner

    def observe(self, command: QueryCommand) -> ObservedCommand:
        executable_observation = self.evidence_store.observe_executable(
            command.executable
        )
        result = self.runner(command)
        evidence = self.evidence_store.record_command(
            adapter_id=command.adapter_id,
            executable=command.executable,
            effect_class=command.effect_class,
            result=result,
            expected_executable=executable_observation,
        )
        return ObservedCommand(result=result, evidence=evidence)

    def record_loopback_response(
        self,
        *,
        adapter_id: str,
        endpoint: str,
        status: int,
        duration_ms: int,
        body: bytes,
        content_type: str | None,
        content_encoding: str | None,
    ) -> LoopbackEvidence:
        return self.evidence_store.record_loopback_response(
            adapter_id=adapter_id,
            endpoint=endpoint,
            status=status,
            duration_ms=duration_ms,
            body=body,
            content_type=content_type,
            content_encoding=content_encoding,
        )

    def record_loopback_failure(
        self,
        *,
        adapter_id: str,
        endpoint: str,
        error_type: str,
        duration_ms: int,
        timed_out: bool,
        output_limit_exceeded: bool,
        body: bytes = b"",
        content_type: str | None = None,
        content_encoding: str | None = None,
    ) -> LoopbackEvidence:
        return self.evidence_store.record_loopback_failure(
            adapter_id=adapter_id,
            endpoint=endpoint,
            error_type=error_type,
            duration_ms=duration_ms,
            timed_out=timed_out,
            output_limit_exceeded=output_limit_exceeded,
            body=body,
            content_type=content_type,
            content_encoding=content_encoding,
        )


class InventoryAdapter(Protocol):
    """An adapter that cannot execute maintenance or deletion."""

    id: str

    def inventory(self, context: AdapterContext) -> InventoryResult: ...
