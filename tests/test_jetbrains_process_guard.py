from __future__ import annotations

import re

from devclean.core.jetbrains_cleanup import _PROCESS_NAME_REGEX


def test_jetbrains_ide_process_guard_does_not_treat_toolbox_as_an_ide() -> None:
    for name in (
        "idea64.exe",
        "pycharm64.exe",
        "webstorm64.exe",
        "rider64.exe",
        "mps64.exe",
    ):
        assert re.fullmatch(_PROCESS_NAME_REGEX, name) is not None

    assert re.fullmatch(_PROCESS_NAME_REGEX, "jetbrains-toolbox.exe") is None
    assert re.fullmatch(_PROCESS_NAME_REGEX, "toolbox.exe") is None
    assert re.fullmatch(_PROCESS_NAME_REGEX, "studio64.exe") is None
