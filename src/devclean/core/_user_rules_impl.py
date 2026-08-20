"""The single configuration layer for DevClean.

Exactly three public JSON documents control scanning, deterministic deletion,
and deterministic retention.  Python contains parsers and matching algorithms,
not a second copy of the rule data.
"""

# Chinese validation messages use fullwidth punctuation.
# ruff: noqa: RUF001

from __future__ import annotations

import fnmatch
import glob
import hashlib
import json
import os
import re
import zipfile
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Final

from devclean.core.json_contract import strict_json_loads
from devclean.core.paths import data_dir
from devclean.core.rule_schema import CleanupCategory, CleanupPolicy, SourceDomain

SCHEMA_VERSION: Final = 3
MAX_DECISION_RULES: Final = 100_000
MAX_REASON_CHARS: Final = 500
MAX_RULE_VALUE_CHARS: Final = 32_767
_DIRECTORY_DECISION_SOURCE: Final = "USER_DIRECTORY_DECISION"
_PRODUCT_RULE_SOURCE: Final = "PRODUCT_AUDITED"

SCAN_RULES_NAME: Final = "scan-rules.json"
DELETE_RULES_NAME: Final = "delete-rules.json"
KEEP_RULES_NAME: Final = "keep-rules.json"
DEFAULT_BACKUP_NAME: Final = "DevClean-default-rules-backup.zip"
_CONFIG_NAMES: Final = (SCAN_RULES_NAME, DELETE_RULES_NAME, KEEP_RULES_NAME)
_DOCUMENT_TYPES: Final = {
    SCAN_RULES_NAME: "devclean.scan_rules",
    DELETE_RULES_NAME: "devclean.delete_rules",
    KEEP_RULES_NAME: "devclean.keep_rules",
}
_CONTRACT_KEYS: Final = frozenset({"contract_version", "output_format", "instructions"})
_SCAN_TOP_LEVEL_KEYS: Final = frozenset(
    {
        "schema_version",
        "document_type",
        "_help",
        "_ai_editing_contract",
        "include_user_profile",
        "include_known_cleanup_roots",
        "review_sample_per_category",
        "additional_paths",
        "excluded_paths",
        "delete_root_ids",
        "skip_directory_groups",
        "known_cleanup_roots",
    }
)
_DELETE_TOP_LEVEL_KEYS: Final = frozenset(
    {
        "schema_version",
        "document_type",
        "_help",
        "_ai_editing_contract",
        "thresholds",
        "classification",
        "classification_groups",
        "match_help",
        "rules",
    }
)
_KEEP_TOP_LEVEL_KEYS: Final = frozenset(
    {
        "schema_version",
        "document_type",
        "_help",
        "_ai_editing_contract",
        "classification",
        "classification_groups",
        "match_help",
        "rules",
    }
)
_KNOWN_ROOT_KEYS: Final = frozenset(
    {
        "id",
        "group",
        "enabled",
        "allow_inside_system_anchor",
        "patterns",
        "category",
        "policy",
        "label",
    }
)
_DECISION_RULE_KEYS: Final = frozenset(
    {
        "id",
        "group",
        "enabled",
        "match",
        "value",
        "source",
        "reason",
        "updated_at",
    }
)
_THRESHOLD_KEYS: Final = frozenset({"old_temp_days", "large_file_bytes", "stale_metadata_days"})
_DELETE_CLASSIFICATION_KEYS: Final = frozenset(
    {
        "development_cache_segments",
        "regenerable_tool_directories",
        "byproduct_suffixes",
        "cache_directory_names",
        "byproduct_segments",
        "build_segments",
        "ide_segments",
        "version_name_regex",
        "version_separators_regex",
        "self_updater_parents",
        "cache_segments",
        "container_segments",
        "container_suffixes",
        "windows_update_segments",
        "windows_update_segment_groups",
        "conda_segments",
        "downloads_segments",
        "inferred_ai_review_categories",
        "inferred_report_only_categories",
        "system_log_suffixes",
        "installer_suffixes",
        "category_source_domains",
    }
)
_KEEP_CLASSIFICATION_KEYS: Final = frozenset(
    {
        "application_data_segments",
        "protected_system_root_names",
        "protected_system_file_names",
        "program_payload_suffixes",
        "application_state_suffixes",
        "installed_payload_segments",
        "application_state_names",
        "application_state_tails",
    }
)


class RuleConfigError(ValueError):
    """A user-edited rule document is invalid."""


class RuleDecision(StrEnum):
    DELETE = "DELETE"
    KEEP = "KEEP"


class RuleMatch(StrEnum):
    EXACT_PATH = "exact_path"
    PATH_PREFIX = "path_prefix"
    PATH_GLOB = "path_glob"
    FILENAME_GLOB = "filename_glob"
    PATH_REGEX = "path_regex"
    FILENAME_REGEX = "filename_regex"


_MATCH_HELP_KEYS: Final = frozenset(match.value for match in RuleMatch)


@dataclass(frozen=True, slots=True)
class KnownRootRule:
    rule_id: str
    group: str
    patterns: tuple[str, ...]
    category: str
    policy: str
    label: str
    enabled: bool = True
    allow_inside_system_anchor: bool = False


@dataclass(frozen=True, slots=True)
class RuleDocumentMetadata:
    help_text: str
    contract_version: int
    output_format: str
    instructions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ScanRules:
    metadata: RuleDocumentMetadata
    include_user_profile: bool
    include_known_cleanup_roots: bool
    review_sample_per_category: int
    additional_paths: tuple[str, ...]
    excluded_paths: tuple[str, ...]
    delete_root_ids: frozenset[str]
    skip_directory_groups: dict[str, tuple[str, ...]]
    known_cleanup_roots: tuple[KnownRootRule, ...]

    @property
    def skip_directory_names(self) -> tuple[str, ...]:
        return tuple(name for names in self.skip_directory_groups.values() for name in names)


@dataclass(frozen=True, slots=True)
class DeleteClassification:
    old_temp_days: int
    large_file_bytes: int
    stale_metadata_days: int
    development_cache_segments: frozenset[str]
    regenerable_tool_directories: frozenset[str]
    byproduct_suffixes: frozenset[str]
    cache_directory_names: frozenset[str]
    byproduct_segments: frozenset[str]
    build_segments: frozenset[str]
    ide_segments: frozenset[str]
    version_name_regex: str
    version_separators_regex: str
    self_updater_parents: frozenset[str]
    cache_segments: frozenset[str]
    container_segments: frozenset[str]
    container_suffixes: frozenset[str]
    windows_update_segments: frozenset[str]
    windows_update_segment_groups: tuple[frozenset[str], ...]
    conda_segments: frozenset[str]
    downloads_segments: frozenset[str]
    inferred_ai_review_categories: frozenset[str]
    inferred_report_only_categories: frozenset[str]
    system_log_suffixes: frozenset[str]
    installer_suffixes: frozenset[str]
    category_source_domains: dict[str, str]


@dataclass(frozen=True, slots=True)
class KeepClassification:
    application_data_segments: frozenset[str]
    protected_system_root_names: tuple[str, ...]
    protected_system_file_names: frozenset[str]
    program_payload_suffixes: frozenset[str]
    application_state_suffixes: frozenset[str]
    installed_payload_segments: frozenset[str]
    application_state_names: frozenset[str]
    application_state_tails: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DecisionRule:
    rule_id: str
    group: str
    match: RuleMatch
    value: str
    enabled: bool = True
    source: str = "USER"
    reason: str = ""
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class DeleteRules:
    metadata: RuleDocumentMetadata
    classification: DeleteClassification
    classification_groups: dict[str, tuple[str, ...]]
    match_help: dict[str, str]
    rules: tuple[DecisionRule, ...] = ()


@dataclass(frozen=True, slots=True)
class KeepRules:
    metadata: RuleDocumentMetadata
    classification: KeepClassification
    classification_groups: dict[str, tuple[str, ...]]
    match_help: dict[str, str]
    rules: tuple[DecisionRule, ...] = ()


