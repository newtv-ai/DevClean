# Windows EXE bundled-runtime license re-audit — 2026-08

## Finding

The initial release review treated `release/licenses/Tcl-Tk-license.terms` as if it were a combined Tcl and Tk notice. It is not: the build copies that file from the installed `tcl\tk8.6\license.terms` path, so the standalone file is the Tk notice only.

That naming error did **not** leave Tcl unlicensed. DevClean already ships the installed CPython Windows `LICENSE.txt` as `CPython-LICENSE.txt`. The checked-in aggregate notice contains two consecutive but distinct Tcl/Tk license blocks: the Tcl block contains DFARs clause `252.227-7014`, while the Tk block contains `252.227-7013`.

CPython's Windows build treats Tcl and Tk as bundled `_tkinter` dependencies, and its binary license aggregation includes their third-party notices. The release therefore already carried the Tcl terms through `CPython-LICENSE.txt`; the defect was the misleading name of the extra Tk-only sidecar, not a missing Tcl license.

## Correction

- keep `CPython-LICENSE.txt` as the authoritative aggregate CPython/Tcl/Tk notice;
- rename the extra Tk-only sidecar from `Tcl-Tk-license.terms` to `Tk-license.terms`;
- make the locked Windows build emit and allowlist that accurate filename;
- keep the accepted checked-in `release/DevClean.exe` unchanged because the correction affects only adjacent notice packaging, not executable bytes;
- require normal exact-head CI to rebuild the Windows artifact from source before merge.

## Release boundary

This follow-up intentionally does not auto-promote a newly built checked-in EXE. DevClean's documented finished-product acceptance remains a human desktop step for the UAC-enabled executable; CI independently verifies lock/dependencies, lint, strict typing, the full current workflow and a fresh Windows EXE artifact.

The public release license directory after this correction contains:

- `DevClean-GPL-3.0.txt`;
- `CPython-LICENSE.txt` (including bundled Tcl and Tk notices);
- `PyInstaller-COPYING.txt`;
- `Tk-license.terms` (a redundant but accurately named direct Tk notice).

## Revisit trigger

Re-audit if the locked CPython packaging layout stops including Tcl/Tk terms in the installed Windows `LICENSE.txt`, if Tkinter is removed/replaced, or if additional runtime dependencies become embedded in `DevClean.exe`.
