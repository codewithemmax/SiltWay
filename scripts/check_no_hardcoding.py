"""Static check enforcing the no hard-coded edge cases rule across the repository."""

from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

SOURCE_DIRS = ("floodrouter", "service", "gateway", "web", "scripts")
SOURCE_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx"}
DOC_EXTENSIONS = {".md", ".yaml", ".yml", ".json", ".toml"}
IGNORED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    ".next",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "data",
    "dist",
    "build",
}
ALLOWED_MATHEMATICAL_FLOATS = {0.0, 0.5, 1.0, 2.0, 100.0, 1000.0}
ISO_DATE_RE = re.compile(r"\b(19|20)\d{2}-\d{2}-\d{2}\b")
FLOAT_LITERAL_RE = re.compile(r"(?<![A-Za-z0-9_])[-+]?\d+\.\d+(?![A-Za-z0-9_])")
EM_EN_DASH_RE = re.compile(r"[\u2013\u2014]")
# Coordinate-like literals that the float check misses, for example integer
# coordinates in frontend code: `lat: 7`, `lon = -3`, `center: [6, 7]`.
COORD_KEY_RE = re.compile(
    r"""(?<![A-Za-z0-9_])["']?(lat|lon|lng|latitude|longitude)["']?\s*[:=]\s*[-+]?\d""",
    re.IGNORECASE,
)
COORD_ARRAY_RE = re.compile(
    r"""(?<![A-Za-z0-9_])["']?(center|bbox|bounds|maxBounds)["']?\s*[:=(]\s*\[\s*\[?\s*[-+]?\d""",
)


@dataclass(frozen=True)
class Violation:
    """Single hard-coding or formatting rule violation."""

    file_path: str
    line: int
    rule: str
    detail: str


def load_forbidden_literals(path: str | Path) -> list[str]:
    """Load non-empty, non-comment forbidden literals from a text file."""
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Forbidden literals file not found: {file_path}")
    literals: list[str] = []
    with file_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            literals.append(line)
    return literals


def _is_lat_lon_like_float(value: float, raw_token: str | None = None) -> bool:
    """Return True if a float constant looks like a coordinate or tunable literal."""
    abs_val = abs(value)
    if abs_val in ALLOWED_MATHEMATICAL_FLOATS:
        return False
    if raw_token is not None and "." in raw_token:
        decimals = raw_token.split(".", 1)[1]
        if len(decimals) >= 2:
            return True
    return 0.0 < abs_val <= 180.0


def scan_text_for_violations(
    text: str,
    file_path: str | Path,
    forbidden_literals: list[str],
    check_floats_and_dates: bool = True,
) -> list[Violation]:
    """Scan a single file's text for forbidden literals, coordinate floats, dates, and dashes."""
    path_str = str(file_path)
    violations: list[Violation] = []
    lines = text.splitlines()

    compiled_patterns = [
        (
            lit,
            re.compile(
                r"(?<![A-Za-z0-9_])" + re.escape(lit) + r"(?![A-Za-z0-9_])",
            ),
        )
        for lit in forbidden_literals
    ]

    for idx, line in enumerate(lines, start=1):
        dash_match = EM_EN_DASH_RE.search(line)
        if dash_match:
            violations.append(
                Violation(
                    file_path=path_str,
                    line=idx,
                    rule="FORBIDDEN_DASH",
                    detail="Line contains an em dash or en dash character.",
                )
            )

        for lit, pattern in compiled_patterns:
            if pattern.search(line):
                violations.append(
                    Violation(
                        file_path=path_str,
                        line=idx,
                        rule="FORBIDDEN_LITERAL",
                        detail=f"Found forbidden literal '{lit}'.",
                    )
                )

        if check_floats_and_dates:
            coord_match = COORD_KEY_RE.search(line) or COORD_ARRAY_RE.search(line)
            if coord_match:
                violations.append(
                    Violation(
                        file_path=path_str,
                        line=idx,
                        rule="COORDINATE_LITERAL",
                        detail=f"Found coordinate-like literal '{coord_match.group(0)}'.",
                    )
                )
            date_match = ISO_DATE_RE.search(line)
            if date_match:
                violations.append(
                    Violation(
                        file_path=path_str,
                        line=idx,
                        rule="HARDCODED_DATE",
                        detail=f"Found hardcoded date literal '{date_match.group(0)}'.",
                    )
                )

    if not check_floats_and_dates:
        return violations

    suffix = Path(file_path).suffix.lower()
    if suffix == ".py":
        try:
            tree = ast.parse(text, filename=path_str)
        except SyntaxError:
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, float):
                    if _is_lat_lon_like_float(float(node.value)):
                        msg = f"Disallowed float literal {node.value} outside config/tests."
                        violations.append(
                            Violation(
                                file_path=path_str,
                                line=getattr(node, "lineno", 1),
                                rule="FLOAT_LITERAL",
                                detail=msg,
                            )
                        )
            return violations

    for idx, line in enumerate(lines, start=1):
        for match in FLOAT_LITERAL_RE.finditer(line):
            token = match.group(0)
            try:
                val = float(token)
            except ValueError:
                continue
            if _is_lat_lon_like_float(val, token):
                violations.append(
                    Violation(
                        file_path=path_str,
                        line=idx,
                        rule="FLOAT_LITERAL",
                        detail=f"Disallowed float literal {token} outside config/tests.",
                    )
                )

    return violations


