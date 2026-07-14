#!/usr/bin/env python3
"""Run the low-tool-call XMind-to-test-case workbook pipeline."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from extract_xmind import (
    DEFAULT_CHUNK_CHARS,
    DEFAULT_MAX_CHUNKS_PER_PACKET,
    DEFAULT_PACKET_CHARS,
    ExtractionError,
    build_chunks,
    read_xmind,
    write_job,
)
from fix_freeze_panes import FreezePaneError, patch_workbook
from prepare_cases import (
    CaseValidationError,
    load_json,
    load_packets,
    load_schema,
    merge_files,
    validate_packet_file,
    write_json,
)


PACKET_BEGIN = "<<<NEXT_PACKET_BEGIN>>>"
PACKET_END = "<<<NEXT_PACKET_END>>>"


class PipelineError(RuntimeError):
    """Raised when the orchestrated workflow cannot continue safely."""


def _skill_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _validate_columns(columns: list[str] | None) -> list[str] | None:
    if columns is None:
        return None
    if not columns:
        raise PipelineError("自定义列不能为空")
    if not all(column and column.strip() == column for column in columns):
        raise PipelineError("每个自定义列名必须是首尾无空格的非空字符串")
    if len(columns) != len(set(columns)):
        raise PipelineError("自定义列名不能重复")
    return columns


def _job_file(job_dir: Path, relative_path: str) -> Path:
    job_dir = job_dir.resolve()
    candidate = (job_dir / relative_path).resolve()
    try:
        candidate.relative_to(job_dir)
    except ValueError as exc:
        raise PipelineError(f"任务文件路径越出工作目录：{relative_path}") from exc
    return candidate


def _load_job(job_dir: Path) -> tuple[Path, Path, dict[str, dict[str, Any]], list[str]]:
    job_dir = job_dir.resolve()
    if not job_dir.is_dir():
        raise PipelineError(f"任务目录不存在：{job_dir}")
    manifest_path = job_dir / "manifest.json"
    schema_path = job_dir / "schema.json"
    packets, packet_order = load_packets(manifest_path)
    load_schema(schema_path)
    for packet_id in packet_order:
        _job_file(job_dir, packets[packet_id]["file"])
    return manifest_path, schema_path, packets, packet_order


def _next_incomplete_packet(
    job_dir: Path,
    manifest_path: Path,
    schema_path: Path,
    packets: dict[str, dict[str, Any]],
    packet_order: list[str],
) -> tuple[dict[str, Any] | None, int]:
    cases_dir = job_dir / "cases"
    completed = 0
    for packet_id in packet_order:
        canonical = cases_dir / f"{packet_id}.json"
        if not canonical.is_file():
            return packets[packet_id], completed
        try:
            validate_packet_file(
                canonical,
                manifest_path,
                schema_path,
                expected_packet_id=packet_id,
            )
        except CaseValidationError:
            return packets[packet_id], completed
        completed += 1
    return None, completed


def _emit(summary: dict[str, Any], job_dir: Path, packet: dict[str, Any] | None) -> None:
    if packet is None:
        summary["next_packet"] = None
        print(json.dumps(summary, ensure_ascii=False))
        return

    packet_path = _job_file(job_dir, packet["file"])
    summary["next_packet"] = {
        "packet_id": packet["id"],
        "chunk_ids": packet["chunk_ids"],
        "char_count": packet.get("char_count"),
        "source": str(packet_path),
        "result_path": str(job_dir / "draft.json"),
    }
    print(json.dumps(summary, ensure_ascii=False))
    print(PACKET_BEGIN)
    print(packet_path.read_text(encoding="utf-8"), end="")
    print(PACKET_END)


def prepare_job(args: argparse.Namespace) -> None:
    columns = _validate_columns(args.column)
    if args.work_dir is None:
        job_dir = Path(tempfile.mkdtemp(prefix="xmind-testcase-"))
    else:
        job_dir = args.work_dir.expanduser().resolve()

    package_format, sheets = read_xmind(args.input.expanduser().resolve())
    chunks = build_chunks(sheets, args.chunk_chars)
    manifest_path = write_job(
        input_path=args.input.expanduser().resolve(),
        work_dir=job_dir,
        package_format=package_format,
        sheets=sheets,
        chunks=chunks,
        chunk_chars=args.chunk_chars,
        packet_chars=args.packet_chars,
        max_chunks_per_packet=args.max_chunks_per_packet,
    )

    schema_path = job_dir / "schema.json"
    if columns is None:
        shutil.copyfile(_skill_dir() / "references" / "default-schema.json", schema_path)
    else:
        write_json(schema_path, {"columns": columns})
    resolved_columns = load_schema(schema_path)
    (job_dir / "cases").mkdir(exist_ok=True)

    manifest = load_json(manifest_path)
    packets, packet_order = load_packets(manifest_path)
    oversized_leaf_count = int(manifest.get("oversized_leaf_count", 0))
    oversized_packet_count = sum(bool(packet.get("oversized")) for packet in packets.values())
    warnings: list[str] = []
    if oversized_leaf_count:
        warnings.append(f"存在 {oversized_leaf_count} 个超长叶子节点，请确认单批是否能放入上下文")
    if oversized_packet_count:
        warnings.append(f"存在 {oversized_packet_count} 个超过 Packet 字符上限的独立批次")

    _emit(
        {
            "status": "ready",
            "job_dir": str(job_dir.resolve()),
            "manifest": str(manifest_path.resolve()),
            "schema": str(schema_path.resolve()),
            "columns": resolved_columns,
            "chunk_count": len(chunks),
            "packet_count": len(packet_order),
            "completed_packet_count": 0,
            "warnings": warnings,
        },
        job_dir.resolve(),
        packets[packet_order[0]],
    )


def validate_packet(args: argparse.Namespace) -> None:
    job_dir = args.job_dir.expanduser().resolve()
    manifest_path, schema_path, packets, packet_order = _load_job(job_dir)
    expected, completed = _next_incomplete_packet(
        job_dir,
        manifest_path,
        schema_path,
        packets,
        packet_order,
    )
    if expected is None:
        raise PipelineError("全部 Packet 已经校验完成，无需继续提交")

    normalized = validate_packet_file(
        args.input.expanduser().resolve(),
        manifest_path,
        schema_path,
        expected_packet_id=expected["id"],
    )
    canonical = job_dir / "cases" / f"{expected['id']}.json"
    write_json(canonical, normalized)
    next_packet, completed_after = _next_incomplete_packet(
        job_dir,
        manifest_path,
        schema_path,
        packets,
        packet_order,
    )
    _emit(
        {
            "status": "valid",
            "job_dir": str(job_dir),
            "packet_id": expected["id"],
            "chunk_count": len(normalized["chunk_results"]),
            "case_count": sum(
                len(chunk_result["test_cases"])
                for chunk_result in normalized["chunk_results"]
            ),
            "completed_packet_count": completed_after,
            "packet_count": len(packet_order),
        },
        job_dir,
        next_packet,
    )


def _parse_builder_output(stdout: str) -> dict[str, Any]:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise PipelineError("Excel 构建器没有返回结果")
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise PipelineError("Excel 构建器返回了无效 JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("output"), str):
        raise PipelineError("Excel 构建器结果缺少 output")
    return payload


def _ensure_node_modules_link(link: Path, target: Path) -> None:
    target = target.expanduser().resolve()
    if not target.is_dir():
        raise PipelineError(f"Node 包目录不存在：{target}")
    if link.is_symlink():
        if link.resolve() != target:
            raise PipelineError(f"已有 node_modules 链接指向其他目录：{link}")
        return
    if link.exists():
        raise PipelineError(f"构建目录中已存在非链接 node_modules：{link}")
    link.symlink_to(target, target_is_directory=True)


def finalize_job(args: argparse.Namespace) -> None:
    job_dir = args.job_dir.expanduser().resolve()
    manifest_path, schema_path, packets, packet_order = _load_job(job_dir)
    missing, completed = _next_incomplete_packet(
        job_dir,
        manifest_path,
        schema_path,
        packets,
        packet_order,
    )
    if missing is not None:
        raise PipelineError(
            f"仍有未校验的 Packet：{missing['id']}（已完成 {completed}/{len(packet_order)}）"
        )

    merged_path = job_dir / "merged.json"
    merged = merge_files(manifest_path, schema_path, job_dir / "cases")
    write_json(merged_path, merged)

    node = args.node.expanduser().resolve()
    if not node.is_file():
        raise PipelineError(f"Node 可执行文件不存在：{node}")
    runner_dir = job_dir / "workbook-builder"
    runner_dir.mkdir(exist_ok=True)
    _ensure_node_modules_link(runner_dir / "node_modules", args.node_modules)
    builder = runner_dir / "build_workbook.mjs"
    shutil.copy2(_skill_dir() / "scripts" / "build_workbook.mjs", builder)

    preview = args.preview.expanduser().resolve() if args.preview else job_dir / "preview.png"
    report = job_dir / "workbook-report.json"
    command = [
        str(node),
        str(builder),
        "--input",
        str(merged_path),
        "--output",
        str(args.output.expanduser().resolve()),
        "--preview",
        str(preview),
        "--report",
        str(report),
    ]
    if args.overwrite:
        command.append("--overwrite")
    completed_process = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed_process.returncode != 0:
        detail = (completed_process.stderr or completed_process.stdout).strip()
        raise PipelineError(f"Excel 构建失败：{detail[-4_000:]}")

    builder_result = _parse_builder_output(completed_process.stdout)
    actual_output = Path(builder_result["output"]).resolve()
    patch_workbook(actual_output)
    formula_error_count = int(builder_result.get("formula_error_count", 0))
    if formula_error_count:
        raise PipelineError(
            f"Excel 公式错误扫描发现 {formula_error_count} 条异常，详情见 {report}"
        )

    print(
        json.dumps(
            {
                "status": "complete",
                "output": str(actual_output),
                "preview": str(Path(builder_result["preview"]).resolve()),
                "report": str(report.resolve()),
                "sheet": builder_result.get("sheet"),
                "column_count": builder_result.get("column_count"),
                "case_count": builder_result.get("case_count"),
                "inspection_record_count": builder_result.get("inspection_record_count"),
                "formula_error_count": formula_error_count,
                "first_row_frozen": True,
            },
            ensure_ascii=False,
        )
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="低工具调用的 XMind 测试用例 Excel 流水线")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="解析 XMind 并输出首个需求 Packet")
    prepare.add_argument("input", type=Path)
    prepare.add_argument("--work-dir", type=Path)
    prepare.add_argument("--chunk-chars", type=int, default=DEFAULT_CHUNK_CHARS)
    prepare.add_argument("--packet-chars", type=int, default=DEFAULT_PACKET_CHARS)
    prepare.add_argument(
        "--max-chunks-per-packet",
        type=int,
        default=DEFAULT_MAX_CHUNKS_PER_PACKET,
    )
    prepare.add_argument("--column", action="append", help="自定义列名；按目标顺序重复传入")

    validate = subparsers.add_parser("validate", help="校验当前 Packet 并输出下一个 Packet")
    validate.add_argument("--job-dir", type=Path, required=True)
    validate.add_argument("--input", type=Path, required=True)

    finalize = subparsers.add_parser("finalize", help="合并并构建最终 Excel")
    finalize.add_argument("--job-dir", type=Path, required=True)
    finalize.add_argument("--node", type=Path, required=True)
    finalize.add_argument("--node-modules", type=Path, required=True)
    finalize.add_argument("--output", type=Path, required=True)
    finalize.add_argument("--preview", type=Path)
    finalize.add_argument("--overwrite", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "prepare":
            prepare_job(args)
        elif args.command == "validate":
            validate_packet(args)
        else:
            finalize_job(args)
    except (PipelineError, ExtractionError, CaseValidationError, FreezePaneError, OSError) as exc:
        print(f"流水线失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
