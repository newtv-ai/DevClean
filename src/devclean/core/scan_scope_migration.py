"""One-time migration away from the legacy whole-profile default scan.

Older DevClean releases persisted ``include_user_profile=true`` into the sidecar
scan rules.  Merely changing the packaged default would therefore leave an
existing installation doing the same multi-hour profile traversal forever.

Migrate only an untouched legacy scan-scope document.  If the user customized
scan scope, preserve it.  A small marker makes this a one-time product-default
migration so a user can deliberately re-enable whole-profile scanning later.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from devclean.core import _user_rules_impl as _impl
from devclean.core._user_rules_impl import UserRules

_MARKER_NAME = ".targeted-scan-default-v1"
_ORIGINAL_LOAD_RULES = _impl.load_rules


def _marker_path() -> Path:
    return _impl.rules_dir() / _MARKER_NAME


def _atomic_write(path: Path, text: str) -> None:
    scratch = path.with_suffix(path.suffix + ".writing")
    scratch.write_text(text, encoding="utf-8", newline="\n")
    os.replace(scratch, path)


def _current_packaged_rules() -> UserRules | None:
    try:
        return _impl.parse_rule_documents(*_impl._packaged_documents())
    except (ImportError, OSError, UnicodeError, ValueError, _impl.RuleConfigError):
        return None


def _legacy_default_scan_matches(rules: UserRules, packaged: UserRules) -> bool:
    scan = rules.scan
    target = packaged.scan
    if not scan.include_user_profile or target.include_user_profile:
        return False
    # The previous packaged default differs from the new one only in this
    # switch.  Equality across the rest of ScanRules means this is a product
    # default migration, not an override of a user's custom scan scope.
    return replace(scan, include_user_profile=False) == target


def _migrate_once(rules: UserRules) -> UserRules:
    marker = _marker_path()
    if marker.is_file():
        return rules
    packaged = _current_packaged_rules()
    if packaged is None:
        return rules

    migrated = rules
    if _legacy_default_scan_matches(rules, packaged):
        migrated = replace(rules, scan=packaged.scan)
        scan_text = _impl.render_rule_documents(migrated)[0]
        _atomic_write(_impl.scan_rules_path(), scan_text)

    # Mark both migrated and intentionally customized installations.  After the
    # first launch on this product version, an explicit user choice to scan the
    # whole profile must remain respected.
    try:
        marker.write_text("targeted known/application roots are the default\n", encoding="utf-8")
    except OSError:
        # Failure to remember the migration is non-fatal; the exact-scope guard
        # above still prevents changing a customized document.
        pass
    return migrated


def load_rules(*, create_missing: bool = True) -> UserRules:
    return _migrate_once(_ORIGINAL_LOAD_RULES(create_missing=create_missing))


def install() -> None:
    if getattr(_impl, "_devclean_targeted_scan_migration", False):
        return
    _impl.load_rules = load_rules
    vars(_impl)["_devclean_targeted_scan_migration"] = True


install()


__all__ = ["install", "load_rules"]