def _iter_non_ignored_files(base_dir: Path) -> list[Path]:
    """Walk base_dir while pruning IGNORED_DIRS before descending into them."""
    collected: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(base_dir):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        cur = Path(dirpath)
        for fname in sorted(filenames):
            collected.append(cur / fname)
    return collected


def scan_repository(
    repo_root: str | Path,
    forbidden_literals_path: str | Path | None = None,
) -> list[Violation]:
    """Scan all source and documentation files in repo_root for violations."""
    root = Path(repo_root).resolve()
    lit_path = (
        Path(forbidden_literals_path).resolve()
        if forbidden_literals_path is not None
        else root / "config" / "forbidden_literals.txt"
    )
    forbidden_literals = load_forbidden_literals(lit_path)
    all_violations: list[Violation] = []
    this_script = Path(__file__).resolve()

    for source_dir_name in SOURCE_DIRS:
        source_dir = root / source_dir_name
        if not source_dir.exists():
            continue
        for file_path in _iter_non_ignored_files(source_dir):
            if file_path.suffix.lower() not in SOURCE_EXTENSIONS:
                continue
            text = file_path.read_text(encoding="utf-8")
            check_floats = file_path.resolve() != this_script
            rel = file_path.relative_to(root).as_posix()
            all_violations.extend(
                scan_text_for_violations(
                    text=text,
                    file_path=rel,
                    forbidden_literals=forbidden_literals,
                    check_floats_and_dates=check_floats,
                )
            )

    for file_path in _iter_non_ignored_files(root):
        if file_path.resolve() == lit_path:
            continue
        if file_path.suffix.lower() in DOC_EXTENSIONS:
            rel = file_path.relative_to(root).as_posix()
            text = file_path.read_text(encoding="utf-8")
            for idx, line in enumerate(text.splitlines(), start=1):
                if EM_EN_DASH_RE.search(line):
                    all_violations.append(
                        Violation(
                            file_path=rel,
                            line=idx,
                            rule="FORBIDDEN_DASH",
                            detail="Line contains an em dash or en dash character.",
                        )
                    )

    return all_violations


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for hard-coding check."""
    parser = argparse.ArgumentParser(description="Check repository for hardcoded literals.")
    parser.add_argument(
        "--root",
        default=".",
        help="Repository root directory.",
    )
    parser.add_argument(
        "--forbidden",
        default="config/forbidden_literals.txt",
        help="Path to forbidden literals file.",
    )
    args = parser.parse_args(argv)

    violations = scan_repository(args.root, args.forbidden)
    if violations:
        for v in violations:
            print(f"{v.file_path}:{v.line} [{v.rule}] {v.detail}", file=sys.stderr)
        print(f"Found {len(violations)} hard-coding/style violation(s).", file=sys.stderr)
        return 1

    print("No hard-coding violations found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