@dataclass(frozen=True, slots=True)
class _DecisionMatcher:
    exact_paths: frozenset[str]
    path_prefixes: tuple[str, ...]
    path_globs: tuple[str, ...]
    filename_globs: tuple[str, ...]
    path_regexes: tuple[re.Pattern[str], ...]
    filename_regexes: tuple[re.Pattern[str], ...]

    @classmethod
    def compile(cls, rules: tuple[DecisionRule, ...]) -> _DecisionMatcher:
        exact_paths: set[str] = set()
        path_prefixes: list[str] = []
        path_globs: list[str] = []
        filename_globs: list[str] = []
        path_regexes: list[re.Pattern[str]] = []
        filename_regexes: list[re.Pattern[str]] = []
        for rule in rules:
            if not rule.enabled:
                continue
            if rule.match is RuleMatch.PATH_REGEX:
                path_regexes.append(re.compile(rule.value, re.IGNORECASE))
            elif rule.match is RuleMatch.FILENAME_REGEX:
                filename_regexes.append(re.compile(rule.value, re.IGNORECASE))
            else:
                value = expand_path(rule.value)
                if rule.match is RuleMatch.EXACT_PATH:
                    exact_paths.add(normalise_path(value).casefold())
                elif rule.match is RuleMatch.PATH_PREFIX:
                    path_prefixes.append(normalise_path(value).casefold())
                elif rule.match is RuleMatch.PATH_GLOB:
                    path_globs.append(os.path.normpath(value).casefold())
                else:
                    filename_globs.append(value.casefold())
        return cls(
            exact_paths=frozenset(exact_paths),
            path_prefixes=tuple(path_prefixes),
            path_globs=tuple(path_globs),
            filename_globs=tuple(filename_globs),
            path_regexes=tuple(path_regexes),
            filename_regexes=tuple(filename_regexes),
        )

    def matches(self, path: str | Path) -> bool:
        normalized = normalise_path(path)
        folded = normalized.casefold()
        if folded in self.exact_paths:
            return True
        if any(
            folded == prefix or folded.startswith(prefix.rstrip(os.sep) + os.sep)
            for prefix in self.path_prefixes
        ):
            return True
        if any(fnmatch.fnmatchcase(folded, pattern) for pattern in self.path_globs):
            return True
        regex_path = normalized.replace("\\", "/")
        if any(pattern.search(regex_path) for pattern in self.path_regexes):
            return True
        name = os.path.basename(normalized).casefold()
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in self.filename_globs):
            return True
        return any(pattern.search(name) for pattern in self.filename_regexes)


@dataclass(frozen=True, slots=True)
class UserRules:
    scan: ScanRules
    delete: DeleteRules
    keep: KeepRules
    _delete_matcher: _DecisionMatcher = field(init=False, repr=False, compare=False)
    _keep_matcher: _DecisionMatcher = field(init=False, repr=False, compare=False)
    _delete_directory_matcher: _DecisionMatcher = field(init=False, repr=False, compare=False)
    _keep_directory_matcher: _DecisionMatcher = field(init=False, repr=False, compare=False)
    _ai_rule_count: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        file_delete = tuple(
            rule for rule in self.delete.rules if rule.source != _DIRECTORY_DECISION_SOURCE
        )
        file_keep = tuple(
            rule for rule in self.keep.rules if rule.source != _DIRECTORY_DECISION_SOURCE
        )
        directory_delete = tuple(
            rule
            for rule in self.delete.rules
            if rule.source == _DIRECTORY_DECISION_SOURCE and rule.match is RuleMatch.EXACT_PATH
        )
        directory_keep = tuple(
            rule
            for rule in self.keep.rules
            if rule.source == _DIRECTORY_DECISION_SOURCE and rule.match is RuleMatch.EXACT_PATH
        )
        object.__setattr__(self, "_delete_matcher", _DecisionMatcher.compile(file_delete))
        object.__setattr__(self, "_keep_matcher", _DecisionMatcher.compile(file_keep))
        object.__setattr__(
            self, "_delete_directory_matcher", _DecisionMatcher.compile(directory_delete)
        )
        object.__setattr__(
            self, "_keep_directory_matcher", _DecisionMatcher.compile(directory_keep)
        )
        object.__setattr__(
            self,
            "_ai_rule_count",
            sum(
                rule.source == "AI_IMPORT"
                for group in (self.delete.rules, self.keep.rules)
                for rule in group
            ),
        )

    def decision_for(self, path: str | Path) -> RuleDecision | None:
        """Return the configured file decision, with KEEP taking priority."""

        if self._keep_matcher.matches(path):
            return RuleDecision.KEEP
        if self._delete_matcher.matches(path):
            return RuleDecision.DELETE
        return None

    def directory_decision_for(self, path: str | Path) -> RuleDecision | None:
        """Return only an explicit exact-path directory decision from the UI."""

        if self._keep_directory_matcher.matches(path):
            return RuleDecision.KEEP
        if self._delete_directory_matcher.matches(path):
            return RuleDecision.DELETE
        return None

    def is_within_kept_directory(self, path: str | Path) -> bool:
        """Return whether *path* is inside an explicitly kept directory."""

        candidate = Path(normalise_path(path))
        return any(
            self._keep_directory_matcher.matches(parent)
            for parent in (candidate, *candidate.parents)
        )

    @property
    def ai_rule_count(self) -> int:
        return self._ai_rule_count


def rules_dir() -> Path:
    return data_dir() / "rules"


def scan_rules_path() -> Path:
    return rules_dir() / SCAN_RULES_NAME


def delete_rules_path() -> Path:
    return rules_dir() / DELETE_RULES_NAME


def keep_rules_path() -> Path:
    return rules_dir() / KEEP_RULES_NAME


def default_backup_path() -> Path:
    return rules_dir() / DEFAULT_BACKUP_NAME


def default_rules() -> UserRules:
    """Parse the three packaged templates; there are no Python defaults."""

    return parse_rule_documents(*_packaged_documents())


def load_rules(*, create_missing: bool = True) -> UserRules:
    """Load the three current-format documents.

    Active sidecar files stay authoritative. Packaged templates are consulted
    best-effort on normal launches so the visible default backup tracks the
    *current executable* rather than whichever DevClean version first created
    the data directory. Missing activity files prefer the current packaged
    template and fall back to the sidecar backup only when packaged resources
    are temporarily unavailable.

    One historical release accidentally shipped machine-specific learned
    decisions inside the packaged DELETE/KEEP templates. When an old sidecar
    backup proves that an unchanged active decision came from that packaged
    baseline, remove only that exact inherited entry. User/AI rules created
    later, or edited since installation, are not present byte-for-byte in the
    old backup and are preserved.
    """

    paths = (scan_rules_path(), delete_rules_path(), keep_rules_path())
    if not create_missing:
        return parse_rule_documents(*read_rule_documents())

    rules_dir().mkdir(parents=True, exist_ok=True)
    packaged: tuple[str, str, str] | None = None
    with suppress(ImportError, OSError, RuleConfigError, UnicodeError):
        packaged = _packaged_documents()

    missing = tuple(path for path in paths if not path.is_file())
    if missing:
        defaults: tuple[str, str, str] | None = packaged
        if defaults is None and default_backup_path().is_file():
            defaults = _default_backup_documents()
        if defaults is None:
            # Preserve the previous clear failure when neither current packaged
            # resources nor a valid sidecar default backup can supply a missing
            # required document.
            defaults = _packaged_documents()
        for path, document in zip(paths, defaults, strict=True):
            if not path.is_file():
                _atomic_write(path, document)

    if packaged is not None and default_backup_path().is_file():
        with suppress(ImportError, OSError, RuleConfigError, UnicodeError):
            _migrate_legacy_packaged_decisions(packaged)

    if packaged is not None:
        with suppress(ImportError, OSError, RuleConfigError, UnicodeError):
            _ensure_default_backup(packaged)

    active = parse_rule_documents(*read_rule_documents())
    if packaged is None:
        return active
    current_defaults = parse_rule_documents(*packaged)
    return _overlay_packaged_product_rules(active, current_defaults)


def restore_default_rules() -> UserRules:
    """Restore active files from the current executable's packaged defaults.

    The visible sidecar ZIP remains a fallback for a damaged/unavailable
    resource package, but an old ZIP must never pin "restore defaults" to a
    previous DevClean release.
    """

    try:
        defaults = _packaged_documents()
    except (ImportError, OSError, RuleConfigError, UnicodeError):
        defaults = _default_backup_documents()
    restored = parse_rule_documents(*defaults)
    rules_dir().mkdir(parents=True, exist_ok=True)
    for path, document in zip(
        (scan_rules_path(), delete_rules_path(), keep_rules_path()),
        defaults,
        strict=True,
    ):
        _atomic_write(path, document)
    with suppress(ImportError, OSError, RuleConfigError, UnicodeError):
        _ensure_default_backup(defaults)
    return restored


