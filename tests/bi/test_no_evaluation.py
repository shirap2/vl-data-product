"""The BI app does not use the ground-truth evaluation module."""

import subprocess
import sys
from pathlib import Path

import pytest

import vl_data.bi as bi_package

MODULE = "vl_data.usecase.evaluate"
PACKAGE_DIR = Path(bi_package.__file__).parent


def test_app_sources_do_not_mention_the_module() -> None:
    sources = sorted(PACKAGE_DIR.glob("*.py"))
    assert len(sources) >= 7
    for source in sources:
        assert MODULE not in source.read_text(encoding="utf-8"), source.name


def test_importing_the_tabs_does_not_load_the_module() -> None:
    code = (
        "import sys\n"
        "import vl_data.bi.data, vl_data.bi.analyze_tab, vl_data.bi.shell\n"
        f"sys.exit(1 if {MODULE!r} in sys.modules else 0)\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, check=False, timeout=120
        )
    except subprocess.TimeoutExpired:
        pytest.fail("importing the BI modules in a subprocess did not finish within 120 seconds")
    assert result.returncode == 0, result.stderr.decode(errors="replace")[-500:]
