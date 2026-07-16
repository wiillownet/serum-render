from __future__ import annotations

import os
from pathlib import Path

import pytest

# Disable Typer's rich-panel error rendering for the whole test session.
# Typer + Click + Rich wrap BadParameter messages into a bordered panel
# whose layout depends on the terminal mode. On CI runners the panel
# renders in a degenerate single-line form that drops the inner text
# entirely — substring assertions like "duration must be > 0" then fail
# even though the exit code is correct. Plain-text mode emits the same
# content as a regular line that assertions can match reliably across
# local + CI environments.
from serum_render.cli import app as _cli_app

_cli_app.rich_markup_mode = None
_cli_app.pretty_exceptions_enable = False


def pytest_addoption(parser):
    parser.addoption(
        "--serum1-plugin-path",
        action="store",
        default=None,
        help="Path to a Serum 1 VST2 (loads .fxp presets) for integration tests.",
    )
    parser.addoption(
        "--serum2-plugin-path",
        action="store",
        default=None,
        help="Path to the Serum 2 VST3 (loads .SerumPreset) for integration tests.",
    )
    parser.addoption(
        "--serum1-preset-dir",
        action="store",
        default=None,
        help="Directory containing .fxp presets for integration tests.",
    )
    parser.addoption(
        "--serum2-preset-dir",
        action="store",
        default=None,
        help="Directory containing .SerumPreset files for integration tests.",
    )


def _gated_path(request, flag: str, env: str) -> str:
    value = request.config.getoption(flag) or os.environ.get(env)
    if not value:
        pytest.skip(f"Not provided. Set {flag} or {env}.")
    return str(Path(value).resolve())


@pytest.fixture
def serum1_plugin_path(request) -> str:
    """Resolved path to a Serum 1 plugin (loads .fxp). Skips if unset."""
    return _gated_path(request, "--serum1-plugin-path", "SERUM1_PLUGIN_PATH")


@pytest.fixture
def serum2_plugin_path(request) -> str:
    """Resolved path to the Serum 2 VST3. Skips if unset."""
    return _gated_path(request, "--serum2-plugin-path", "SERUM2_PLUGIN_PATH")


def _gated_presets(request, flag: str, env: str, pattern: str) -> list[str]:
    preset_dir = request.config.getoption(flag) or os.environ.get(env)
    if not preset_dir:
        pytest.skip(f"Not provided. Set {flag} or {env}.")
    files = sorted(Path(preset_dir).rglob(pattern))[:2]
    if len(files) < 2:
        pytest.skip(f"Need >=2 {pattern} files in {preset_dir}, found {len(files)}.")
    return [str(f.resolve()) for f in files]


@pytest.fixture
def serum1_preset_files(request) -> list[str]:
    """Two real `.fxp` files for smoke tests; skips if unavailable."""
    return _gated_presets(
        request, "--serum1-preset-dir", "SERUM1_PRESET_DIR", "*.fxp"
    )


@pytest.fixture
def serum2_preset_files(request) -> list[str]:
    """Two real `.SerumPreset` files for smoke tests; skips if unavailable.
    Gated independently so a user with only one plugin runs their half."""
    return _gated_presets(
        request, "--serum2-preset-dir", "SERUM2_PRESET_DIR", "*.SerumPreset"
    )
