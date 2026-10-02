"""按期回收命令行工具。

用法（项目根目录执行）::

    python scripts/maintenance_cli.py --dry-run
    python scripts/maintenance_cli.py --apply
    python scripts/maintenance_cli.py --apply --sections checkpoints,logs
    python scripts/maintenance_cli.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from maintenance import MaintenancePolicy, run_maintenance  # noqa: E402

SECTIONS = ("checkpoints", "artifacts", "logs", "temp")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行时资源定期回收")
    parser.add_argument("--apply", action="store_true", help="真正删除；不传则仅预览（dry-run）")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不删除（默认行为）")
    parser.add_argument(
        "--sections",
        default=",".join(SECTIONS),
        help=f"要处理的节，逗号分隔，可选: {','.join(SECTIONS)}",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    parser.add_argument("--project-dir", default="", help="项目根目录（默认当前目录）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sections = [s.strip() for s in args.sections.split(",") if s.strip()]
    invalid = [s for s in sections if s not in SECTIONS]
    if invalid:
        print(f"未知回收节: {', '.join(invalid)}", file=sys.stderr)
        return 2

    policy = MaintenancePolicy.from_env(args.project_dir or None)
    dry_run = not args.apply or args.dry_run
    report = run_maintenance(policy, dry_run=dry_run, sections=sections)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.summary())
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
