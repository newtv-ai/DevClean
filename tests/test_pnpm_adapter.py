from __future__ import annotations

from pathlib import Path

from reclaimer.adapters.base import AdapterContext, ProbeStatus
from reclaimer.adapters.pnpm import PnpmStoreAdapter, discover_store_roots
from reclaimer.core.models import RiskTier, SemanticType
from reclaimer.evidence.store import EvidenceStore


def test_pnpm_root_discovery_deduplicates_conventional_locations(tmp_path: Path) -> None:
    pnpm_home = tmp_path / "pnpm-home"
    local_appdata = tmp_path / "local"
    volume = tmp_path / "volume"
    home_v10 = pnpm_home / "store" / "v10"
    local_v11 = local_appdata / "pnpm" / "store" / "v11"
    volume_v3 = volume / ".pnpm-store" / "v3"
    for root in (home_v10, local_v11, volume_v3):
        root.mkdir(parents=True)

    roots = discover_store_roots(
        {"PNPM_HOME": str(pnpm_home), "LOCALAPPDATA": str(local_appdata)},
        (volume,),
    )

    assert set(roots) == {home_v10, local_v11, volume_v3}


def test_pnpm_adapter_never_calls_vendor_runner(tmp_path: Path) -> None:
    pnpm_home = tmp_path / "pnpm-home"
    v10 = pnpm_home / "store" / "v10"
    v3 = pnpm_home / "store" / "v3"
    v10.mkdir(parents=True)
    v3.mkdir()
    (v10 / "package.bin").write_bytes(b"package")
    (v3 / "legacy.bin").write_bytes(b"legacy")

    def forbidden_runner(command):
        raise AssertionError(f"pnpm vendor command must not run: {command.argv}")

    context = AdapterContext(
        "scan_fixture",
        EvidenceStore("scan_fixture", root=tmp_path / "evidence"),
        forbidden_runner,
    )
    result = PnpmStoreAdapter(
        environment={"PNPM_HOME": str(pnpm_home)},
        volume_roots=(),
    ).inventory(context)

    assert result.probe.status is ProbeStatus.AVAILABLE
    assert len(result.resources) == 2
    by_name = {Path(resource.path or "").name: resource for resource in result.resources}
    assert by_name["v10"].semantic_type is SemanticType.PACKAGE_STORE
    assert by_name["v10"].risk_tier is RiskTier.YELLOW
    assert by_name["v3"].semantic_type is SemanticType.UNKNOWN
    assert by_name["v3"].risk_tier is RiskTier.RED
    assert all(resource.actionable is False for resource in result.resources)
    assert result.evidence == ()
    assert any(issue.code == "CUSTOM_ROOTS_NOT_DISCOVERED" for issue in result.issues)


def test_pnpm_no_root_is_visible_not_silently_successful(tmp_path: Path) -> None:
    context = AdapterContext(
        "scan_fixture",
        EvidenceStore("scan_fixture", root=tmp_path / "evidence"),
        lambda command: (_ for _ in ()).throw(AssertionError(command.argv)),
    )
    result = PnpmStoreAdapter(environment={}, volume_roots=()).inventory(context)

    assert result.probe.status is ProbeStatus.AVAILABLE
    assert result.resources == ()
    assert any(issue.code == "NO_CONVENTIONAL_ROOT" for issue in result.issues)