def _migrate_legacy_packaged_decisions(
    packaged: tuple[str, str, str],
) -> None:
    """Remove unchanged accidental AI imports while allowing audited defaults."""

    # The historical contamination was specifically a snapshot of AI_IMPORT
    # decisions from one development machine. Product-audited rules deliberately
    # shipped by a newer executable must not disable this cleanup migration.
    parse_rule_documents(*packaged)
    legacy_defaults = parse_rule_documents(*_default_backup_documents())
    legacy_delete = frozenset(
        rule for rule in legacy_defaults.delete.rules if rule.source == "AI_IMPORT"
    )
    legacy_keep = frozenset(
        rule for rule in legacy_defaults.keep.rules if rule.source == "AI_IMPORT"
    )
    if not legacy_delete and not legacy_keep:
        return

    active = parse_rule_documents(*read_rule_documents())
    delete_rules = tuple(rule for rule in active.delete.rules if rule not in legacy_delete)
    keep_rules = tuple(rule for rule in active.keep.rules if rule not in legacy_keep)
    if delete_rules == active.delete.rules and keep_rules == active.keep.rules:
        return

    save_rules(
        UserRules(
            scan=active.scan,
            delete=replace(active.delete, rules=delete_rules),
            keep=replace(active.keep, rules=keep_rules),
        )
    )


def _overlay_packaged_product_rules(active: UserRules, packaged: UserRules) -> UserRules:
    """Apply current product file knowledge independently of local history."""

    product_delete = tuple(
        rule for rule in packaged.delete.rules if rule.source == _PRODUCT_RULE_SOURCE
    )
    product_keep = tuple(
        rule for rule in packaged.keep.rules if rule.source == _PRODUCT_RULE_SOURCE
    )
    local_delete = tuple(
        rule for rule in active.delete.rules if rule.source != _PRODUCT_RULE_SOURCE
    )
    local_keep = tuple(rule for rule in active.keep.rules if rule.source != _PRODUCT_RULE_SOURCE)
    return UserRules(
        scan=active.scan,
        delete=replace(active.delete, rules=(*product_delete, *local_delete)),
        keep=replace(active.keep, rules=(*product_keep, *local_keep)),
    )


def read_rule_documents(*, errors: str = "strict") -> tuple[str, str, str]:
    """Read the three files verbatim, including invalid JSON for UI repair."""

    try:
        return (
            scan_rules_path().read_text(encoding="utf-8", errors=errors),
            delete_rules_path().read_text(encoding="utf-8", errors=errors),
            keep_rules_path().read_text(encoding="utf-8", errors=errors),
        )
    except (OSError, UnicodeError) as error:
        raise RuleConfigError(f"无法读取规则文件：{error}") from error


def save_rules(rules: UserRules) -> None:
    """Atomically replace the three validated documents."""

    scan_text, delete_text, keep_text = render_rule_documents(rules)
    rules_dir().mkdir(parents=True, exist_ok=True)
    for path, text in (
        (scan_rules_path(), scan_text),
        (delete_rules_path(), delete_text),
        (keep_rules_path(), keep_text),
    ):
        _atomic_write(path, text)


def render_rule_documents(rules: UserRules) -> tuple[str, str, str]:
    """Return stable, human-editable JSON for the three documents."""

    scan_document = {
        "schema_version": SCHEMA_VERSION,
        "document_type": _DOCUMENT_TYPES[SCAN_RULES_NAME],
        "_help": rules.scan.metadata.help_text,
        "_ai_editing_contract": _metadata_contract_document(rules.scan.metadata),
        "include_user_profile": rules.scan.include_user_profile,
        "include_known_cleanup_roots": rules.scan.include_known_cleanup_roots,
        "review_sample_per_category": rules.scan.review_sample_per_category,
        "additional_paths": list(rules.scan.additional_paths),
        "excluded_paths": list(rules.scan.excluded_paths),
        "delete_root_ids": sorted(rules.scan.delete_root_ids),
        "skip_directory_groups": {
            group: list(names) for group, names in rules.scan.skip_directory_groups.items()
        },
        "known_cleanup_roots": [
            {
                "id": rule.rule_id,
                "group": rule.group,
                "enabled": rule.enabled,
                "allow_inside_system_anchor": rule.allow_inside_system_anchor,
                "patterns": list(rule.patterns),
                "category": rule.category,
                "policy": rule.policy,
                "label": rule.label,
            }
            for rule in rules.scan.known_cleanup_roots
        ],
    }
    delete_document = {
        "schema_version": SCHEMA_VERSION,
        "document_type": _DOCUMENT_TYPES[DELETE_RULES_NAME],
        "_help": rules.delete.metadata.help_text,
        "_ai_editing_contract": _metadata_contract_document(rules.delete.metadata),
        "thresholds": {
            "old_temp_days": rules.delete.classification.old_temp_days,
            "large_file_bytes": rules.delete.classification.large_file_bytes,
            "stale_metadata_days": rules.delete.classification.stale_metadata_days,
        },
        "classification": _delete_classification_document(rules.delete.classification),
        "classification_groups": {
            group: list(fields) for group, fields in rules.delete.classification_groups.items()
        },
        "match_help": dict(rules.delete.match_help),
        "rules": _decision_rule_documents(rules.delete.rules),
    }
    keep_document = {
        "schema_version": SCHEMA_VERSION,
        "document_type": _DOCUMENT_TYPES[KEEP_RULES_NAME],
        "_help": rules.keep.metadata.help_text,
        "_ai_editing_contract": _metadata_contract_document(rules.keep.metadata),
        "classification": _keep_classification_document(rules.keep.classification),
        "classification_groups": {
            group: list(fields) for group, fields in rules.keep.classification_groups.items()
        },
        "match_help": dict(rules.keep.match_help),
        "rules": _decision_rule_documents(rules.keep.rules),
    }
    return (_render(scan_document), _render(delete_document), _render(keep_document))


def parse_rule_documents(scan_text: str, delete_text: str, keep_text: str) -> UserRules:
    """Parse and validate the three editor buffers as one configuration."""

    scan_payload = _parse_document(scan_text, SCAN_RULES_NAME, _SCAN_TOP_LEVEL_KEYS)
    delete_payload = _parse_document(delete_text, DELETE_RULES_NAME, _DELETE_TOP_LEVEL_KEYS)
    keep_payload = _parse_document(keep_text, KEEP_RULES_NAME, _KEEP_TOP_LEVEL_KEYS)
    return UserRules(
        scan=_parse_scan_rules(scan_payload),
        delete=_parse_delete_rules(delete_payload),
        keep=_parse_keep_rules(keep_payload),
    )


def add_ai_verdicts(
    rules: UserRules,
    verdicts: list[tuple[str, RuleDecision, str]],
) -> UserRules:
    """Persist AI decisions as portable exact and conservative shape rules."""

    return _add_verdict_rules(
        rules,
        verdicts,
        source="AI_IMPORT",
        group="ai_import",
    )


def add_user_verdicts(
    rules: UserRules,
    verdicts: list[tuple[str, RuleDecision, str]],
) -> UserRules:
    """Persist the user's final file decision with reusable file-rule shapes."""

    return _add_verdict_rules(
        rules,
        verdicts,
        source="USER_DECISION",
        group="user_decision",
    )


