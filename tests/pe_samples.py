"""Build small, structurally valid Windows PE files for tests.

The files follow the PE/COFF layout (DOS header, PE32+ headers, section table,
a resource tree holding a VS_VERSIONINFO block) and may carry data appended
after the image, which is where NSIS and 7-Zip installers keep their payload.
They are never executed.
"""

from __future__ import annotations

import struct
from collections.abc import Mapping

_FILE_ALIGNMENT = 0x200
_SECTION_ALIGNMENT = 0x1000
_HEADERS_SIZE = 0x400


def _align(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


def _version_node(key: str, value: bytes = b"", children: bytes = b"", text: bool = False) -> bytes:
    key_bytes = key.encode("utf-16-le") + b"\0\0"
    head = 6 + len(key_bytes)
    body = key_bytes + b"\0" * ((-head) % 4) + value
    body += b"\0" * ((-(6 + len(body))) % 4) + children
    value_length = len(value) // 2 if text else len(value)
    return struct.pack("<HHH", 6 + len(body), value_length, 1 if text else 0) + body


def version_info(strings: Mapping[str, str]) -> bytes:
    """A VS_VERSIONINFO block with one string table."""

    fixed = struct.pack("<13I", 0xFEEF04BD, 0x10000, *([0] * 11))
    entries = b"".join(
        _version_node(key, value.encode("utf-16-le") + b"\0\0", text=True)
        for key, value in strings.items()
    )
    table = _version_node("040904B0", children=entries, text=True)
    string_info = _version_node("StringFileInfo", children=table, text=True)
    return _version_node("VS_VERSION_INFO", fixed, string_info)


def _resource_section(rva: int, version: bytes) -> bytes:
    def directory(entry_id: int, target: int) -> bytes:
        return struct.pack("<IIHHHH", 0, 0, 0, 0, 0, 1) + struct.pack("<II", entry_id, target)

    tree = directory(16, 0x80000000 | 0x18)  # RT_VERSION
    tree += directory(1, 0x80000000 | 0x30)  # resource name 1
    tree += directory(0x409, 0x48)  # language: points at the data entry
    tree += struct.pack("<IIII", rva + 0x58, len(version), 0, 0)
    return tree + version


def build_pe(
    *,
    version: Mapping[str, str] | None = None,
    overlay: bytes = b"",
    code: bytes = b"\xc3",
    extra_sections: tuple[bytes, ...] = (),
    signature: bytes = b"",
) -> bytes:
    """Return the bytes of a minimal PE32+ executable.

    *code* fills ``.text``; *overlay* is appended after the image; *signature*
    is appended last and registered as the Authenticode certificate table.
    """

    sections: list[tuple[bytes, bytes]] = [(b".text", code)]
    if version is not None:
        sections.append((b".rsrc", b""))
    sections.extend((name, b"\0" * 16) for name in extra_sections)

    rva = _SECTION_ALIGNMENT
    raw = _HEADERS_SIZE
    table = b""
    body = b""
    resource = (0, 0)
    for name, data in sections:
        if name == b".rsrc":
            assert version is not None
            data = _resource_section(rva, version_info(version))
            resource = (rva, len(data))
        raw_size = _align(len(data), _FILE_ALIGNMENT)
        table += struct.pack(
            "<8sIIIIIIHHI", name, len(data), rva, raw_size, raw, 0, 0, 0, 0, 0x40000040
        )
        body += data.ljust(raw_size, b"\0")
        rva += _align(max(len(data), 1), _SECTION_ALIGNMENT)
        raw += raw_size

    image_end = raw
    certificate = (image_end + len(overlay), len(signature)) if signature else (0, 0)
    directories = [(0, 0)] * 16
    directories[2] = resource
    directories[4] = certificate
    optional = struct.pack("<HBB", 0x20B, 14, 0).ljust(108, b"\0")
    optional = (
        optional[:56]
        + struct.pack("<II", rva, _HEADERS_SIZE)
        + optional[64:108]
        + struct.pack("<I", 16)
    )
    optional += b"".join(struct.pack("<II", *entry) for entry in directories)
    coff = struct.pack("<HHIIIHH", 0x8664, len(sections), 0, 0, 0, len(optional), 0x22)
    dos = b"MZ".ljust(60, b"\0") + struct.pack("<I", 0x40)
    headers = (dos + b"PE\0\0" + coff + optional + table).ljust(_HEADERS_SIZE, b"\0")
    return headers + body + overlay + signature


def nsis_installer(payload: bytes = b"\0" * 512) -> bytes:
    """Shaped like an NSIS installer: the NSIS first header after the image."""

    first_header = struct.pack("<II", 0, 0xDEADBEEF) + b"NullsoftInst" + struct.pack("<II", 0, 0)
    return build_pe(overlay=first_header + payload)


def inno_installer(name: str = "Tool") -> bytes:
    return build_pe(
        version={
            "Comments": "This installation was built with Inno Setup.",
            "FileDescription": f"{name} Setup",
            "ProductName": name,
        },
        overlay=b"zlb\x1a" + b"\0" * 256,
    )


def versioned_program(description: str, product: str = "Tool") -> bytes:
    return build_pe(version={"FileDescription": description, "ProductName": product})


def portable_program() -> bytes:
    """An ordinary program: version info, no installer data."""

    return versioned_program("Portable Tool", "Portable Tool")


__all__ = [
    "build_pe",
    "inno_installer",
    "nsis_installer",
    "portable_program",
    "version_info",
    "versioned_program",
]
