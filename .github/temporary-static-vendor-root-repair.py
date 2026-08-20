from pathlib import Path

path = Path("tests/test_cleanup_execution.py")
text = path.read_text(encoding="utf-8")
old = '''from devclean.core.postscan_cleanup import (\n    CleanupExecutionProgress,\n    ScanCleanupCandidate,\n'''
new = '''from devclean.core.postscan_cleanup import (\n    CleanupExecutionProgress,\n    CleanupRefusal,\n    ScanCleanupCandidate,\n'''
if old not in text:
    raise RuntimeError("cleanup execution import anchor missing")
path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")
