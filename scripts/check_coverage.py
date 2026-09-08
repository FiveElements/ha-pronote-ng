#!/usr/bin/env python3
"""Enforce the coverage thresholds of SPECIFICATION.md §11.

A threshold that does not fail is not a threshold: this script exits non-zero
when the global line+branch coverage drops below 80 %, or when any of the four
modules where a mistake is invisible at runtime drops below 100 %.
"""

from __future__ import annotations

from pathlib import Path
import sys
import xml.etree.ElementTree as ET

GLOBAL_MINIMUM = 80.0

# The four modules where an error produces no visible symptom: a rate limiter
# that lets a call through, a scheduler that skips a tier, a gateway that
# silently mis-decodes, a delta detector that misses a change.
CRITICAL_MODULES = {
    "ratelimit.py": 100.0,
    "scheduler.py": 100.0,
    "gateway.py": 100.0,
    "delta.py": 100.0,
}


def _module_coverage(root: ET.Element) -> dict[str, float]:
    """Return per-file coverage, counting both lines and branches."""
    result: dict[str, float] = {}
    for class_element in root.iter("class"):
        filename = class_element.get("filename", "")
        name = Path(filename).name
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
        result[name] = 100.0 * covered / total if total else 100.0
    return result


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
        actual = per_module.get(module)
        if actual is None:
            failures.append(f"{module} is absent from the coverage report")
        elif actual + 1e-9 < minimum:
            failures.append(
                f"{module} coverage {actual:.2f}% is below the required {minimum:.0f}%"
            )

    print(f"global: line {line_rate:.2f}% / branch {branch_rate:.2f}%")
    for module in sorted(CRITICAL_MODULES):
        value = per_module.get(module)
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