def add_user_directory_verdicts(
    rules: UserRules,
    verdicts: list[tuple[str, RuleDecision, str]],
) -> UserRules:
    """Persist explicit directory choices as exact-path-only rules."""

    incoming: dict[str, tuple[str, RuleDecision, str]] = {}
    for path, decision, reason in verdicts:
        normalized = normalise_path(path)
        incoming[normalized.casefold()] = (normalized, decision, reason)
    if not incoming:
        return rules

    removal = {
        _stored_rule_key(RuleMatch.EXACT_PATH, path)
        for path, _decision, _reason in incoming.values()
    }
    delete = [
        rule
        for rule in rules.delete.rules
        if not (
            rule.source == _DIRECTORY_DECISION_SOURCE
            and _stored_rule_key(rule.match, rule.value) in removal
        )
    ]
    keep = [
        rule
        for rule in rules.keep.rules
        if not (
            rule.source == _DIRECTORY_DECISION_SOURCE
            and _stored_rule_key(rule.match, rule.value) in removal
        )
    ]
    used_ids = {rule.rule_id for rule in (*delete, *keep)}
    now = datetime.now(UTC).isoformat()
    for path, decision, reason in incoming.values():
        rule_id = _unique_rule_id(
            _decision_rule_id(_DIRECTORY_DECISION_SOURCE, f"exact_path:{path}"),
            used_ids,
        )
        used_ids.add(rule_id)
        entry = DecisionRule(
            rule_id=rule_id,
            group="user_directory_decision",
            match=RuleMatch.EXACT_PATH,
            value=path,
            source=_DIRECTORY_DECISION_SOURCE,
            reason=reason[:MAX_REASON_CHARS],
            updated_at=now,
        )
        (delete if decision is RuleDecision.DELETE else keep).append(entry)
    updated = UserRules(
        scan=rules.scan,
        delete=replace(rules.delete, rules=_bounded_rules(delete)),
        keep=replace(rules.keep, rules=_bounded_rules(keep)),
    )
    save_rules(updated)
    return updated


def _add_verdict_rules(
    rules: UserRules,
    verdicts: list[tuple[str, RuleDecision, str]],
    *,
    source: str,
    group: str,
) -> UserRules:
    incoming: dict[str, tuple[str, RuleDecision, str]] = {}
    for path, decision, reason in verdicts:
        normalized = normalise_path(path)
        incoming[normalized.casefold()] = (normalized, decision, reason)

    requested: dict[
        tuple[RuleMatch, str],
        list[tuple[str, RuleMatch, RuleDecision, str]],
    ] = {}
    removal_keys: set[tuple[RuleMatch, str]] = set()
    for normalized, decision, reason in incoming.values():
        removal_keys.add(_stored_rule_key(RuleMatch.EXACT_PATH, normalized))
        for match, value in _decision_rule_shapes(normalized, decision=decision, reason=reason):
            key = _stored_rule_key(match, value)
            removal_keys.add(key)
            requested.setdefault(key, []).append((value, match, decision, reason))

    # A reusable type rule is valid only while every observation of that shape
    # agrees. Conflicting evidence removes the broad rule and leaves the
    # portable exact-path decisions in place.
    existing_decisions: dict[tuple[RuleMatch, str], set[RuleDecision]] = {}
    existing_exact: list[tuple[RuleDecision, str]] = []
    for decision, decision_rules in (
        (RuleDecision.DELETE, rules.delete.rules),
        (RuleDecision.KEEP, rules.keep.rules),
    ):
        for rule in decision_rules:
            if (
                rule.enabled
                and rule.match is RuleMatch.EXACT_PATH
                and not (
                    rule.source == source
                    and _stored_rule_key(rule.match, rule.value) in removal_keys
                )
            ):
                existing_exact.append(
                    (
                        decision,
                        normalise_path(expand_path(rule.value)),
                    )
                )
            if rule.source != source:
                continue
            shapes = (
                _decision_rule_shapes(
                    normalise_path(expand_path(rule.value)),
                    decision=decision,
                    reason=rule.reason,
                )
                if rule.match is RuleMatch.EXACT_PATH
                else ((rule.match, rule.value),)
            )
            for match, value in shapes:
                existing_decisions.setdefault(_stored_rule_key(match, value), set()).add(decision)
    blocked_templates: set[tuple[RuleMatch, str]] = set()
    for key, entries in requested.items():
        if key[0] is not RuleMatch.PATH_GLOB:
            continue
        decisions = {entry[2] for entry in entries}
        decisions.update(existing_decisions.get(key, set()))
        template = entries[-1][0]
        folded_template = os.path.normpath(expand_path(template)).casefold()
        compared_exact = [
            *existing_exact,
            *((entry[1], entry[0]) for entry in incoming.values()),
        ]
        decisions.update(
            exact_decision
            for exact_decision, exact_path in compared_exact
            if fnmatch.fnmatchcase(
                normalise_path(exact_path).casefold(),
                folded_template,
            )
        )
        if len(decisions) > 1:
            blocked_templates.add(key)
            removal_keys.add(key)

    delete = _without_source_rule_keys(rules.delete.rules, frozenset(removal_keys), source)
    keep = _without_source_rule_keys(rules.keep.rules, frozenset(removal_keys), source)
    used_ids = {rule.rule_id for rule in (*delete, *keep)}
    now = datetime.now(UTC).isoformat()
    for key, entries in requested.items():
        if key in blocked_templates:
            continue
        value, match, decision, reason = entries[-1]
        rule_id = _unique_rule_id(_decision_rule_id(source, f"{match.value}:{value}"), used_ids)
        used_ids.add(rule_id)
        reusable = match is RuleMatch.PATH_GLOB
        entry = DecisionRule(
            rule_id=rule_id,
            group=group,
            match=match,
            value=value,
            source=source,
            reason=(("同类动态路径模板；" if reusable else "") + reason)[:MAX_REASON_CHARS],
            updated_at=now,
        )
        (delete if decision is RuleDecision.DELETE else keep).append(entry)
    delete, keep = _bound_ai_decisions(delete, keep)
    updated = UserRules(
        scan=rules.scan,
        delete=replace(rules.delete, rules=_bounded_rules(delete)),
        keep=replace(rules.keep, rules=_bounded_rules(keep)),
    )
    save_rules(updated)
    return updated


_PORTABLE_PATH_VARIABLES: Final = (
    "TEMP",
    "LOCALAPPDATA",
    "APPDATA",
    "USERPROFILE",
    "PROGRAMDATA",
    "SYSTEMROOT",
)
_DATE_SEGMENT = re.compile(r"^(?:19|20)\d{2}$")
_MONTH_SEGMENT = re.compile(r"^(?:0[1-9]|1[0-2])$")
_DAY_SEGMENT = re.compile(r"^(?:0[1-9]|[12]\d|3[01])$")
_VOLATILE_TEXT = re.compile(
    r"(?:"
    r"(?:19|20)\d{2}[-_.]\d{2}[-_.]\d{2}"
    r"(?:[T_ -]\d{2}(?:[-_.:]\d{2}){1,3}(?:[-_.]\d+)?)?"
    r"|(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])"
    r"(?:[T_-]?\d{4,})?"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|(?<=[._-])1[6-9](?:\d{8}|\d{11})(?=$|[._-])"
    r"|(?<![0-9A-Za-z])[0-9a-f]{16,}(?![0-9A-Za-z])"
    r"|(?<![0-9A-Za-z])\d{7,}(?![0-9A-Za-z])"
    r")",
    re.IGNORECASE,
)
_GENERIC_TEMPLATE_SEGMENTS: Final = frozenset({"temp", "tmp", "cache", "caches", "log", "logs"})
_CONTEXT_DEPENDENT_DECISION_REASON = re.compile(
    r"(?:"
    r"(?:超过|大于|至少)\s*\d+\s*天"
    r"|(?:older\s+than|at\s+least)\s+\d+\s+days?"
    r"|长期未"
    r"|旧版本"
    r"|被.{0,12}(?:更新|版本).{0,12}取代"
    r"|今天|今日|当天|昨天|明天"
    r"|正在使用|仍在使用|当前版本|最新版本"
    r"|\btoday\b|\byesterday\b|\btomorrow\b"
    r"|\bcurrently?\s+(?:active|used|in use)\b"
    r"|\bcurrent\s+version\b|\blatest\s+version\b"
    r")",
    re.IGNORECASE,
)


def _decision_rule_shapes(
    path: str,
    *,
    decision: RuleDecision | None = None,
    reason: str = "",
) -> tuple[tuple[RuleMatch, str], ...]:
    portable = _portable_path_value(path)
    exact = portable or path
    shapes: list[tuple[RuleMatch, str]] = [(RuleMatch.EXACT_PATH, exact)]
    if decision is not None and _CONTEXT_DEPENDENT_DECISION_REASON.search(reason):
        return tuple(shapes)
    template = _volatile_path_glob(exact)
    if template is not None:
        shapes.append((RuleMatch.PATH_GLOB, template))
    return tuple(shapes)


