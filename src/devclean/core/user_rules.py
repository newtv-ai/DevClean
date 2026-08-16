"""Semantic guard around the stable rule-document implementation.

The parser and persistence engine live in ``_user_rules_impl`` unchanged. This
module adds one application-aware boundary: AI verdicts may not decide data that
an audited application profile marks USER or KEEP, and a user's decision about
one history item may not become a reusable template for all history items.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

from devclean.core import _user_rules_impl as _impl
from devclean.core._user_rules_impl import (
    DEFAULT_BACKUP_NAME,
    DELETE_RULES_NAME,
    KEEP_RULES_NAME,
    MAX_DECISION_RULES,
    SCAN_RULES_NAME,
    DecisionRule,
    DeleteClassification,
    DeleteRules,
    KeepClassification,
    KeepRules,
    KnownRootRule,
    RuleConfigError,
    RuleDecision,
    RuleDocumentMetadata,
    RuleMatch,
    ScanRules,
    UserRules,
    clear_ai_rules,
    default_backup_path,
    default_rules,
    delete_rules_path,
    expand_braces,
    expand_path,
    expanded_scan_paths,
    keep_rules_path,
    normalise_path,
    parse_rule_documents,
    read_rule_documents,
    render_rule_documents,
    restore_default_rules,
    reusable_path_pattern,
    rules_dir,
    save_rules,
    scan_rules_path,
)
from devclean.core.application_cleanup import DecisionOwner, match_application_rule

_ORIGINAL_ADD_AI_VERDICTS = _impl.add_ai_verdicts
_ORIGINAL_ADD_USER_VERDICTS = _impl.add_user_verdicts
_ORIGINAL_LOAD_RULES = _impl.load_rules


def _owner_for_path(path: str | Path) -> DecisionOwner | None:
    rule = match_application_rule(path)
    return None if rule is None else rule.owner


def _owner_for_stored_rule(rule: DecisionRule) -> DecisionOwner | None:
    if rule.match not in {
        RuleMatch.EXACT_PATH,
        RuleMatch.PATH_PREFIX,
        RuleMatch.PATH_GLOB,
        RuleMatch.PATH_REGEX,
    }:
        return None
    try:
        owner = _owner_for_path(expand_path(rule.value))
    except (OSError, ValueError):
        owner = None
    if owner is not None:
        return owner

    # Fallback for portable Codex rules when the original profile/CODEX_HOME is
    # no longer the active environment. This is deliberately narrow.
    value = rule.value.replace("/", "\\").casefold()
    user_markers = (
        r"\.codex\sessions",
        r"\.codex\archived_sessions",
        r"\.codex\history.jsonl",
        r"%codex_home%\sessions",
        r"%codex_home%\archived_sessions",
        r"%codex_home%\history.jsonl",
    )
    if any(marker in value for marker in user_markers):
        return DecisionOwner.USER
    keep_markers = (
        r"\.codex\plugins\cache",
        r"\.codex\plugins\data",
        r"\.codex\auth.json",
        r"\.codex\config.toml",
        r"\.codex\session_index.jsonl",
        r"%codex_home%\plugins\cache",
        r"%codex_home%\plugins\data",
        r"%codex_home%\auth.json",
        r"%codex_home%\config.toml",
        r"%codex_home%\session_index.jsonl",
    )
    if any(marker in value for marker in keep_markers):
        return DecisionOwner.KEEP
    if any(
        name in value
        for name in (
            "state_5.sqlite",
            "goals_1.sqlite",
            "memories_1.sqlite",
            "queue_1.sqlite",
            "thread_history_1.sqlite",
        )
    ) and ("\\.codex\\" in value or "%codex_home%" in value):
        return DecisionOwner.KEEP
    return None


def _remove_semantically_invalid_rules(
    rules: UserRules,
) -> tuple[UserRules, bool]:
    def keep_rule(rule: DecisionRule, decision: RuleDecision) -> bool:
        owner = _owner_for_stored_rule(rule)
        if owner not in {DecisionOwner.USER, DecisionOwner.KEEP}:
            return True
        if rule.source == "AI_IMPORT":
            return False
        if rule.source != "USER_DECISION":
            return True
        if owner is DecisionOwner.KEEP and decision is RuleDecision.DELETE:
            return False
        return rule.match is not RuleMatch.PATH_GLOB

    delete_rules = tuple(
        rule
        for rule in rules.delete.rules
        if keep_rule(rule, RuleDecision.DELETE)
    )
    keep_rules = tuple(
        rule
        for rule in rules.keep.rules
        if keep_rule(rule, RuleDecision.KEEP)
    )
    if delete_rules == rules.delete.rules and keep_rules == rules.keep.rules:
        return rules, False
    return (
        UserRules(
            scan=rules.scan,
            delete=replace(rules.delete, rules=delete_rules),
            keep=replace(rules.keep, rules=keep_rules),
        ),
        True,
    )


def _persist_if_sanitized(rules: UserRules) -> UserRules:
    sanitized, changed = _remove_semantically_invalid_rules(rules)
    if changed:
        _impl.save_rules(sanitized)
    return sanitized


def add_ai_verdicts(
    rules: UserRules,
    verdicts: list[tuple[str, RuleDecision, str]],
) -> UserRules:
    """Persist AI verdicts only for data the application profile delegates."""

    allowed = [
        verdict
        for verdict in verdicts
        if _owner_for_path(verdict[0]) not in {DecisionOwner.USER, DecisionOwner.KEEP}
    ]
    updated = _ORIGINAL_ADD_AI_VERDICTS(rules, allowed) if allowed else rules
    return _persist_if_sanitized(updated)


def add_user_verdicts(
    rules: UserRules,
    verdicts: list[tuple[str, RuleDecision, str]],
) -> UserRules:
    """Persist user choices without generalizing application-owned history."""

    allowed = [
        verdict
        for verdict in verdicts
        if not (
            verdict[1] is RuleDecision.DELETE
            and _owner_for_path(verdict[0]) is DecisionOwner.KEEP
        )
    ]
    updated = _ORIGINAL_ADD_USER_VERDICTS(rules, allowed) if allowed else rules
    return _persist_if_sanitized(updated)


def load_rules(*, create_missing: bool = True) -> UserRules:
    """Load rules and migrate away stale AI authority over application data."""

    return _persist_if_sanitized(
        _ORIGINAL_LOAD_RULES(create_missing=create_missing)
    )


# Keep runtime introspection/monkeypatch behaviour identical to the original
# module: callers receive the implementation module, with only the guarded
# public entry points replaced. This also keeps its private test hooks working.
_impl.add_ai_verdicts = add_ai_verdicts
_impl.add_user_verdicts = add_user_verdicts
_impl.load_rules = load_rules
sys.modules[__name__] = _impl


__all__ = [
    "DEFAULT_BACKUP_NAME",
    "DELETE_RULES_NAME",
    "DecisionRule",
    "DeleteClassification",
    "DeleteRules",
    "KEEP_RULES_NAME",
    "KeepClassification",
    "KeepRules",
    "KnownRootRule",
    "MAX_DECISION_RULES",
    "RuleConfigError",
    "RuleDecision",
    "RuleDocumentMetadata",
    "RuleMatch",
    "SCAN_RULES_NAME",
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
