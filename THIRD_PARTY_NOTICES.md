# Third-party notices

DevClean does not vendor third-party cleaner rules, cleaner binaries, or cleaner
source code. Runtime dependencies for the Python wheel are empty; development and
build dependencies are declared in `pyproject.toml` and locked in `uv.lock`.

## Windows executable

The end-user `DevClean.exe` is built with PyInstaller and contains a CPython
runtime and the Tcl/Tk components used by Tkinter. These components remain under
their respective licenses; building one executable does not remove the obligation
to provide their notices.

`scripts/build_windows_exe.ps1` resolves the texts from the exact locked build
environment and creates this release payload:

```text
artifacts/windows-exe/dist/
├── DevClean.exe
└── licenses/
    ├── DevClean-GPL-3.0.txt
    ├── THIRD_PARTY_NOTICES.md
    ├── CPython-LICENSE.txt
    ├── Tcl-Tk-license.terms
    └── PyInstaller-COPYING.txt
```

Anyone distributing the Windows executable must distribute the complete
`licenses` directory beside it. The build fails closed if any required license
text cannot be found.

## Optional companion boundary

BleachBit is an optional, separately installed companion. DevClean does not
distribute or invoke BleachBit cleaning in v0.x. Winapp2.ini is not included.
