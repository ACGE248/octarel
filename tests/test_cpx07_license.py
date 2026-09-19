"""ENG-CP-07 (issue #175): LICENSE, NOTICE, and third-party attribution."""

from __future__ import annotations

from tests.octarel_paths import OCTAREL_ROOT

LICENSE = OCTAREL_ROOT / "LICENSE"
NOTICE = OCTAREL_ROOT / "NOTICE"
THIRD_PARTY = OCTAREL_ROOT / "THIRD_PARTY.md"
ASSET_LICENSES = OCTAREL_ROOT / "scripts/agents/control_plane/dashboard/ASSET_LICENSES.md"
XTERM_LICENSE = OCTAREL_ROOT / "scripts/agents/control_plane/dashboard/vendor/xterm/LICENSE"
SIMPLE_ICONS = OCTAREL_ROOT / "scripts/agents/control_plane/dashboard/assets/icons/LICENSE-simple-icons.md"


def test_mit_license_present() -> None:
    text = LICENSE.read_text(encoding="utf-8")
    assert "MIT License" in text
    assert "Octarel contributors" in text


def test_notice_covers_vendored_runtime_assets() -> None:
    text = NOTICE.read_text(encoding="utf-8")
    assert "xterm" in text.lower()
    assert "simple icons" in text.lower() or "Simple Icons" in text
    assert "CC0" in text
    assert XTERM_LICENSE.is_file()
    assert SIMPLE_ICONS.is_file()
    assert ASSET_LICENSES.is_file()


def test_third_party_inventory_lists_runtime_and_dev_dependencies() -> None:
    text = THIRD_PARTY.read_text(encoding="utf-8")
    for name in ("fastapi", "uvicorn", "httpx", "psutil", "PyJWT", "pytest", "ruff"):
        assert name.lower() in text.lower()
    for name in ("@playwright/test", "@axe-core/playwright", "@xterm/xterm", "simple-icons"):
        assert name in text


def test_every_vendored_dashboard_asset_has_a_license_file() -> None:
    vendor = OCTAREL_ROOT / "scripts/agents/control_plane/dashboard/vendor"
    for child in vendor.iterdir():
        if child.is_dir():
            assert (child / "LICENSE").is_file() or list(child.glob("LICENSE*")), child
