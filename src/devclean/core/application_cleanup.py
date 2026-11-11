"""Public semantic boundary for application-aware cleanup.

The stable Codex engine and each audited application profile live behind this
facade. Generic scan/delete code imports only this module so application-owned
paths keep their source-specific semantics: TOOL is program-decided, USER is a
real user-preference decision, and KEEP remains non-deletable.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import os
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime
from functools import lru_cache
from pathlib import PurePath, PureWindowsPath

from devclean.core import _application_cleanup_impl as _impl
from devclean.core import android_avd_cleanup as _android_avd
from devclean.core import android_sdk_cleanup as _android_sdk
from devclean.core import android_studio_cleanup as _android_studio
from devclean.core import brave_cleanup as _brave_cleanup
from devclean.core import bun_cleanup as _bun
from devclean.core import cargo_cleanup as _cargo
from devclean.core import chrome_cleanup as _chrome_cleanup
from devclean.core import conda_cleanup as _conda
from devclean.core import cursor_cleanup as _cursor
from devclean.core import cypress_cleanup as _cypress
from devclean.core import docker_cleanup as _docker
from devclean.core import edge_cleanup as _edge_cleanup
from devclean.core import electron_cleanup as _electron
from devclean.core import firefox_cleanup as _firefox_cleanup
from devclean.core import go_cleanup as _go
from devclean.core import gradle_cleanup as _gradle
from devclean.core import huggingface_cleanup as _huggingface
from devclean.core import jetbrains_cleanup as _jetbrains
from devclean.core import maven_cleanup as _maven
from devclean.core import npm_cleanup as _npm
from devclean.core import nuget_cleanup as _nuget
from devclean.core import ollama_cleanup as _ollama
from devclean.core import opera_cleanup as _opera_cleanup
from devclean.core import pip_cleanup as _pip
from devclean.core import playwright_cleanup as _playwright
from devclean.core import pnpm_cleanup as _pnpm
from devclean.core import puppeteer_cleanup as _puppeteer
from devclean.core import toolbox_cleanup as _toolbox
from devclean.core import trae_cleanup as _trae_cleanup
from devclean.core import uv_cleanup as _uv
from devclean.core import visual_studio_cleanup as _visual_studio
from devclean.core import visual_studio_installer_cleanup as _visual_studio_installer
from devclean.core import vivaldi_cleanup as _vivaldi_cleanup
from devclean.core import vscode_cleanup as _vscode_cleanup
from devclean.core import windsurf_cleanup as _windsurf_cleanup
from devclean.core import yarn_cleanup as _yarn
from devclean.core._application_cleanup_impl import (
    CODEX_RULES,
    ApplicationCleanupRule,
    ApplicationPolicyDecision,
    ApplicationRoot,
    DecisionOwner,
    LastUseStrategy,
    MatchKind,
    PolicyAction,
    RebuildCost,
    effective_idle_days,
    file_time_utc,
    scan_root_snapshot,
    snapshot_roots,
)
from devclean.core.android_avd_cleanup import (
    ANDROID_AVD_RULES,
    android_avd_process_running,
    android_avd_scan_roots,
    clear_android_avd_process_cache,
    evaluate_android_avd_path,
    match_android_avd_rule,
)
from devclean.core.android_sdk_cleanup import (
    ANDROID_SDK_RULES,
    android_sdk_audited_tool_roots,
    android_sdk_process_running,
    android_sdk_scan_roots,
    clear_android_sdk_process_cache,
    evaluate_android_sdk_path,
    match_android_sdk_rule,
    whole_tree_android_sdk_rule,
)
from devclean.core.android_studio_cleanup import (
    ANDROID_STUDIO_RULES,
    android_studio_audited_tool_roots,
    android_studio_process_running,
    android_studio_scan_roots,
    clear_android_studio_process_cache,
    evaluate_android_studio_path,
    match_android_studio_rule,
    whole_tree_android_studio_rule,
)
from devclean.core.brave_cleanup import (
    BRAVE_RULES,
    brave_audited_tool_roots,
    brave_process_running,
    brave_scan_roots,
    clear_brave_process_cache,
    evaluate_brave_path,
    match_brave_rule,
    whole_tree_brave_rule,
)
from devclean.core.bun_cleanup import (
    BUN_RULES,
    bun_audited_tool_roots,
    bun_process_running,
    bun_scan_roots,
    clear_bun_process_cache,
    evaluate_bun_path,
    match_bun_rule,
    whole_tree_bun_rule,
)
from devclean.core.cargo_cleanup import (
    CARGO_RULES,
    cargo_audited_tool_roots,
    cargo_process_running,
    cargo_scan_roots,
    clear_cargo_process_cache,
    evaluate_cargo_path,
    match_cargo_rule,
    whole_tree_cargo_rule,
)
from devclean.core.chrome_cleanup import (
    CHROME_RULES,
    chrome_audited_tool_roots,
    chrome_process_running,
    chrome_scan_roots,
    clear_chrome_process_cache,
    evaluate_chrome_path,
    match_chrome_rule,
    whole_tree_chrome_rule,
)
from devclean.core.claude_cleanup import (
    CLAUDE_RULES,
    claude_application_roots,
    claude_process_running,
    claude_scan_roots,
    clear_claude_process_cache,
    clear_claude_settings_cache,
    evaluate_claude_path,
    match_claude_rule,
)
from devclean.core.conda_cleanup import (
    CONDA_RULES,
    clear_conda_process_cache,
    conda_audited_tool_roots,
    conda_process_running,
    conda_scan_roots,
    evaluate_conda_path,
    match_conda_rule,
    whole_tree_conda_rule,
)
from devclean.core.cursor_cleanup import (
    CURSOR_RULES,
    clear_cursor_process_cache,
    cursor_application_roots,
    cursor_process_running,
    cursor_scan_roots,
    evaluate_cursor_path,
    match_cursor_rule,
)
from devclean.core.cypress_cleanup import (
    CYPRESS_RULES,
    clear_cypress_process_cache,
    cypress_audited_tool_roots,
    cypress_process_running,
    cypress_scan_roots,
    evaluate_cypress_path,
    match_cypress_rule,
    whole_tree_cypress_rule,
)
from devclean.core.docker_cleanup import (
    DOCKER_RULES,
    clear_docker_process_cache,
    docker_audited_tool_roots,
    docker_process_running,
    docker_scan_roots,
    evaluate_docker_path,
    match_docker_rule,
    whole_tree_docker_rule,
)
from devclean.core.edge_cleanup import (
    EDGE_RULES,
    clear_edge_process_cache,
    edge_audited_tool_roots,
    edge_process_running,
    edge_scan_roots,
    evaluate_edge_path,
    match_edge_rule,
    whole_tree_edge_rule,
)
from devclean.core.electron_cleanup import (
    ELECTRON_RULES,
    clear_electron_process_cache,
    electron_audited_tool_roots,
    electron_process_running,
    electron_scan_roots,
    evaluate_electron_path,
    match_electron_rule,
    whole_tree_electron_rule,
)
from devclean.core.firefox_cleanup import (
    FIREFOX_RULES,
    clear_firefox_process_cache,
    evaluate_firefox_path,
    firefox_audited_tool_roots,
    firefox_process_running,
    firefox_scan_roots,
    match_firefox_rule,
    whole_tree_firefox_rule,
)
from devclean.core.go_cleanup import (
    GO_RULES,
    clear_go_process_cache,
    evaluate_go_path,
    go_audited_tool_roots,
    go_process_running,
    go_scan_roots,
    match_go_rule,
    whole_tree_go_rule,
)
from devclean.core.gradle_cleanup import (
    GRADLE_RULES,
    clear_gradle_process_cache,
    evaluate_gradle_path,
    gradle_audited_tool_roots,
    gradle_process_running,
    gradle_scan_roots,
    match_gradle_rule,
    whole_tree_gradle_rule,
)
from devclean.core.huggingface_cleanup import (
    HUGGINGFACE_RULES,
    clear_huggingface_process_cache,
    evaluate_huggingface_path,
    huggingface_audited_tool_roots,
    huggingface_process_running,
    huggingface_scan_roots,
    match_huggingface_rule,
    whole_tree_huggingface_rule,
)
from devclean.core.jetbrains_cleanup import (
    JETBRAINS_RULES,
    clear_jetbrains_process_cache,
    evaluate_jetbrains_path,
    jetbrains_audited_tool_roots,
    jetbrains_process_running,
    jetbrains_scan_roots,
    match_jetbrains_rule,
    whole_tree_jetbrains_rule,
)
from devclean.core.maven_cleanup import (
    MAVEN_RULES,
    clear_maven_process_cache,
    evaluate_maven_path,
    match_maven_rule,
    maven_audited_tool_roots,
    maven_process_running,
    maven_scan_roots,
    whole_tree_maven_rule,
)
from devclean.core.npm_cleanup import (
    NPM_RULES,
    clear_npm_process_cache,
    evaluate_npm_path,
    match_npm_rule,
    npm_audited_tool_roots,
    npm_process_running,
    npm_scan_roots,
    whole_tree_npm_rule,
)
from devclean.core.nuget_cleanup import (
    NUGET_RULES,
    clear_nuget_process_cache,
    evaluate_nuget_path,
    match_nuget_rule,
    nuget_audited_tool_roots,
    nuget_process_running,
    nuget_scan_roots,
    whole_tree_nuget_rule,
)
from devclean.core.ollama_cleanup import (
    OLLAMA_RULES,
    clear_ollama_process_cache,
    evaluate_ollama_path,
    match_ollama_rule,
    ollama_audited_tool_roots,
    ollama_process_running,
    ollama_scan_roots,
    whole_tree_ollama_rule,
)
from devclean.core.opera_cleanup import (
    OPERA_RULES,
    clear_opera_process_cache,
    evaluate_opera_path,
    match_opera_rule,
    opera_audited_tool_roots,
    opera_process_running,
    opera_scan_roots,
    whole_tree_opera_rule,
)
from devclean.core.pip_cleanup import (
    PIP_RULES,
    clear_pip_process_cache,
    evaluate_pip_path,
    match_pip_rule,
    pip_audited_tool_roots,
    pip_process_running,
    pip_scan_roots,
    whole_tree_pip_rule,
)
from devclean.core.playwright_cleanup import (
    PLAYWRIGHT_RULES,
    clear_playwright_process_cache,
    evaluate_playwright_path,
    match_playwright_rule,
    playwright_audited_tool_roots,
    playwright_process_running,
    playwright_scan_roots,
    whole_tree_playwright_rule,
)
from devclean.core.pnpm_cleanup import (
    PNPM_RULES,
    clear_pnpm_process_cache,
    evaluate_pnpm_path,
    match_pnpm_rule,
    pnpm_audited_tool_roots,
    pnpm_process_running,
    pnpm_scan_roots,
    whole_tree_pnpm_rule,
)
from devclean.core.puppeteer_cleanup import (
    PUPPETEER_RULES,
    clear_puppeteer_process_cache,
    evaluate_puppeteer_path,
    match_puppeteer_rule,
    puppeteer_audited_tool_roots,
    puppeteer_process_running,
    puppeteer_scan_roots,
    whole_tree_puppeteer_rule,
)
from devclean.core.toolbox_cleanup import (
    TOOLBOX_RULES,
    clear_toolbox_process_cache,
    evaluate_toolbox_path,
    match_toolbox_rule,
    toolbox_audited_tool_roots,
    toolbox_process_running,
    toolbox_scan_roots,
    whole_tree_toolbox_rule,
)
from devclean.core.trae_cleanup import (
    TRAE_RULES,
    clear_trae_process_cache,
    evaluate_trae_path,
    match_trae_rule,
    trae_audited_tool_roots,
    trae_process_running,
    trae_scan_roots,
    whole_tree_trae_rule,
)
from devclean.core.uv_cleanup import (
    UV_RULES,
    clear_uv_process_cache,
    evaluate_uv_path,
    match_uv_rule,
    uv_audited_tool_roots,
    uv_process_running,
    uv_scan_roots,
    whole_tree_uv_rule,
)
from devclean.core.visual_studio_cleanup import (
    VISUAL_STUDIO_RULES,
    clear_visual_studio_process_cache,
    evaluate_visual_studio_path,
    match_visual_studio_rule,
    visual_studio_audited_tool_roots,
    visual_studio_process_running,
    visual_studio_scan_roots,
    whole_tree_visual_studio_rule,
)
from devclean.core.visual_studio_installer_cleanup import (
    VISUAL_STUDIO_INSTALLER_RULES,
    clear_visual_studio_installer_process_cache,
    evaluate_visual_studio_installer_path,
    match_visual_studio_installer_rule,
    visual_studio_installer_audited_tool_roots,
    visual_studio_installer_process_running,
    visual_studio_installer_scan_roots,
    whole_tree_visual_studio_installer_rule,
)
from devclean.core.vivaldi_cleanup import (
    VIVALDI_RULES,
    clear_vivaldi_process_cache,
    evaluate_vivaldi_path,
    match_vivaldi_rule,
    vivaldi_audited_tool_roots,
    vivaldi_process_running,
    vivaldi_scan_roots,
    whole_tree_vivaldi_rule,
)
from devclean.core.vscode_cleanup import (
    VSCODE_RULES,
    clear_vscode_process_cache,
    evaluate_vscode_path,
    match_vscode_rule,
    vscode_audited_tool_roots,
    vscode_process_running,
    vscode_scan_roots,
    whole_tree_vscode_rule,
)
from devclean.core.windsurf_cleanup import (
    WINDSURF_RULES,
    clear_windsurf_process_cache,
    evaluate_windsurf_path,
    match_windsurf_rule,
    whole_tree_windsurf_rule,
    windsurf_audited_tool_roots,
    windsurf_process_running,
    windsurf_scan_roots,
)
from devclean.core.yarn_cleanup import (
    YARN_RULES,
    clear_yarn_process_cache,
    evaluate_yarn_path,
    match_yarn_rule,
    whole_tree_yarn_rule,
    yarn_audited_tool_roots,
    yarn_process_running,
    yarn_scan_roots,
)

_ORIGINAL_APPLICATION_ROOTS = _impl.application_roots
_ORIGINAL_EVALUATE_APPLICATION_PATH = _impl.evaluate_application_path
_ORIGINAL_MATCH_APPLICATION_RULE = _impl.match_application_rule
_ORIGINAL_APPLICATION_PROCESS_RUNNING = _impl.application_process_running


def application_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[ApplicationRoot, ...]:
    """Return audited roots represented by the fixed-key application profiles."""

    return (
        *_ORIGINAL_APPLICATION_ROOTS(environment),
        *claude_application_roots(environment),
        *cursor_application_roots(environment),
    )


def application_scan_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[PureWindowsPath, ...]:
    """Return application-specific roots that may contain reclaimable storage."""

    codex = tuple(root.path for root in _ORIGINAL_APPLICATION_ROOTS(environment))
    return tuple(
        dict.fromkeys(
            (
                *codex,
                *claude_scan_roots(environment),
                *cursor_scan_roots(environment),
                *vscode_scan_roots(environment),
                *trae_scan_roots(environment),
                *windsurf_scan_roots(environment),
                *npm_scan_roots(environment),
                *pnpm_scan_roots(environment),
                *yarn_scan_roots(environment),
                *bun_scan_roots(environment),
                *pip_scan_roots(environment),
                *uv_scan_roots(environment),
                *conda_scan_roots(environment),
                *nuget_scan_roots(environment),
                *go_scan_roots(environment),
                *cargo_scan_roots(environment),
                *maven_scan_roots(environment),
                *huggingface_scan_roots(environment),
                *ollama_scan_roots(environment),
                *docker_scan_roots(environment),
                *playwright_scan_roots(environment),
                *puppeteer_scan_roots(environment),
                *cypress_scan_roots(environment),
                *electron_scan_roots(environment),
                *visual_studio_scan_roots(environment),
                *visual_studio_installer_scan_roots(environment),
                *chrome_scan_roots(environment),
                *edge_scan_roots(environment),
                *brave_scan_roots(environment),
                *vivaldi_scan_roots(environment),
                *opera_scan_roots(environment),
                *firefox_scan_roots(environment),
                *jetbrains_scan_roots(environment),
                *toolbox_scan_roots(environment),
                *android_studio_scan_roots(environment),
                *gradle_scan_roots(environment),
                *android_sdk_scan_roots(environment),
                *android_avd_scan_roots(environment),
            )
        )
    )


def audited_dynamic_tool_roots(
    environment: Mapping[str, str] | None = None,
) -> tuple[tuple[PureWindowsPath, ApplicationCleanupRule], ...]:
    """Return whole-tree TOOL roots for profiles with multiple/dynamic roots."""

    return (
        *vscode_audited_tool_roots(environment),
        *trae_audited_tool_roots(environment),
        *windsurf_audited_tool_roots(environment),
        *npm_audited_tool_roots(environment),
        *pnpm_audited_tool_roots(environment),
        *yarn_audited_tool_roots(environment),
        *bun_audited_tool_roots(environment),
        *pip_audited_tool_roots(environment),
        *uv_audited_tool_roots(environment),
        *conda_audited_tool_roots(environment),
        *nuget_audited_tool_roots(environment),
        *go_audited_tool_roots(environment),
        *cargo_audited_tool_roots(environment),
        *maven_audited_tool_roots(environment),
        *huggingface_audited_tool_roots(environment),
        *ollama_audited_tool_roots(environment),
        *docker_audited_tool_roots(environment),
        *playwright_audited_tool_roots(environment),
        *puppeteer_audited_tool_roots(environment),
        *cypress_audited_tool_roots(environment),
        *electron_audited_tool_roots(environment),
        *visual_studio_audited_tool_roots(environment),
        *visual_studio_installer_audited_tool_roots(environment),
        *chrome_audited_tool_roots(environment),
        *edge_audited_tool_roots(environment),
        *brave_audited_tool_roots(environment),
        *vivaldi_audited_tool_roots(environment),
        *opera_audited_tool_roots(environment),
        *firefox_audited_tool_roots(environment),
        *jetbrains_audited_tool_roots(environment),
        *toolbox_audited_tool_roots(environment),
        *android_studio_audited_tool_roots(environment),
        *gradle_audited_tool_roots(environment),
        *android_sdk_audited_tool_roots(environment),
    )


def match_application_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    """Return the most-specific audited application rule for *path*."""

    android_avd = match_android_avd_rule(path, environment)
    if android_avd is not None:
        return android_avd
    android_sdk = match_android_sdk_rule(path, environment)
    if android_sdk is not None:
        return android_sdk
    gradle = match_gradle_rule(path, environment)
    if gradle is not None:
        return gradle
    android_studio = match_android_studio_rule(path, environment)
    if android_studio is not None:
        return android_studio
    toolbox = match_toolbox_rule(path, environment)
    if toolbox is not None:
        return toolbox
    jetbrains = match_jetbrains_rule(path, environment)
    if jetbrains is not None:
        return jetbrains
    firefox = match_firefox_rule(path, environment)
    if firefox is not None:
        return firefox
    opera = match_opera_rule(path, environment)
    if opera is not None:
        return opera
    vivaldi = match_vivaldi_rule(path, environment)
    if vivaldi is not None:
        return vivaldi
    brave = match_brave_rule(path, environment)
    if brave is not None:
        return brave
    edge = match_edge_rule(path, environment)
    if edge is not None:
        return edge
    chrome = match_chrome_rule(path, environment)
    if chrome is not None:
        return chrome
    windsurf = match_windsurf_rule(path, environment)
    if windsurf is not None:
        return windsurf
    trae = match_trae_rule(path, environment)
    if trae is not None:
        return trae
    vscode = match_vscode_rule(path, environment)
    if vscode is not None:
        return vscode
    cursor = match_cursor_rule(path, environment)
    if cursor is not None:
        return cursor
    claude = match_claude_rule(path, environment)
    if claude is not None:
        return claude
    visual_studio = match_visual_studio_rule(path, environment)
    if visual_studio is not None:
        return visual_studio
    visual_studio_installer = match_visual_studio_installer_rule(path, environment)
    if visual_studio_installer is not None:
        return visual_studio_installer
    electron = match_electron_rule(path, environment)
    if electron is not None:
        return electron
    cypress = match_cypress_rule(path, environment)
    if cypress is not None:
        return cypress
    puppeteer = match_puppeteer_rule(path, environment)
    if puppeteer is not None:
        return puppeteer
    playwright = match_playwright_rule(path, environment)
    if playwright is not None:
        return playwright
    docker = match_docker_rule(path, environment)
    if docker is not None:
        return docker
    ollama = match_ollama_rule(path, environment)
    if ollama is not None:
        return ollama
    huggingface = match_huggingface_rule(path, environment)
    if huggingface is not None:
        return huggingface
    maven = match_maven_rule(path, environment)
    if maven is not None:
        return maven
    cargo = match_cargo_rule(path, environment)
    if cargo is not None:
        return cargo
    go_rule = match_go_rule(path, environment)
    if go_rule is not None:
        return go_rule
    nuget = match_nuget_rule(path, environment)
    if nuget is not None:
        return nuget
    conda = match_conda_rule(path, environment)
    if conda is not None:
        return conda
    uv = match_uv_rule(path, environment)
    if uv is not None:
        return uv
    pip = match_pip_rule(path, environment)
    if pip is not None:
        return pip
    bun = match_bun_rule(path, environment)
    if bun is not None:
        return bun
    yarn = match_yarn_rule(path, environment)
    if yarn is not None:
        return yarn
    pnpm = match_pnpm_rule(path, environment)
    if pnpm is not None:
        return pnpm
    npm = match_npm_rule(path, environment)
    if npm is not None:
        return npm
    return _ORIGINAL_MATCH_APPLICATION_RULE(path, environment)


def evaluate_application_path(
    path: str | os.PathLike[str],
    *,
    logical_size: int,
    last_used: datetime | None,
    now: datetime | None = None,
    process_running: bool | None = None,
    environment: Mapping[str, str] | None = None,
) -> ApplicationPolicyDecision | None:
    """Evaluate one path for the generic scan/review pipeline.

    TOOL items retain their program decision, USER-owned data remains a real
    ``USER_DECISION`` for the review lane, and KEEP remains protected.
    """

    # Tried in this order; the first application with a decision wins.
    evaluators: tuple[tuple[str, Callable[..., ApplicationPolicyDecision | None]], ...] = (
        ("android_avd", evaluate_android_avd_path),
        ("android_sdk", evaluate_android_sdk_path),
        ("gradle", evaluate_gradle_path),
        ("android_studio", evaluate_android_studio_path),
        ("toolbox", evaluate_toolbox_path),
        ("jetbrains", evaluate_jetbrains_path),
        ("firefox", evaluate_firefox_path),
        ("opera", evaluate_opera_path),
        ("vivaldi", evaluate_vivaldi_path),
        ("brave", evaluate_brave_path),
        ("edge", evaluate_edge_path),
        ("chrome", evaluate_chrome_path),
        ("windsurf", evaluate_windsurf_path),
        ("trae", evaluate_trae_path),
        ("vscode", evaluate_vscode_path),
        ("cursor", evaluate_cursor_path),
        ("claude", evaluate_claude_path),
        ("visual_studio", evaluate_visual_studio_path),
        ("visual_studio_installer", evaluate_visual_studio_installer_path),
        ("electron", evaluate_electron_path),
        ("cypress", evaluate_cypress_path),
        ("puppeteer", evaluate_puppeteer_path),
        ("playwright", evaluate_playwright_path),
        ("docker", evaluate_docker_path),
        ("ollama", evaluate_ollama_path),
        ("huggingface", evaluate_huggingface_path),
        ("maven", evaluate_maven_path),
        ("cargo", evaluate_cargo_path),
        ("go", evaluate_go_path),
        ("nuget", evaluate_nuget_path),
        ("conda", evaluate_conda_path),
        ("uv", evaluate_uv_path),
        ("pip", evaluate_pip_path),
        ("bun", evaluate_bun_path),
        ("yarn", evaluate_yarn_path),
        ("pnpm", evaluate_pnpm_path),
        ("npm", evaluate_npm_path),
        ("codex", _ORIGINAL_EVALUATE_APPLICATION_PATH),
    )
    outside = _applications_not_holding(path) if environment is None else frozenset()
    for name, evaluate in evaluators:
        if name in outside:
            continue
        decision = evaluate(
            path,
            logical_size=logical_size,
            last_used=last_used,
            now=now,
            process_running=process_running,
            environment=environment,
        )
        if decision is None:
            continue
        if decision.action is PolicyAction.TOOL_KEEP_IN_USE and not must_close_before_cleanup(
            decision.rule
        ):
            # Judged as the pre-delete guard judges it (process_guard_refusal): a
            # cache cleanable while its program runs is listed as if it were
            # closed, and its deletion skips the files the program holds open.
            relaxed = evaluate(
                path,
                logical_size=logical_size,
                last_used=last_used,
                now=now,
                process_running=False,
                environment=environment,
            )
            if relaxed is not None:
                return relaxed
        return decision
    return None


@dataclasses.dataclass(frozen=True, slots=True)
class _ApplicationScope:
    """Where one application's matcher can ever find a rule for a path.

    *roots* returns the object(s) the matcher anchors its rules at; every path
    in it counts.  A few applications also recognise project files anywhere
    (``package.json``, ``Cargo.toml``...): those file names, path segments and
    file-name patterns make the application always be asked.
    """

    name: str
    roots: Callable[[], object]
    names: frozenset[str] = frozenset()
    parts: frozenset[str] = frozenset()
    globs: tuple[str, ...] = ()


# Every application except Claude, whose rules also anchor at the whole user
# profile.  Each matcher was read to confirm that, apart from the listed names,
# it only matches at or below the folders its root function returns; the
# evaluator equivalence test compares every decision with and without this.
_SCOPED_APPLICATIONS: tuple[_ApplicationScope, ...] = (
    _ApplicationScope("android_avd", lambda: _android_avd.android_avd_roots()),
    _ApplicationScope("android_sdk", lambda: _android_sdk.android_sdk_roots()),
    _ApplicationScope("gradle", lambda: _gradle.gradle_roots()),
    _ApplicationScope("android_studio", lambda: _android_studio.android_studio_roots()),
    _ApplicationScope("toolbox", lambda: _toolbox.toolbox_root()),
    _ApplicationScope("jetbrains", lambda: _jetbrains.jetbrains_roots()),
    _ApplicationScope("firefox", lambda: _firefox_cleanup.firefox_roots()),
    _ApplicationScope("opera", lambda: _opera_cleanup.opera_roots()),
    _ApplicationScope("vivaldi", lambda: _vivaldi_cleanup.vivaldi_roots()),
    _ApplicationScope("brave", lambda: _brave_cleanup.brave_roots()),
    _ApplicationScope("edge", lambda: _edge_cleanup.edge_roots()),
    _ApplicationScope("chrome", lambda: _chrome_cleanup.chrome_roots()),
    _ApplicationScope("windsurf", lambda: _windsurf_cleanup.windsurf_roots()),
    _ApplicationScope("trae", lambda: _trae_cleanup.trae_roots()),
    _ApplicationScope("vscode", lambda: _vscode_cleanup.vscode_roots()),
    _ApplicationScope("cursor", lambda: (_cursor.cursor_application_roots(), _cursor_temp_roots())),
    _ApplicationScope("visual_studio", lambda: _visual_studio.visual_studio_roots()),
    _ApplicationScope(
        "visual_studio_installer",
        lambda: _visual_studio_installer.visual_studio_installer_roots(),
    ),
    _ApplicationScope("electron", lambda: _electron.electron_roots()),
    _ApplicationScope("cypress", lambda: _cypress.cypress_roots()),
    _ApplicationScope("puppeteer", lambda: _puppeteer.puppeteer_roots()),
    _ApplicationScope("playwright", lambda: _playwright.playwright_roots()),
    _ApplicationScope("docker", lambda: _docker.docker_roots()),
    _ApplicationScope("ollama", lambda: _ollama.ollama_roots()),
    _ApplicationScope("huggingface", lambda: _huggingface.huggingface_roots()),
    _ApplicationScope(
        "maven",
        lambda: _maven.maven_roots(),
        names=_maven._PROJECT_METADATA_NAMES | _maven._PROJECT_MVN_FILES,
        parts=frozenset({".mvn"}),
    ),
    _ApplicationScope(
        "cargo",
        lambda: _cargo.cargo_roots(),
        names=_cargo._PROJECT_METADATA_NAMES | _cargo._PROJECT_CONFIG_NAMES,
    ),
    _ApplicationScope("go", lambda: _go.go_roots(), names=_go._PROJECT_METADATA_NAMES),
    _ApplicationScope("nuget", lambda: _nuget.nuget_roots(), names=_nuget._PROJECT_METADATA_NAMES),
    _ApplicationScope(
        "conda",
        lambda: _conda.conda_roots(),
        names=frozenset({".condarc", "condarc"}),
        parts=frozenset({"conda-meta"}),
    ),
    _ApplicationScope("uv", lambda: _uv.uv_roots()),
    _ApplicationScope("pip", lambda: _pip.pip_roots()),
    _ApplicationScope(
        "bun",
        lambda: _bun.bun_roots(),
        names=_bun._PROJECT_METADATA_NAMES,
        parts=frozenset({".bun"}),
    ),
    _ApplicationScope(
        "yarn",
        lambda: _yarn.yarn_roots(),
        names=_yarn._PROJECT_METADATA_NAMES,
        parts=frozenset({".yarn"}),
    ),
    _ApplicationScope(
        "pnpm", lambda: _pnpm.pnpm_roots(), names=_pnpm._PNPM_METADATA_FILENAMES
    ),
    _ApplicationScope(
        "npm",
        lambda: _npm.npm_roots(),
        names=_npm._NPM_KEEP_FILENAMES | frozenset({"npm-debug.log"}),
    ),
    _ApplicationScope("codex", lambda: _ORIGINAL_APPLICATION_ROOTS()),
)


def _cursor_temp_roots() -> object:
    # Imported here: the updater module patches this one while it loads.
    from devclean.core import cursor_updater_cleanup

    return cursor_updater_cleanup._temp_roots()


@dataclasses.dataclass(frozen=True, slots=True)
class _ScopeIndex:
    by_folder: dict[str, frozenset[str]]
    by_name: dict[str, frozenset[str]]
    by_part: dict[str, frozenset[str]]
    globs: tuple[tuple[str, str], ...]
    unbounded: frozenset[str]


def _applications_not_holding(path: str | os.PathLike[str]) -> frozenset[str]:
    """Scoped applications that cannot have a rule for *path*.

    Only inside a scan snapshot, where each root set is computed once and is
    the very object the matcher itself reads; elsewhere nothing is skipped.
    """

    if not _impl.in_scan_snapshot():
        return frozenset()
    index = _impl.run_cached("application_scope_index", _application_scope_index)
    normalized = _impl._normalize(path)
    holding = set(index.unbounded)
    end = normalized.find("\\")
    while True:
        prefix = normalized if end < 0 else normalized[:end]
        holding.update(index.by_folder.get(prefix, ()))
        if end < 0:
            break
        end = normalized.find("\\", end + 1)
    parts = normalized.split("\\")
    name = parts[-1]
    holding.update(index.by_name.get(name, ()))
    for part in parts:
        holding.update(index.by_part.get(part, ()))
    holding.update(app for pattern, app in index.globs if fnmatch.fnmatchcase(name, pattern))
    return frozenset(scope.name for scope in _SCOPED_APPLICATIONS if scope.name not in holding)


def _application_scope_index() -> _ScopeIndex:
    by_folder: dict[str, set[str]] = {}
    by_name: dict[str, set[str]] = {}
    by_part: dict[str, set[str]] = {}
    globs: list[tuple[str, str]] = []
    unbounded: set[str] = set()
    for scope in _SCOPED_APPLICATIONS:
        for root in _paths_within(scope.roots()):
            folder = _impl._normalize(root)
            if not folder or any(char in folder for char in "*?["):
                # A wildcard in the folder itself makes matching not a plain
                # prefix test; such an application is always asked.
                unbounded.add(scope.name)
            else:
                by_folder.setdefault(folder, set()).add(scope.name)
        for file_name in scope.names:
            by_name.setdefault(file_name.casefold(), set()).add(scope.name)
        for part in scope.parts:
            by_part.setdefault(part.casefold(), set()).add(scope.name)
        globs.extend((pattern.casefold(), scope.name) for pattern in scope.globs)
    return _ScopeIndex(
        by_folder={folder: frozenset(names) for folder, names in by_folder.items()},
        by_name={key: frozenset(names) for key, names in by_name.items()},
        by_part={key: frozenset(names) for key, names in by_part.items()},
        globs=tuple(globs),
        unbounded=frozenset(unbounded),
    )


def _paths_within(value: object) -> Iterator[str]:
    """Every path inside a root object: dataclass fields, tuples and lists."""

    if value is None or isinstance(value, (str, bytes, int, float, bool)):
        return
    if isinstance(value, PurePath):
        yield str(value)
    elif isinstance(value, (tuple, list, set, frozenset)):
        for member in value:
            yield from _paths_within(member)
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        for field in dataclasses.fields(value):
            yield from _paths_within(getattr(value, field.name))


PROTECTED_OWNER_REFUSAL = "the target now belongs to protected application data"
RUNNING_OWNER_REFUSAL = "owning application is running; close it before cleaning this target"

# One row per audited application: its live-process probe and the function
# that forgets everything cached for it (process state and tool discovery).
# Probes are looked up at call time so tests can replace them.
_APPLICATION_STATE: dict[str, tuple[Callable[[], bool], Callable[[], None]]] = {
    "android_avd": (lambda: android_avd_process_running(), clear_android_avd_process_cache),
    "android_sdk": (lambda: android_sdk_process_running(), clear_android_sdk_process_cache),
    "gradle": (lambda: gradle_process_running(), clear_gradle_process_cache),
    "android_studio": (
        lambda: android_studio_process_running(),
        clear_android_studio_process_cache,
    ),
    "toolbox": (lambda: toolbox_process_running(), clear_toolbox_process_cache),
    "jetbrains": (lambda: jetbrains_process_running(), clear_jetbrains_process_cache),
    "firefox": (lambda: firefox_process_running(), clear_firefox_process_cache),
    "opera": (lambda: opera_process_running(), clear_opera_process_cache),
    "vivaldi": (lambda: vivaldi_process_running(), clear_vivaldi_process_cache),
    "brave": (lambda: brave_process_running(), clear_brave_process_cache),
    "edge": (lambda: edge_process_running(), clear_edge_process_cache),
    "chrome": (lambda: chrome_process_running(), clear_chrome_process_cache),
    "visual_studio": (lambda: visual_studio_process_running(), clear_visual_studio_process_cache),
    "visual_studio_installer": (
        lambda: visual_studio_installer_process_running(),
        clear_visual_studio_installer_process_cache,
    ),
    "electron": (lambda: electron_process_running(), clear_electron_process_cache),
    "cypress": (lambda: cypress_process_running(), clear_cypress_process_cache),
    "puppeteer": (lambda: puppeteer_process_running(), clear_puppeteer_process_cache),
    "playwright": (lambda: playwright_process_running(), clear_playwright_process_cache),
    "docker": (lambda: docker_process_running(), clear_docker_process_cache),
    "ollama": (lambda: ollama_process_running(), clear_ollama_process_cache),
    "huggingface": (lambda: huggingface_process_running(), clear_huggingface_process_cache),
    "maven": (lambda: maven_process_running(), clear_maven_process_cache),
    "cargo": (lambda: cargo_process_running(), clear_cargo_process_cache),
    "go": (lambda: go_process_running(), clear_go_process_cache),
    "nuget": (lambda: nuget_process_running(), clear_nuget_process_cache),
    "conda": (lambda: conda_process_running(), clear_conda_process_cache),
    "uv": (lambda: uv_process_running(), clear_uv_process_cache),
    "pip": (lambda: pip_process_running(), clear_pip_process_cache),
    "bun": (lambda: bun_process_running(), clear_bun_process_cache),
    "yarn": (lambda: yarn_process_running(), clear_yarn_process_cache),
    "pnpm": (lambda: pnpm_process_running(), clear_pnpm_process_cache),
    "npm": (lambda: npm_process_running(), clear_npm_process_cache),
    "windsurf": (lambda: windsurf_process_running(), clear_windsurf_process_cache),
    "trae": (lambda: trae_process_running(), clear_trae_process_cache),
    "vscode": (lambda: vscode_process_running(), clear_vscode_process_cache),
    "cursor": (lambda: cursor_process_running(), clear_cursor_process_cache),
    "claude": (lambda: claude_process_running(), clear_claude_process_cache),
}


def application_process_running(app_id: str) -> bool:
    state = _APPLICATION_STATE.get(app_id)
    if state is None:
        return _ORIGINAL_APPLICATION_PROCESS_RUNNING(app_id)
    return state[0]()


def clear_process_cache() -> None:
    _impl.clear_process_cache()
    for _probe, clear in _APPLICATION_STATE.values():
        clear()


def _refresh_application(app_id: str) -> None:
    """Forget one application's cached process state and discovery.

    Within one cleanup run this happens once per application, not once per
    file, so a batch of that application's files starts its tools only once.
    """

    if not _impl.first_in_run(("refresh_application", app_id)):
        return
    state = _APPLICATION_STATE.get(app_id)
    if state is None:
        _impl.clear_process_cache()
    else:
        state[1]()


def process_guard_allows(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> bool:
    """Re-check application ownership and any live-process requirement.

    Candidate authority is established by the completed scan plus explicit user
    selection. A USER rule therefore must not be treated like KEEP here: USER is
    allowed after that selection, KEEP remains a hard block, and TOOL/USER paths
    with a process-close requirement still need the owning application closed.

    Ownership is re-read without starting other programs: environment, settings
    files, registry and folder layout are evaluated again, and a package
    manager's reported locations are reused only while that tool's own
    configuration files are unchanged (otherwise only that tool is asked
    again). A path no application owns, such as a downloaded installer, is then
    deleted as an ordinary file. For an owned path only the owning
    application is rediscovered and its process checked again; other
    applications' caches are left alone, so later ordinary files in the same
    cleanup still start nothing.
    """

    return process_guard_refusal(path, environment) is None


def process_guard_refusal(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> str | None:
    """Return why *path* may not be deleted now, or ``None`` when it may.

    See :func:`process_guard_allows` for what is re-checked and what is not.
    """

    clear_claude_settings_cache()
    rule = match_application_rule(path, environment)
    if rule is None:
        return None
    _refresh_application(rule.app_id)
    refreshed = match_application_rule(path, environment)
    if refreshed is not None and refreshed.app_id != rule.app_id:
        _refresh_application(refreshed.app_id)
    if refreshed is not None and refreshed.owner is DecisionOwner.KEEP:
        return PROTECTED_OWNER_REFUSAL
    if refreshed is None or not must_close_before_cleanup(refreshed):
        # Includes the audited caches: while their program runs, the files it
        # holds open are skipped at deletion and the rest still goes.
        return None
    if application_process_running(refreshed.app_id):
        return RUNNING_OWNER_REFUSAL
    return None


def must_close_before_cleanup(rule: ApplicationCleanupRule) -> bool:
    """Whether *rule*'s data may only be cleaned while its program is closed.

    The one answer for both sides: the scan (``evaluate_application_path``)
    lists the other rules' data while their program runs, and the pre-delete
    guard (``process_guard_refusal``) lets it go.
    """

    return rule.requires_process_closed and rule.rule_id not in cleanable_while_running()


@lru_cache(maxsize=1)
def cleanable_while_running() -> frozenset[str]:
    """Rules whose files a running program merely re-creates when they are gone.

    The browsers' and editors' audited caches (HTTP, code, GPU, shader...) and
    Claude's plugin clones nothing references any more.  Not here: IDE index
    caches, which an IDE keeps maintaining while it runs, and the downloads an
    editor may be installing from right now (extension VSIX packages, the WSL
    server); for those the program is closed first.
    """

    # Imported here: these modules patch this one while they load.
    from devclean.core import (
        brave_cache_semantics,
        chrome_cache_semantics,
        claude_plugin_cache_cleanup,
        edge_cache_semantics,
        firefox_cache_semantics,
        opera_cache_semantics,
        trae_windsurf_cache_semantics,
        vivaldi_cache_semantics,
        vscode_cache_semantics,
    )

    return frozenset(
        {
            *brave_cache_semantics._SAFE_CACHE_RULE_IDS,
            *chrome_cache_semantics._SAFE_CACHE_RULE_IDS,
            *edge_cache_semantics._SAFE_CACHE_RULE_IDS,
            *firefox_cache_semantics._SAFE_CACHE_RULE_IDS,
            *opera_cache_semantics._SAFE_CACHE_RULE_IDS,
            *vivaldi_cache_semantics._SAFE_CACHE_RULE_IDS,
            *vscode_cache_semantics._SAFE_CACHE_RULE_IDS,
            *trae_windsurf_cache_semantics._TRAE_SAFE_CACHE_RULE_IDS,
            *trae_windsurf_cache_semantics._WINDSURF_SAFE_CACHE_RULE_IDS,
            claude_plugin_cache_cleanup._STAGING_RULE.rule_id,
            claude_plugin_cache_cleanup._ORPHAN_RULE.rule_id,
        }
        - _INSTALLING_FROM
    )


# Editor downloads that an install in progress may be reading.
_INSTALLING_FROM = frozenset(
    {
        "vscode-extension-vsix-cache",
        "vscode-wsl-server-download-cache",
        "trae-cached-extension-vsix",
        "windsurf-extension-vsix-cache",
    }
)


def whole_tree_application_rule(
    path: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> ApplicationCleanupRule | None:
    """Return a TOOL rule only when *path* is exactly an audited whole-tree root."""

    dynamic = whole_tree_android_sdk_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_gradle_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_android_studio_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_toolbox_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_jetbrains_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_firefox_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_opera_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_vivaldi_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_brave_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_edge_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_chrome_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_visual_studio_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_visual_studio_installer_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_electron_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_cypress_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_puppeteer_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_playwright_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_docker_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_ollama_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_huggingface_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_maven_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_cargo_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_go_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_nuget_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_conda_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_uv_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_pip_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_bun_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_yarn_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_pnpm_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_npm_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_windsurf_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_trae_rule(path, environment)
    if dynamic is not None:
        return dynamic
    dynamic = whole_tree_vscode_rule(path, environment)
    if dynamic is not None:
        return dynamic
    rule = match_application_rule(path, environment)
    if (
        rule is None
        or rule.owner is not DecisionOwner.TOOL
        or not rule.allow_whole_tree
        or rule.root_key == "ANYWHERE"
    ):
        return None
    roots = {root.key: root.path for root in application_roots(environment)}
    root = roots.get(rule.root_key)
    if root is None:
        return None
    normalized = _impl._normalize(path)
    for expanded in _impl._expand_braces(rule.relative_pattern):
        candidate = PureWindowsPath(root) / expanded if expanded else PureWindowsPath(root)
        if normalized == _impl._normalize(candidate):
            return rule
    return None


def application_display_name(app_id: str) -> str:
    return {
        "android_avd": "Android Emulator AVD",
        "android_sdk": "Android SDK",
        "gradle": "Gradle",
        "android_studio": "Android Studio",
        "brave": "Brave",
        "bun": "Bun",
        "cargo": "Cargo / Rust",
        "chrome": "Chrome / Chromium",
        "conda": "Conda / Miniconda / Anaconda",
        "cypress": "Cypress",
        "docker": "Docker Desktop",
        "edge": "Microsoft Edge",
        "electron": "Electron",
        "firefox": "Mozilla Firefox",
        "go": "Go",
        "huggingface": "Hugging Face Hub",
        "jetbrains": "JetBrains IDE",
        "maven": "Apache Maven",
        "ollama": "Ollama",
        "playwright": "Playwright",
        "puppeteer": "Puppeteer",
        "toolbox": "JetBrains Toolbox",
        "nuget": "NuGet",
        "opera": "Opera / Opera GX",
        "vivaldi": "Vivaldi",
        "visual_studio": "Visual Studio",
        "visual_studio_installer": "Visual Studio Installer",
        "codex": "Codex",
        "claude": "Claude Code",
        "cursor": "Cursor",
        "npm": "npm",
        "pip": "pip",
        "pnpm": "pnpm",
        "uv": "uv",
        "yarn": "Yarn",
        "vscode": "VS Code",
        "trae": "Trae",
        "windsurf": "Windsurf",
    }.get(app_id, app_id)


__all__ = [
    "ANDROID_AVD_RULES",
    "ANDROID_SDK_RULES",
    "ANDROID_STUDIO_RULES",
    "BRAVE_RULES",
    "BUN_RULES",
    "CARGO_RULES",
    "CHROME_RULES",
    "CLAUDE_RULES",
    "CODEX_RULES",
    "CONDA_RULES",
    "CURSOR_RULES",
    "CYPRESS_RULES",
    "DOCKER_RULES",
    "EDGE_RULES",
    "ELECTRON_RULES",
    "FIREFOX_RULES",
    "GO_RULES",
    "GRADLE_RULES",
    "HUGGINGFACE_RULES",
    "JETBRAINS_RULES",
    "MAVEN_RULES",
    "NPM_RULES",
    "NUGET_RULES",
    "OLLAMA_RULES",
    "OPERA_RULES",
    "PIP_RULES",
    "PLAYWRIGHT_RULES",
    "PNPM_RULES",
    "PROTECTED_OWNER_REFUSAL",
    "PUPPETEER_RULES",
    "RUNNING_OWNER_REFUSAL",
    "TOOLBOX_RULES",
    "TRAE_RULES",
    "UV_RULES",
    "VISUAL_STUDIO_INSTALLER_RULES",
    "VISUAL_STUDIO_RULES",
    "VIVALDI_RULES",
    "VSCODE_RULES",
    "WINDSURF_RULES",
    "YARN_RULES",
    "ApplicationCleanupRule",
    "ApplicationPolicyDecision",
    "ApplicationRoot",
    "DecisionOwner",
    "LastUseStrategy",
    "MatchKind",
    "PolicyAction",
    "RebuildCost",
    "application_display_name",
    "application_process_running",
    "application_roots",
    "application_scan_roots",
    "audited_dynamic_tool_roots",
    "clear_process_cache",
    "effective_idle_days",
    "evaluate_application_path",
    "file_time_utc",
    "match_application_rule",
    "must_close_before_cleanup",
    "process_guard_allows",
    "process_guard_refusal",
    "scan_root_snapshot",
    "snapshot_roots",
    "whole_tree_application_rule",
]
