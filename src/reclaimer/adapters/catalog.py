"""Explicit built-in adapter catalog.

No reflection, package entry points, or user-provided modules are loaded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from reclaimer.core.models import EffectClass


@dataclass(frozen=True, slots=True)
class AdapterDescriptor:
    adapter_id: str
    display_name: str
    effect_class: EffectClass
    official_source: str
    checked_at: date
    inventory_only: bool = True


BUILTIN_ADAPTERS: tuple[AdapterDescriptor, ...] = (
    AdapterDescriptor(
        "filesystem",
        "Report-only directory inventory",
        EffectClass.PURE_QUERY,
        "https://docs.python.org/3.11/library/os.html#os.scandir",
        date(2026, 7, 10),
    ),
    AdapterDescriptor(
        "windows_maintenance",
        "Windows official maintenance guidance",
        EffectClass.PURE_QUERY,
        "https://learn.microsoft.com/windows-hardware/manufacture/desktop/what-is-dism",
        date(2026, 7, 10),
    ),
    AdapterDescriptor(
        "huggingface",
        "Hugging Face cache inventory",
        EffectClass.PURE_QUERY,
        "https://huggingface.co/docs/huggingface_hub/en/guides/cli",
        date(2026, 7, 10),
    ),
    AdapterDescriptor(
        "pip",
        "pip cache inventory",
        EffectClass.PURE_QUERY,
        "https://pip.pypa.io/en/stable/cli/pip_cache/",
        date(2026, 7, 10),
    ),
    AdapterDescriptor(
        "uv",
        "uv cache inventory",
        EffectClass.PURE_QUERY,
        "https://docs.astral.sh/uv/concepts/cache/",
        date(2026, 7, 10),
    ),
    AdapterDescriptor(
        "conda",
        "Conda cache dry-run inventory",
        EffectClass.OBSERVATION_WITH_OPERATIONAL_WRITES,
        "https://docs.conda.io/projects/conda/en/stable/commands/clean.html",
        date(2026, 7, 10),
    ),
    AdapterDescriptor(
        "npm",
        "npm cache inventory",
        EffectClass.PURE_QUERY,
        "https://docs.npmjs.com/cli/v11/commands/npm-cache/",
        date(2026, 7, 10),
    ),
    AdapterDescriptor(
        "pnpm",
        "pnpm store inventory",
        EffectClass.PURE_QUERY,
        "https://pnpm.io/cli/store",
        date(2026, 7, 10),
    ),
    AdapterDescriptor(
        "docker",
        "Docker online-daemon inventory",
        EffectClass.OBSERVATION_WITH_OPERATIONAL_WRITES,
        "https://docs.docker.com/reference/cli/docker/system/df/",
        date(2026, 7, 10),
    ),
    AdapterDescriptor(
        "ollama",
        "Ollama loopback inventory",
        EffectClass.PURE_QUERY,
        "https://docs.ollama.com/api/tags",
        date(2026, 7, 10),
    ),
    AdapterDescriptor(
        "vscode",
        "VS Code extension directory inventory",
        EffectClass.PURE_QUERY,
        "https://code.visualstudio.com/docs/configure/extensions/extension-marketplace",
        date(2026, 7, 11),
    ),
)

_BY_ID = {descriptor.adapter_id: descriptor for descriptor in BUILTIN_ADAPTERS}


def get_descriptor(adapter_id: str) -> AdapterDescriptor | None:
    return _BY_ID.get(adapter_id)


def list_descriptors() -> tuple[AdapterDescriptor, ...]:
    return BUILTIN_ADAPTERS
