from pathlib import Path

path = Path("scripts/temporary_generic_review_routing_patch.py")
text = path.read_text(encoding="utf-8")
old = 'tracker.write_text(\n    """# Full rule re-audit tracker'
new = 'tracker.write_text(\n    "# Full rule re-audit tracker'
if old not in text:
    raise RuntimeError("expected tracker string typo not found")
path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")
