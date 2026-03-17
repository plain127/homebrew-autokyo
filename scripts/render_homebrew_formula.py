#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from autokyo import __version__


def build_formula(*, version: str, sha256: str, python_formula: str, homepage: str) -> str:
    return f'''class Autokyo < Formula
  include Language::Python::Virtualenv

  desc "macOS local automation tool for page-by-page ebook viewer workflows"
  homepage "{homepage}"
  url "{homepage}/archive/refs/tags/v{version}.tar.gz"
  sha256 "{sha256}"

  depends_on "{python_formula}"

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "autokyo", shell_output("#{{bin}}/autokyo --help")
  end
end
'''


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render a Homebrew formula for a tagged AutoKyo release.",
    )
    parser.add_argument(
        "--version",
        default=__version__,
        help=f"Release version without the leading v. Defaults to {__version__}",
    )
    parser.add_argument(
        "--sha256",
        required=True,
        help="SHA256 of the GitHub release source archive",
    )
    parser.add_argument(
        "--python-formula",
        default="python@3.12",
        help='Homebrew Python dependency. Defaults to "python@3.12"',
    )
    parser.add_argument(
        "--homepage",
        default="https://github.com/plain127/homebrew-autokyo",
        help="GitHub repository homepage",
    )
    parser.add_argument(
        "--output",
        default="Formula/autokyo.rb",
        help='Output path. Defaults to "Formula/autokyo.rb"',
    )
    args = parser.parse_args()

    formula = build_formula(
        version=args.version,
        sha256=args.sha256,
        python_formula=args.python_formula,
        homepage=args.homepage.rstrip("/"),
    )

    if args.output == "-":
        print(formula, end="")
        return 0

    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(formula, encoding="utf-8")
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
