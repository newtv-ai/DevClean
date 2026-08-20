from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"expected patch anchor missing in {path}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")


# REPORT_ONLY observations are intentionally not retained by TriageSession.  A
# confirmed file-level DELETE must therefore be materialized before session.add,
# otherwise the item disappears before the partitioner can apply the rule.
replace_once(
    "src/devclean/ui/app.py",
    '''                    session.add(\n                        triage_file(\n                            record,\n                            known_roots=active_known_roots,\n                            delete_config=active_rules.delete.classification,\n                            keep_config=active_rules.keep.classification,\n                            now=now,\n                        )\n                    )\n''',
    '''                    file_item = triage_file(\n                        record,\n                        known_roots=active_known_roots,\n                        delete_config=active_rules.delete.classification,\n                        keep_config=active_rules.keep.classification,\n                        now=now,\n                    )\n                    session.add(_effective_deletable_item(file_item, active_rules))\n''',
)

# The unit-level partition test must model the same retention boundary as the
# real scanner.  The previous version added a REPORT_ONLY item directly, which
# TriageSession correctly discards before partitioning.
replace_once(
    "tests/test_generic_review_routing.py",
    '''    session = TriageSession(review_sample_per_category=rules.scan.review_sample_per_category)\n    session.observe_path(item.path, rules)\n    session.add(item)\n\n    deletable, unsure = app._partition_items(session, rules)\n    assert [candidate.path for candidate in deletable] == [item.path]\n''',
    '''    session = TriageSession(review_sample_per_category=rules.scan.review_sample_per_category)\n    session.observe_path(item.path, rules)\n    session.add(app._effective_deletable_item(item, rules))\n\n    deletable, unsure = app._partition_items(session, rules)\n    assert [candidate.path for candidate in deletable] == [item.path]\n''',
)

# PDB files are debug symbols, not hard runtime payloads in the packaged keep
# classifier.  Use a DLL to prove that learned DELETE cannot beat a real hard
# program-payload protection.
replace_once(
    "tests/test_generic_review_routing.py",
    '    path = tmp_path / "cache" / "runtime.pdb"\n',
    '    path = tmp_path / "cache" / "runtime.dll"\n',
)