def reusable_path_pattern(path: str | Path) -> str | None:
    """Return the conservative generated-name pattern used for AI reuse."""

    normalized = normalise_path(path)
    portable = _portable_path_value(normalized)
    return _volatile_path_glob(portable or normalized)


def _portable_path_value(path: str) -> str | None:
    normalized = normalise_path(path)
    candidates: list[tuple[int, str, str, str]] = []
    seen_roots: set[str] = set()

    # A learned rules file may be copied from Alice's computer to Bob's.  Match
    # the conventional Windows profile layout independently of the environment
    # of the computer doing the conversion.
    profile_match = re.match(
        r"^(?P<drive>[A-Za-z]:)\\Users\\(?P<username>[^\\]+)",
        normalized,
        re.IGNORECASE,
    )
    inferred_username = ""
    if profile_match is not None:
        inferred_username = profile_match.group("username")
        profile_root = profile_match.group(0)
        inferred_roots = (
            (
                profile_root + r"\AppData\Local\Temp",
                r"%LOCALAPPDATA%\Temp",
            ),
            (
                profile_root + r"\AppData\Local",
                "%LOCALAPPDATA%",
            ),
            (
                profile_root + r"\AppData\Roaming",
                "%APPDATA%",
            ),
            (profile_root, "%USERPROFILE%"),
        )
        for root, variable in inferred_roots:
            folded = root.casefold()
            if normalized.casefold() == folded:
                candidates.append((len(root), variable, "", inferred_username))
            elif normalized.casefold().startswith(folded.rstrip(os.sep) + os.sep):
                candidates.append(
                    (
                        len(root),
                        variable,
                        normalized[len(root) :],
                        inferred_username,
                    )
                )

    for name in _PORTABLE_PATH_VARIABLES:
        raw = os.environ.get(name)
        if not raw:
            continue
        root = normalise_path(raw)
        folded = root.casefold()
        if folded in seen_roots:
            continue
        seen_roots.add(folded)
        if normalized.casefold() == folded:
            candidates.append((len(root), f"%{name}%", "", inferred_username))
        elif normalized.casefold().startswith(folded.rstrip(os.sep) + os.sep):
            candidates.append(
                (
                    len(root),
                    f"%{name}%",
                    normalized[len(root) :],
                    inferred_username,
                )
            )
    if not candidates:
        return None
    _length, variable, tail, source_username = max(candidates, key=lambda item: item[0])
    username = source_username or os.environ.get("USERNAME")
    if not username:
        profile = os.environ.get("USERPROFILE")
        username = Path(profile).name if profile else ""
    if username:
        tail = re.sub(
            rf"(?<![0-9A-Za-z]){re.escape(username)}(?![0-9A-Za-z])",
            "%USERNAME%",
            tail,
            flags=re.IGNORECASE,
        )
    return variable + tail


def _volatile_path_glob(portable_path: str) -> str | None:
    drive, tail = os.path.splitdrive(portable_path)
    prefix = drive
    if portable_path.startswith("%"):
        end = portable_path.find("%", 1)
        if end > 0:
            prefix = portable_path[: end + 1]
            tail = portable_path[end + 1 :]
    parts = [part for part in re.split(r"[\\/]+", tail) if part]
    rendered: list[str] = []
    volatile = False
    anchored_volatile = False
    index = 0
    while index < len(parts):
        if (
            index + 2 < len(parts)
            and _DATE_SEGMENT.fullmatch(parts[index])
            and _MONTH_SEGMENT.fullmatch(parts[index + 1])
            and _DAY_SEGMENT.fullmatch(parts[index + 2])
        ):
            rendered.extend(("*", "*", "*"))
            volatile = True
            index += 3
            continue
        part = parts[index]
        match = _volatile_segment_match(part)
        if match is None:
            rendered.append(glob.escape(part))
        else:
            before = glob.escape(part[: match.start()])
            # Preserve only a real suffix that starts after the volatile token.
            # pathlib treats ``urls.store.4_13429459072546848`` as having the
            # suffix ``.4_134...``; appending that after ``*`` duplicated the
            # very timestamp we meant to abstract.
            suffix_start = part.rfind(".")
            after = glob.escape(part[suffix_start:]) if suffix_start >= match.end() else ""
            rendered.append(before + "*" + after)
            volatile = True
            anchored_volatile = anchored_volatile or bool(before or after)
        index += 1
    if not volatile:
        return None
    stable = [
        part.casefold()
        for part in parts
        if _volatile_segment_match(part) is None
        and not _DATE_SEGMENT.fullmatch(part)
        and part.casefold() not in _GENERIC_TEMPLATE_SEGMENTS
    ]
    if not anchored_volatile and len(stable) < 2:
        return None
    separator = os.sep
    return prefix + separator + separator.join(rendered)


def _volatile_segment_match(part: str) -> re.Match[str] | None:
    matches = [match for match in (_VOLATILE_TEXT.search(part),) if match]
    profile = os.environ.get("USERPROFILE")
    if profile:
        username = Path(profile).name
        if username:
            username_match = re.search(
                rf"(?<![0-9A-Za-z]){re.escape(username)}(?![0-9A-Za-z])",
                part,
                re.IGNORECASE,
            )
            if username_match is not None:
                matches.append(username_match)
    return min(matches, key=lambda match: match.start(), default=None)


def clear_ai_rules(rules: UserRules) -> UserRules:
    """Remove only AI-created decisions, leaving hand-edited rules intact."""

    updated = UserRules(
        scan=rules.scan,
        delete=replace(
            rules.delete,
            rules=tuple(rule for rule in rules.delete.rules if rule.source != "AI_IMPORT"),
        ),
        keep=replace(
            rules.keep,
            rules=tuple(rule for rule in rules.keep.rules if rule.source != "AI_IMPORT"),
        ),
    )
    save_rules(updated)
    return updated


def expand_path(value: str, environment: dict[str, str] | None = None) -> str:
    """Expand Windows environment syntax and ``~`` without compatibility rules."""

    env = os.environ if environment is None else environment
    folded = {key.casefold(): item for key, item in env.items()}

    def replace_variable(match: re.Match[str]) -> str:
        return folded.get(match.group(1).casefold(), match.group(0))

    expanded = re.sub(r"%([^%]+)%", replace_variable, value)
    return os.path.expanduser(expanded)


def expanded_scan_paths(values: tuple[str, ...]) -> tuple[Path, ...]:
    paths: list[Path] = []
    for value in values:
        expanded = expand_path(value)
        if expanded and "%" not in expanded:
            paths.append(Path(expanded))
    return tuple(paths)


def expand_braces(pattern: str) -> tuple[str, ...]:
    """Expand simple ``{a,b}`` groups used by scan path templates."""

    start = pattern.find("{")
    if start < 0:
        if "}" in pattern:
            raise RuleConfigError(f"路径包含多余的右花括号：{pattern}")
        return (pattern,)
    end = pattern.find("}", start + 1)
    if end < 0:
        raise RuleConfigError(f"路径缺少右花括号：{pattern}")
    choices = pattern[start + 1 : end].split(",")
    if not choices or any(not choice for choice in choices):
        raise RuleConfigError(f"路径组合无效：{pattern}")
    expanded: list[str] = []
    for choice in choices:
        expanded.extend(expand_braces(pattern[:start] + choice + pattern[end + 1 :]))
    return tuple(expanded)


def normalise_path(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(path))))


def _packaged_documents() -> tuple[str, str, str]:
    package = resources.files("devclean.config")
    try:
        return tuple(package.joinpath(name).read_text(encoding="utf-8") for name in _CONFIG_NAMES)  # type: ignore[return-value]
    except (OSError, UnicodeError) as error:
        raise RuleConfigError(f"无法读取随程序提供的三份初始配置：{error}") from error


