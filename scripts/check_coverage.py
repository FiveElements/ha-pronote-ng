#!/usr/bin/env python3
"""Enforce the coverage thresholds of SPECIFICATION.md §11.

A threshold that does not fail is not a threshold: this script exits non-zero
when the global line+branch coverage drops below 80 %, or when any of the four
modules where a mistake is invisible at runtime drops below 100 %.

Keys are path *suffixes* (``pronote_ng/ratelimit.py``), never basenames.
Indexing by ``Path(filename).name`` would let a second ``ratelimit.py``
(an Ecoledirecte limiter, say) overwrite the Pronote row and disarm the
gate while CI stayed green.
"""

from __future__ import annotations

from pathlib import Path
import sys
import xml.etree.ElementTree as ET

GLOBAL_MINIMUM = 80.0

# The four modules where an error produces no visible symptom: a rate limiter
# that lets a call through, a scheduler that skips a tier, a gateway that
# silently mis-decodes, a delta detector that misses a change.
#
# Suffixes under the package, not basenames: see the module docstring.
CRITICAL_MODULES = {
    "pronote_ng/ratelimit.py": 100.0,
    "pronote_ng/scheduler.py": 100.0,
    "pronote_ng/gateway.py": 100.0,
    "pronote_ng/delta.py": 100.0,
}


def _posix(filename: str) -> str:
    """Normalise a coverage.xml path so Windows and POSIX reports compare."""
    return Path(filename).as_posix().replace("\\", "/")


def _report_paths(root: ET.Element, filename: str) -> tuple[str, ...]:
    """Filenames as written, plus each ``<source>`` joined in front.

    ``pytest --cov=custom_components/pronote_ng`` writes Cobertura basenames
    (``ratelimit.py``) and puts the package directory in ``<source>``. A
    suffix lookup of ``pronote_ng/ratelimit.py`` only works after that join.
    A second ``ratelimit.py`` under ``connectors/ecoledirecte/`` stays a
    different path: it does not end with ``/pronote_ng/ratelimit.py``.
    """
    filename = _posix(filename)
    paths = [filename]
    for source in root.iter("source"):
        text = (source.text or "").strip()
        if text:
            paths.append(_posix(f"{text.rstrip('/')}/{filename}"))
    return tuple(paths)


def _module_coverage(root: ET.Element) -> dict[str, float]:
    """Return per-file coverage keyed by every path the report can name."""
    result: dict[str, float] = {}
    for class_element in root.iter("class"):
        filename = _posix(class_element.get("filename", ""))
        covered = total = 0
        for line in class_element.iter("line"):
            total += 1
            covered += 1 if line.get("hits", "0") != "0" else 0
            condition = line.get("condition-coverage")
            if condition and "(" in condition:
                fraction = condition.split("(", 1)[1].rstrip(")")
                taken, possible = (int(part) for part in fraction.split("/"))
                total += possible
                covered += taken
        percent = 100.0 * covered / total if total else 100.0
        for path in _report_paths(root, filename):
            result[path] = percent
    return result


def coverage_for(per_module: dict[str, float], required: str) -> float | None:
    """Look up ``required`` as an exact key or unique posix suffix.

    Two paths that share a basename do not match a short suffix such as
    ``pronote_ng/ratelimit.py``: ``pronote_ng/connectors/ecoledirecte/ratelimit.py``
    does not end with that string. Ambiguous matches count as missing so the
    gate fails closed.
    """
    required = _posix(required)
    if required in per_module:
        return per_module[required]
    suffix = required if required.startswith("/") else f"/{required}"
    matches = [
        value
        for name, value in per_module.items()
        if _posix(name) == required or _posix(name).endswith(suffix)
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_coverage.py <coverage.xml>", file=sys.stderr)
        return 2

    report = Path(argv[1])
    if not report.is_file():
        print(f"coverage report not found: {report}", file=sys.stderr)
        return 2

    root = ET.parse(report).getroot()  # noqa: S314 -- our own CI artifact

    failures: list[str] = []

    line_rate = float(root.get("line-rate", "0")) * 100.0
    branch_rate = float(root.get("branch-rate", "0")) * 100.0
    overall = min(line_rate, branch_rate) if branch_rate else line_rate
    if overall < GLOBAL_MINIMUM:
        failures.append(
            f"global coverage {overall:.2f}% is below the {GLOBAL_MINIMUM:.0f}% floor"
        )

    per_module = _module_coverage(root)
    for module, minimum in sorted(CRITICAL_MODULES.items()):
        actual = coverage_for(per_module, module)
        if actual is None:
            failures.append(f"{module} is absent from the coverage report")
        elif actual + 1e-9 < minimum:
            failures.append(
                f"{module} coverage {actual:.2f}% is below the required {minimum:.0f}%"
            )

    print(f"global: line {line_rate:.2f}% / branch {branch_rate:.2f}%")
    for module in sorted(CRITICAL_MODULES):
        value = coverage_for(per_module, module)
        shown = f"{value:.2f}%" if value is not None else "missing"
        print(f"  {module}: {shown}")

    if failures:
        print("\nCoverage gate failed:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print("\nCoverage gate passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
