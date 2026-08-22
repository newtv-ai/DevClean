from pathlib import Path

from devclean.ui.modern_app import ordered_drive_roots


def test_drive_roots_are_presented_from_c_forward() -> None:
    roots = tuple(Path(f"{letter}:/") for letter in "GFCED")

    ordered = ordered_drive_roots(roots)

    assert tuple(path.drive.upper() for path in ordered) == (
        "C:",
        "D:",
        "E:",
        "F:",
        "G:",
    )
