"""Windows-only, read-only platform primitives for the current milestone."""

from .filesystem import (
    FILE_ATTRIBUTE_OFFLINE,
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS,
    FILE_ATTRIBUTE_RECALL_ON_OPEN,
    FILE_ATTRIBUTE_REPARSE_POINT,
    FileSystemMetadata,
    is_cloud_placeholder,
    is_cloud_reparse_tag,
    read_file_metadata,
)

__all__ = [
    "FILE_ATTRIBUTE_OFFLINE",
    "FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS",
    "FILE_ATTRIBUTE_RECALL_ON_OPEN",
    "FILE_ATTRIBUTE_REPARSE_POINT",
    "FileSystemMetadata",
    "is_cloud_placeholder",
    "is_cloud_reparse_tag",
    "read_file_metadata",
]
