"""The coverage gate must not disarm itself when two files share a basename.

``scripts/check_coverage.py`` used to key modules by ``Path(filename).name``.
A second ``ratelimit.py`` (an Ecoledirecte limiter under the same package)
would overwrite the Pronote row: the 100 % bar would measure the new file,
Pronote's limiter could drop in silence, and CI would stay green. That is the
same family of defect as a ``--since`` argument the citation gate used to
ignore.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "check_coverage", ROOT / "scripts" / "check_coverage.py"
)
assert _SPEC is not None
assert _SPEC.loader is not None
check_coverage = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_coverage)


def _class(parent: Element, filename: str, hits: str) -> None:
    """One coverage class with a single line of known hits."""
    item = SubElement(parent, "class", filename=filename)
    lines = SubElement(item, "lines")
    SubElement(lines, "line", number="1", hits=hits)


def _report(*files: tuple[str, str], source: str | None = None) -> Element:
    root = Element("coverage", attrib={"line-rate": "1.0", "branch-rate": "1.0"})
    if source is not None:
        sources = SubElement(root, "sources")
        element = SubElement(sources, "source")
        element.text = source
    packages = SubElement(root, "packages")
    package = SubElement(packages, "package")
    classes = SubElement(package, "classes")
    for filename, hits in files:
        _class(classes, filename, hits)
    return root


def test_a_second_ratelimit_py_does_not_replace_the_pronote_row() -> None:
    """The Pronote limiter stays the gated file when an ED limiter shares the name."""
    root = _report(
        ("custom_components/pronote_ng/ratelimit.py", "1"),
        ("custom_components/pronote_ng/connectors/ecoledirecte/ratelimit.py", "0"),
    )
    per_module = check_coverage._module_coverage(root)
    assert check_coverage.coverage_for(per_module, "pronote_ng/ratelimit.py") == 100.0
    ed = check_coverage.coverage_for(
        per_module, "pronote_ng/connectors/ecoledirecte/ratelimit.py"
    )
    assert ed == 0.0


def test_the_gate_finds_pronote_modules_under_the_package_prefix() -> None:
    """coverage.xml stores a prefix; the required key is the package suffix."""
    root = _report(("custom_components/pronote_ng/gateway.py", "1"))
    per_module = check_coverage._module_coverage(root)
    assert check_coverage.coverage_for(per_module, "pronote_ng/gateway.py") == 100.0


def test_a_package_relative_cobertura_report_joins_source_to_find_pronote() -> None:
    """CI writes basenames; joining <source> is what makes the suffix exist."""
    root = _report(
        ("ratelimit.py", "1"),
        ("connectors/ecoledirecte/ratelimit.py", "0"),
        source="/home/runner/work/ha-pronote-ng/ha-pronote-ng/custom_components/pronote_ng",
    )
    per_module = check_coverage._module_coverage(root)
    assert check_coverage.coverage_for(per_module, "pronote_ng/ratelimit.py") == 100.0
    ed = check_coverage.coverage_for(
        per_module, "pronote_ng/connectors/ecoledirecte/ratelimit.py"
    )
    assert ed == 0.0


def test_an_ambiguous_suffix_is_missing_so_the_gate_fails_closed() -> None:
    """Two files ending with the same required path must not pick a winner."""
    root = _report(
        ("a/pronote_ng/delta.py", "1"),
        ("b/pronote_ng/delta.py", "0"),
    )
    per_module = check_coverage._module_coverage(root)
    assert check_coverage.coverage_for(per_module, "pronote_ng/delta.py") is None


def test_tostring_keeps_the_fixture_well_formed() -> None:
    """Guard the helper: a broken fixture would make the lookups pass vacuously."""
    xml = tostring(_report(("pronote_ng/scheduler.py", "1")), encoding="unicode")
    assert "scheduler.py" in xml