def _parse_document(text: str, name: str, allowed_keys: frozenset[str]) -> dict[str, object]:
    try:
        payload = strict_json_loads(text)
    except (UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise RuleConfigError(f"{name} 不是有效 JSON：{error}") from error
    if not isinstance(payload, dict):
        raise RuleConfigError(f"{name} 顶层必须是对象")
    _require_exact_keys(payload, allowed_keys, f"{name} 顶层")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise RuleConfigError(f"{name} 只支持 schema_version={SCHEMA_VERSION}")
    if payload.get("document_type") != _DOCUMENT_TYPES[name]:
        raise RuleConfigError(f"{name} 的 document_type 必须是 {_DOCUMENT_TYPES[name]}")
    return payload


def _parse_scan_rules(payload: dict[str, object]) -> ScanRules:
    raw_roots = payload.get("known_cleanup_roots")
    if not isinstance(raw_roots, list):
        raise RuleConfigError("scan-rules.json 的 known_cleanup_roots 必须是数组")
    roots: list[KnownRootRule] = []
    root_ids: set[str] = set()
    for index, raw in enumerate(raw_roots, start=1):
        if not isinstance(raw, dict):
            raise RuleConfigError(f"known_cleanup_roots 第 {index} 项必须是对象")
        _require_exact_keys(raw, _KNOWN_ROOT_KEYS, f"known_cleanup_roots 第 {index} 项")
        category = _required_string(raw.get("category"), f"已知目录 {index} 的 category")
        policy = _required_string(raw.get("policy"), f"已知目录 {index} 的 policy")
        try:
            CleanupCategory(category)
            CleanupPolicy(policy)
        except ValueError as error:
            raise RuleConfigError(
                f"known_cleanup_roots 第 {index} 项使用了未知 category 或 policy"
            ) from error
        patterns = _strings(raw.get("patterns"), f"已知目录 {index} 的 patterns")
        if not patterns:
            raise RuleConfigError(f"known_cleanup_roots 第 {index} 项至少需要一个 pattern")
        for pattern in patterns:
            expand_braces(pattern)
        rule_id = _required_string(raw.get("id"), f"已知目录 {index} 的 id")
        if rule_id in root_ids:
            raise RuleConfigError(f"known_cleanup_roots 的 id 重复：{rule_id}")
        root_ids.add(rule_id)
        roots.append(
            KnownRootRule(
                rule_id=rule_id,
                group=_required_string(raw.get("group"), f"已知目录 {index} 的 group"),
                patterns=patterns,
                category=category,
                policy=policy,
                label=_required_string(raw.get("label"), f"已知目录 {index} 的 label"),
                enabled=_bool(raw.get("enabled", True), "enabled"),
                allow_inside_system_anchor=_bool(
                    raw.get("allow_inside_system_anchor", False),
                    "allow_inside_system_anchor",
                ),
            )
        )
    delete_root_ids = frozenset(_strings(payload.get("delete_root_ids"), "delete_root_ids"))
    unknown_delete_roots = delete_root_ids - root_ids
    if unknown_delete_roots:
        raise RuleConfigError(
            "delete_root_ids 只能引用 known_cleanup_roots 中存在的 id："
            f"{sorted(unknown_delete_roots)}"
        )
    return ScanRules(
        metadata=_parse_metadata(payload, SCAN_RULES_NAME),
        include_user_profile=_bool(payload.get("include_user_profile"), "include_user_profile"),
        include_known_cleanup_roots=_bool(
            payload.get("include_known_cleanup_roots"),
            "include_known_cleanup_roots",
        ),
        review_sample_per_category=_positive_int(
            payload.get("review_sample_per_category"),
            "review_sample_per_category",
        ),
        additional_paths=_strings(payload.get("additional_paths"), "additional_paths"),
        excluded_paths=_strings(payload.get("excluded_paths"), "excluded_paths"),
        delete_root_ids=delete_root_ids,
        skip_directory_groups=_parse_string_groups(
            payload.get("skip_directory_groups"),
            "skip_directory_groups",
        ),
        known_cleanup_roots=tuple(roots),
    )


def _parse_delete_rules(payload: dict[str, object]) -> DeleteRules:
    thresholds = _object(payload.get("thresholds"), "thresholds")
    classification = _object(payload.get("classification"), "classification")
    _require_exact_keys(thresholds, _THRESHOLD_KEYS, "thresholds")
    _require_exact_keys(classification, _DELETE_CLASSIFICATION_KEYS, "delete classification")
    classification_groups = _parse_classification_groups(
        payload.get("classification_groups"),
        _DELETE_CLASSIFICATION_KEYS,
        "delete classification_groups",
    )
    match_help = _parse_match_help(payload.get("match_help"), DELETE_RULES_NAME)
    version_name = _required_string(classification.get("version_name_regex"), "version_name_regex")
    version_separators = _required_string(
        classification.get("version_separators_regex"),
        "version_separators_regex",
    )
    for label, pattern in (
        ("version_name_regex", version_name),
        ("version_separators_regex", version_separators),
    ):
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as error:
            raise RuleConfigError(f"{label} 是无效正则：{error}") from error
    config = DeleteClassification(
        old_temp_days=_nonnegative_int(thresholds.get("old_temp_days"), "old_temp_days"),
        large_file_bytes=_nonnegative_int(thresholds.get("large_file_bytes"), "large_file_bytes"),
        stale_metadata_days=_nonnegative_int(
            thresholds.get("stale_metadata_days"), "stale_metadata_days"
        ),
        development_cache_segments=_folded_set(classification, "development_cache_segments"),
        regenerable_tool_directories=_folded_set(classification, "regenerable_tool_directories"),
        byproduct_suffixes=_folded_set(classification, "byproduct_suffixes"),
        cache_directory_names=_folded_set(classification, "cache_directory_names"),
        byproduct_segments=_folded_set(classification, "byproduct_segments"),
        build_segments=_folded_set(classification, "build_segments"),
        ide_segments=_folded_set(classification, "ide_segments"),
        version_name_regex=version_name,
        version_separators_regex=version_separators,
        self_updater_parents=_folded_set(classification, "self_updater_parents"),
        cache_segments=_folded_set(classification, "cache_segments"),
        container_segments=_folded_set(classification, "container_segments"),
        container_suffixes=_folded_set(classification, "container_suffixes"),
        windows_update_segments=_folded_set(classification, "windows_update_segments"),
        windows_update_segment_groups=_folded_groups(
            classification, "windows_update_segment_groups"
        ),
        conda_segments=_folded_set(classification, "conda_segments"),
        downloads_segments=_folded_set(classification, "downloads_segments"),
        inferred_ai_review_categories=_category_set(
            classification, "inferred_ai_review_categories"
        ),
        inferred_report_only_categories=_category_set(
            classification, "inferred_report_only_categories"
        ),
        system_log_suffixes=_folded_set(classification, "system_log_suffixes"),
        installer_suffixes=_folded_set(classification, "installer_suffixes"),
        category_source_domains=_source_domain_map(classification.get("category_source_domains")),
    )
    return DeleteRules(
        metadata=_parse_metadata(payload, DELETE_RULES_NAME),
        classification=config,
        classification_groups=classification_groups,
        match_help=match_help,
        rules=_parse_decision_rules(payload, DELETE_RULES_NAME),
    )


def _parse_keep_rules(payload: dict[str, object]) -> KeepRules:
    classification = _object(payload.get("classification"), "classification")
    _require_exact_keys(classification, _KEEP_CLASSIFICATION_KEYS, "keep classification")
    classification_groups = _parse_classification_groups(
        payload.get("classification_groups"),
        _KEEP_CLASSIFICATION_KEYS,
        "keep classification_groups",
    )
    match_help = _parse_match_help(payload.get("match_help"), KEEP_RULES_NAME)
    config = KeepClassification(
        application_data_segments=_folded_set(classification, "application_data_segments"),
        protected_system_root_names=_strings(
            classification.get("protected_system_root_names"),
            "protected_system_root_names",
        ),
        protected_system_file_names=_folded_set(classification, "protected_system_file_names"),
        program_payload_suffixes=_folded_set(classification, "program_payload_suffixes"),
        application_state_suffixes=_folded_set(classification, "application_state_suffixes"),
        installed_payload_segments=_folded_set(classification, "installed_payload_segments"),
        application_state_names=_folded_set(classification, "application_state_names"),
        application_state_tails=tuple(
            item.casefold()
            for item in _strings(
                classification.get("application_state_tails"),
                "application_state_tails",
            )
        ),
    )
    return KeepRules(
        metadata=_parse_metadata(payload, KEEP_RULES_NAME),
        classification=config,
        classification_groups=classification_groups,
        match_help=match_help,
        rules=_parse_decision_rules(payload, KEEP_RULES_NAME),
    )


def _parse_decision_rules(payload: dict[str, object], name: str) -> tuple[DecisionRule, ...]:
    raw_rules = payload.get("rules")
    if not isinstance(raw_rules, list):
        raise RuleConfigError(f"{name} 的 rules 必须是数组")
    if len(raw_rules) > MAX_DECISION_RULES:
        raise RuleConfigError(f"{name} 最多允许 {MAX_DECISION_RULES:,} 条规则")
    parsed: list[DecisionRule] = []
    rule_ids: set[str] = set()
    for index, raw in enumerate(raw_rules, start=1):
        if not isinstance(raw, dict):
            raise RuleConfigError(f"{name} 第 {index} 条规则必须是对象")
        _require_exact_keys(raw, _DECISION_RULE_KEYS, f"{name} 第 {index} 条规则")
        try:
            match = RuleMatch(str(raw.get("match", "")))
        except ValueError as error:
            raise RuleConfigError(
                f"{name} 第 {index} 条 match 必须是 exact_path、path_prefix、"
                "path_glob、filename_glob、path_regex 或 filename_regex"
            ) from error
        value = _required_string(raw.get("value"), f"{name} 第 {index} 条 value")
        if match in {RuleMatch.PATH_REGEX, RuleMatch.FILENAME_REGEX}:
            try:
                re.compile(value, re.IGNORECASE)
            except re.error as error:
                raise RuleConfigError(f"{name} 第 {index} 条正则表达式无效：{error}") from error
        rule_id = _bounded_string(
            raw.get("id"),
            f"{name} 第 {index} 条规则 id",
            max_chars=128,
            allow_empty=False,
        )
        if rule_id in rule_ids:
            raise RuleConfigError(f"{name} 的规则 id 重复：{rule_id}")
        rule_ids.add(rule_id)
        parsed.append(
            DecisionRule(
                rule_id=rule_id,
                group=_required_string(raw.get("group"), f"{name} 第 {index} 条 group"),
                match=match,
                value=value,
                enabled=_bool(raw.get("enabled", True), "enabled"),
                source=_bounded_string(
                    raw.get("source"),
                    f"{name} 第 {index} 条规则 source",
                    max_chars=64,
                    allow_empty=False,
                ),
                reason=_bounded_string(
                    raw.get("reason"),
                    f"{name} 第 {index} 条规则 reason",
                    max_chars=MAX_REASON_CHARS,
                    allow_empty=True,
                ),
                updated_at=_bounded_string(
                    raw.get("updated_at"),
                    f"{name} 第 {index} 条规则 updated_at",
                    max_chars=64,
                    allow_empty=True,
                ),
            )
        )
    return tuple(parsed)


def _delete_classification_document(
    config: DeleteClassification,
) -> dict[str, object]:
    return {
        "development_cache_segments": sorted(config.development_cache_segments),
        "regenerable_tool_directories": sorted(config.regenerable_tool_directories),
        "byproduct_suffixes": sorted(config.byproduct_suffixes),
        "cache_directory_names": sorted(config.cache_directory_names),
        "byproduct_segments": sorted(config.byproduct_segments),
        "build_segments": sorted(config.build_segments),
        "ide_segments": sorted(config.ide_segments),
        "version_name_regex": config.version_name_regex,
        "version_separators_regex": config.version_separators_regex,
        "self_updater_parents": sorted(config.self_updater_parents),
        "cache_segments": sorted(config.cache_segments),
        "container_segments": sorted(config.container_segments),
        "container_suffixes": sorted(config.container_suffixes),
        "windows_update_segments": sorted(config.windows_update_segments),
        "windows_update_segment_groups": [
            sorted(group) for group in config.windows_update_segment_groups
        ],
        "conda_segments": sorted(config.conda_segments),
        "downloads_segments": sorted(config.downloads_segments),
        "inferred_ai_review_categories": sorted(config.inferred_ai_review_categories),
        "inferred_report_only_categories": sorted(config.inferred_report_only_categories),
        "system_log_suffixes": sorted(config.system_log_suffixes),
        "installer_suffixes": sorted(config.installer_suffixes),
        "category_source_domains": dict(config.category_source_domains),
    }


def _keep_classification_document(
    config: KeepClassification,
) -> dict[str, object]:
    return {
        "application_data_segments": sorted(config.application_data_segments),
        "protected_system_root_names": list(config.protected_system_root_names),
        "protected_system_file_names": sorted(config.protected_system_file_names),
        "program_payload_suffixes": sorted(config.program_payload_suffixes),
        "application_state_suffixes": sorted(config.application_state_suffixes),
        "installed_payload_segments": sorted(config.installed_payload_segments),
        "application_state_names": sorted(config.application_state_names),
        "application_state_tails": list(config.application_state_tails),
    }


def _decision_rule_documents(
    rules: tuple[DecisionRule, ...],
) -> list[dict[str, object]]:
    return [
        {
            "id": rule.rule_id,
            "group": rule.group,
            "enabled": rule.enabled,
            "match": rule.match.value,
            "value": rule.value,
            "source": rule.source,
            "reason": rule.reason,
            "updated_at": rule.updated_at,
        }
        for rule in rules
    ]


def _metadata_contract_document(
    metadata: RuleDocumentMetadata,
) -> dict[str, object]:
    return {
        "contract_version": metadata.contract_version,
        "output_format": metadata.output_format,
        "instructions": list(metadata.instructions),
    }


def _require_exact_keys(payload: dict[str, object], expected: frozenset[str], label: str) -> None:
    actual = set(payload)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    raise RuleConfigError(f"{label} 字段不符合统一格式；缺少={missing}，多余={extra}")


def _parse_metadata(payload: dict[str, object], name: str) -> RuleDocumentMetadata:
    help_text = _required_string(payload.get("_help"), f"{name} 的 _help")
    contract = _object(
        payload.get("_ai_editing_contract"),
        f"{name} 的 _ai_editing_contract",
    )
    _require_exact_keys(contract, _CONTRACT_KEYS, f"{name} 的 _ai_editing_contract")
    contract_version = contract.get("contract_version")
    if (
        not isinstance(contract_version, int)
        or isinstance(contract_version, bool)
        or contract_version != 1
    ):
        raise RuleConfigError(f"{name} 的 contract_version 必须是 1")
    if contract.get("output_format") != "FULL_JSON_OBJECT_ONLY":
        raise RuleConfigError(f"{name} 的 output_format 必须是 FULL_JSON_OBJECT_ONLY")
    instructions = _strings(contract.get("instructions"), f"{name} 的 instructions")
    if not instructions:
        raise RuleConfigError(f"{name} 的 instructions 不能为空")
    return RuleDocumentMetadata(
        help_text=help_text,
        contract_version=contract_version,
        output_format="FULL_JSON_OBJECT_ONLY",
        instructions=instructions,
    )


def _parse_string_groups(value: object, label: str) -> dict[str, tuple[str, ...]]:
    groups = _object(value, label)
    if not groups:
        raise RuleConfigError(f"{label} 不能为空")
    parsed: dict[str, tuple[str, ...]] = {}
    seen: set[str] = set()
    for group, raw_values in groups.items():
        group_name = _required_string(group, f"{label} 的分组名")
        values = _strings(raw_values, f"{label}.{group_name}")
        folded = {item.casefold() for item in values}
        if len(folded) != len(values):
            raise RuleConfigError(f"{label}.{group_name} 中不能包含重复值")
        duplicates = seen & folded
        if duplicates:
            raise RuleConfigError(f"{label} 的不同分组包含重复值：{sorted(duplicates)}")
        seen.update(folded)
        parsed[group_name] = values
    return parsed


def _parse_classification_groups(
    value: object, expected_fields: frozenset[str], label: str
) -> dict[str, tuple[str, ...]]:
    groups = _parse_string_groups(value, label)
    flattened = [field for fields in groups.values() for field in fields]
    if len(flattened) != len(set(flattened)):
        raise RuleConfigError(f"{label} 中同一字段不能出现两次")
    actual = set(flattened)
    if actual != expected_fields:
        raise RuleConfigError(
            f"{label} 必须完整且仅覆盖 classification 字段；"
            f"缺少={sorted(expected_fields - actual)}，"
            f"多余={sorted(actual - expected_fields)}"
        )
    return groups


def _parse_match_help(value: object, name: str) -> dict[str, str]:
    help_values = _object(value, f"{name} 的 match_help")
    _require_exact_keys(help_values, _MATCH_HELP_KEYS, f"{name} 的 match_help")
    return {
        match: _required_string(description, f"{name} 的 match_help.{match}")
        for match, description in help_values.items()
    }


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuleConfigError(f"{label} 必须是对象")
    return value


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RuleConfigError(f"{label} 必须是字符串数组")
    strings = tuple(str(item) for item in value)
    if any(not item for item in strings):
        raise RuleConfigError(f"{label} 中不能包含空字符串")
    if any(len(item) > MAX_RULE_VALUE_CHARS for item in strings):
        raise RuleConfigError(f"{label} 中的值过长")
    return strings


def _folded_set(payload: dict[str, object], label: str) -> frozenset[str]:
    return frozenset(item.casefold() for item in _strings(payload.get(label), label))


def _folded_groups(payload: dict[str, object], label: str) -> tuple[frozenset[str], ...]:
    value = payload.get(label)
    if not isinstance(value, list):
        raise RuleConfigError(f"{label} 必须是字符串数组的数组")
    groups: list[frozenset[str]] = []
    for index, group in enumerate(value, start=1):
        groups.append(
            frozenset(item.casefold() for item in _strings(group, f"{label} 第 {index} 组"))
        )
    return tuple(groups)


def _category_set(payload: dict[str, object], label: str) -> frozenset[str]:
    values = _strings(payload.get(label), label)
    parsed: set[str] = set()
    for value in values:
        try:
            parsed.add(CleanupCategory(value).value)
        except ValueError as error:
            raise RuleConfigError(f"{label} 包含未知 CleanupCategory：{value}") from error
    return frozenset(parsed)


def _source_domain_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise RuleConfigError("category_source_domains 必须是对象")
    expected = {category.value for category in CleanupCategory}
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        raise RuleConfigError(
            f"category_source_domains 必须完整覆盖 CleanupCategory；缺少={missing}，多余={extra}"
        )
    parsed: dict[str, str] = {}
    for category, domain in value.items():
        if not isinstance(domain, str):
            raise RuleConfigError(f"{category} 的 source domain 必须是字符串")
        try:
            parsed[str(category)] = SourceDomain(domain).value
        except ValueError as error:
            raise RuleConfigError(f"{category} 使用了未知 source domain") from error
    return parsed


def _bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise RuleConfigError(f"{label} 必须是 true 或 false")
    return value


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise RuleConfigError(f"{label} 必须是字符串")
    if not value:
        raise RuleConfigError(f"{label} 不能为空")
    if len(value) > MAX_RULE_VALUE_CHARS:
        raise RuleConfigError(f"{label} 过长")
    return value


def _bounded_string(
    value: object,
    label: str,
    *,
    max_chars: int,
    allow_empty: bool,
) -> str:
    if not isinstance(value, str):
        raise RuleConfigError(f"{label} 必须是字符串")
    if not allow_empty and not value:
        raise RuleConfigError(f"{label} 不能为空")
    if len(value) > max_chars:
        raise RuleConfigError(f"{label} 最长允许 {max_chars} 个字符")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuleConfigError(f"{label} 必须是非负整数")
    return value


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RuleConfigError(f"{label} 必须是正整数")
    return value


def _stored_rule_key(match: RuleMatch, value: str) -> tuple[RuleMatch, str]:
    if match in {
        RuleMatch.EXACT_PATH,
        RuleMatch.PATH_PREFIX,
        RuleMatch.PATH_GLOB,
    }:
        return (match, os.path.normpath(value).casefold())
    return (match, value.casefold())


def _without_source_rule_keys(
    rules: tuple[DecisionRule, ...],
    keys: frozenset[tuple[RuleMatch, str]],
    source: str,
) -> list[DecisionRule]:
    return [
        rule
        for rule in rules
        if not (rule.source == source and _stored_rule_key(rule.match, rule.value) in keys)
    ]


def _bounded_rules(rules: list[DecisionRule]) -> tuple[DecisionRule, ...]:
    if len(rules) <= MAX_DECISION_RULES:
        return tuple(rules)
    manual = [rule for rule in rules if rule.source != "AI_IMPORT"]
    imported = [rule for rule in rules if rule.source == "AI_IMPORT"]
    available = max(0, MAX_DECISION_RULES - len(manual))
    retained_imported = imported[-available:] if available else []
    return tuple(manual[-MAX_DECISION_RULES:] + retained_imported)


def _bound_ai_decisions(
    delete: list[DecisionRule], keep: list[DecisionRule]
) -> tuple[list[DecisionRule], list[DecisionRule]]:
    tagged = [
        (group, index, rule)
        for group, rules in (("delete", delete), ("keep", keep))
        for index, rule in enumerate(rules)
        if rule.source == "AI_IMPORT"
    ]
    if len(tagged) <= MAX_DECISION_RULES:
        return delete, keep
    tagged.sort(key=lambda item: item[2].updated_at)
    remove = {(group, index) for group, index, _rule in tagged[: len(tagged) - MAX_DECISION_RULES]}
    return (
        [rule for index, rule in enumerate(delete) if ("delete", index) not in remove],
        [rule for index, rule in enumerate(keep) if ("keep", index) not in remove],
    )


def _decision_rule_id(source: str, path: str) -> str:
    prefix = "ai" if source == "AI_IMPORT" else "user"
    return prefix + "_" + hashlib.sha256(path.casefold().encode("utf-8")).hexdigest()[:24]


def _unique_rule_id(base: str, used: set[str]) -> str:
    if base not in used:
        return base
    counter = 2
    while f"{base}_{counter}" in used:
        counter += 1
    return f"{base}_{counter}"


def _render(document: dict[str, object]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def _atomic_write(path: Path, text: str) -> None:
    scratch = path.with_suffix(path.suffix + ".writing")
    scratch.write_text(text, encoding="utf-8", newline="\n")
    os.replace(scratch, path)


def _ensure_default_backup(packaged: tuple[str, str, str]) -> None:
    target = default_backup_path()
    try:
        with zipfile.ZipFile(target, "r") as archive:
            if archive.namelist() == list(_CONFIG_NAMES) and all(
                archive.read(name).decode("utf-8") == text
                for name, text in zip(_CONFIG_NAMES, packaged, strict=True)
            ):
                return
    except (OSError, UnicodeError, ValueError, zipfile.BadZipFile, KeyError):
        pass

    scratch = target.with_suffix(target.suffix + ".writing")
    with zipfile.ZipFile(scratch, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, text in zip(_CONFIG_NAMES, packaged, strict=True):
            archive.writestr(name, text.encode("utf-8"))
    os.replace(scratch, target)


def _default_backup_documents() -> tuple[str, str, str]:
    target = default_backup_path()
    try:
        with zipfile.ZipFile(target, "r") as archive:
            if archive.namelist() != list(_CONFIG_NAMES):
                raise RuleConfigError(f"默认备份内容不完整：{target}")
            return tuple(archive.read(name).decode("utf-8") for name in _CONFIG_NAMES)  # type: ignore[return-value]
    except RuleConfigError:
        raise
    except (
        OSError,
        UnicodeError,
        ValueError,
        zipfile.BadZipFile,
        KeyError,
    ) as error:
        raise RuleConfigError(f"无法读取同目录默认备份：{target}：{error}") from error


__all__ = [
    "DEFAULT_BACKUP_NAME",
    "DELETE_RULES_NAME",
    "KEEP_RULES_NAME",
    "MAX_DECISION_RULES",
    "SCAN_RULES_NAME",
    "DeleteClassification",
    "DeleteRules",
    "KeepClassification",
    "KeepRules",
    "KnownRootRule",
    "RuleConfigError",
    "RuleDecision",
    "RuleDocumentMetadata",
    "RuleMatch",
    "ScanRules",
    "UserRules",
    "add_ai_verdicts",
    "add_user_verdicts",
    "clear_ai_rules",
    "default_backup_path",
    "default_rules",
    "delete_rules_path",
    "expand_braces",
    "expand_path",
    "expanded_scan_paths",
    "keep_rules_path",
    "load_rules",
    "normalise_path",
    "parse_rule_documents",
    "read_rule_documents",
    "render_rule_documents",
    "restore_default_rules",
    "reusable_path_pattern",
    "rules_dir",
    "save_rules",
    "scan_rules_path",
]
