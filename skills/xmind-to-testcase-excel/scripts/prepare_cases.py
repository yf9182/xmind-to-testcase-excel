#!/usr/bin/env python3
"""Validate per-chunk AI test cases and merge them into one strict JSON file."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable


PENDING_PATTERN = re.compile(r"需要确认|待确认|下版本")
PRIORITIES = {"P0", "P1", "P2", "P3"}


class CaseValidationError(RuntimeError):
    """Raised when model-produced test cases violate the file contract."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CaseValidationError(f"JSON 中存在重复字段：{key}")
        result[key] = value
    return result


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except OSError as exc:
        raise CaseValidationError(f"无法读取 {path}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise CaseValidationError(f"{path} 不是有效 JSON：{exc.msg}（第 {exc.lineno} 行）") from exc


def load_schema(path: Path) -> list[str]:
    payload = load_json(path)
    if not isinstance(payload, dict) or set(payload) != {"columns"}:
        raise CaseValidationError("schema 顶层必须且只能包含 columns")
    raw_columns = payload["columns"]
    if not isinstance(raw_columns, list) or not raw_columns:
        raise CaseValidationError("schema.columns 必须是非空数组")
    if not all(isinstance(column, str) and column.strip() == column and column for column in raw_columns):
        raise CaseValidationError("每个列名必须是首尾无空格的非空字符串")
    if len(raw_columns) != len(set(raw_columns)):
        raise CaseValidationError("schema.columns 中存在重复列名")
    return list(raw_columns)


def load_manifest(path: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    payload = load_json(path)
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise CaseValidationError("manifest.version 必须为 1")
    raw_chunks = payload.get("chunks")
    if not isinstance(raw_chunks, list) or not raw_chunks:
        raise CaseValidationError("manifest.chunks 必须是非空数组")

    chunks: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for index, raw in enumerate(raw_chunks, start=1):
        if not isinstance(raw, dict):
            raise CaseValidationError(f"manifest 第 {index} 个分块不是对象")
        chunk_id = raw.get("id")
        if not isinstance(chunk_id, str) or not re.fullmatch(r"chunk-\d{4}", chunk_id):
            raise CaseValidationError(f"manifest 第 {index} 个分块 id 无效")
        if chunk_id in chunks:
            raise CaseValidationError(f"manifest 中存在重复分块：{chunk_id}")
        if not isinstance(raw.get("module"), str) or not raw["module"].strip():
            raise CaseValidationError(f"{chunk_id} 缺少 module")
        if not isinstance(raw.get("file"), str) or not raw["file"]:
            raise CaseValidationError(f"{chunk_id} 缺少 file")
        chunks[chunk_id] = raw
        order.append(chunk_id)
    return chunks, order


def load_packets(path: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    chunks, chunk_order = load_manifest(path)
    payload = load_json(path)
    raw_packets = payload.get("packets") if isinstance(payload, dict) else None
    if raw_packets is None:
        packets = {
            f"packet-{index:04d}": {
                "id": f"packet-{index:04d}",
                "file": chunks[chunk_id]["file"],
                "chunk_ids": [chunk_id],
            }
            for index, chunk_id in enumerate(chunk_order, start=1)
        }
        return packets, list(packets)
    if not isinstance(raw_packets, list) or not raw_packets:
        raise CaseValidationError("manifest.packets 必须是非空数组")

    packets: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    flattened: list[str] = []
    for index, raw in enumerate(raw_packets, start=1):
        if not isinstance(raw, dict):
            raise CaseValidationError(f"manifest 第 {index} 个 Packet 不是对象")
        packet_id = raw.get("id")
        if not isinstance(packet_id, str) or not re.fullmatch(r"packet-\d{4}", packet_id):
            raise CaseValidationError(f"manifest 第 {index} 个 Packet id 无效")
        if packet_id in packets:
            raise CaseValidationError(f"manifest 中存在重复 Packet：{packet_id}")
        if not isinstance(raw.get("file"), str) or not raw["file"]:
            raise CaseValidationError(f"{packet_id} 缺少 file")
        chunk_ids = raw.get("chunk_ids")
        if not isinstance(chunk_ids, list) or not chunk_ids:
            raise CaseValidationError(f"{packet_id}.chunk_ids 必须是非空数组")
        if not all(isinstance(chunk_id, str) and chunk_id in chunks for chunk_id in chunk_ids):
            raise CaseValidationError(f"{packet_id} 包含未知 chunk_id")
        if len(chunk_ids) != len(set(chunk_ids)):
            raise CaseValidationError(f"{packet_id} 包含重复 chunk_id")
        normalized = dict(raw)
        normalized["chunk_ids"] = list(chunk_ids)
        packets[packet_id] = normalized
        order.append(packet_id)
        flattened.extend(chunk_ids)

    if flattened != chunk_order:
        raise CaseValidationError("manifest.packets 必须按顺序且不重不漏地覆盖全部分块")
    return packets, order


def _source_markdown(manifest_path: Path, chunk: dict[str, Any]) -> str:
    base = manifest_path.parent.resolve()
    source = (base / chunk["file"]).resolve()
    try:
        source.relative_to(base)
    except ValueError as exc:
        raise CaseValidationError("manifest 的分块路径越出任务目录") from exc
    try:
        return source.read_text(encoding="utf-8")
    except OSError as exc:
        raise CaseValidationError(f"无法读取需求分块 {source}：{exc}") from exc


def validate_result(
    payload: Any,
    columns: list[str],
    chunk: dict[str, Any],
    source_markdown: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {"chunk_id", "test_cases"}:
        raise CaseValidationError("分块 JSON 顶层必须且只能包含 chunk_id 和 test_cases")
    if payload["chunk_id"] != chunk["id"]:
        raise CaseValidationError(
            f"chunk_id 不匹配：期望 {chunk['id']}，实际 {payload['chunk_id']!r}"
        )
    raw_cases = payload["test_cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise CaseValidationError(f"{chunk['id']} 的 test_cases 必须是非空数组")

    column_set = set(columns)
    normalized_cases: list[dict[str, str]] = []
    for index, raw_case in enumerate(raw_cases, start=1):
        if not isinstance(raw_case, dict):
            raise CaseValidationError(f"{chunk['id']} 第 {index} 条用例不是对象")
        if set(raw_case) != column_set:
            missing = sorted(column_set - set(raw_case))
            extra = sorted(set(raw_case) - column_set)
            raise CaseValidationError(
                f"{chunk['id']} 第 {index} 条用例字段与 schema 不一致；缺少={missing}，多出={extra}"
            )
        if not all(isinstance(value, str) for value in raw_case.values()):
            raise CaseValidationError(f"{chunk['id']} 第 {index} 条用例的所有值必须是字符串")

        case = {column: raw_case[column].strip() for column in columns}
        if not any(case.values()):
            raise CaseValidationError(f"{chunk['id']} 第 {index} 条用例为空")
        if "用例ID" in columns and case["用例ID"]:
            raise CaseValidationError(f"{chunk['id']} 第 {index} 条用例的用例ID必须留空")
        if "模块" in columns and not case["模块"]:
            case["模块"] = chunk["module"].strip()
        for required in ("模块", "用例标题", "预期结果"):
            if required in columns and not case[required]:
                raise CaseValidationError(f"{chunk['id']} 第 {index} 条用例的{required}不能为空")
        if "优先级" in columns and case["优先级"] not in PRIORITIES:
            raise CaseValidationError(
                f"{chunk['id']} 第 {index} 条用例的优先级必须是 P0、P1、P2 或 P3"
            )
        normalized_cases.append(case)

    if PENDING_PATTERN.search(source_markdown):
        combined = "\n".join(value for case in normalized_cases for value in case.values())
        if "待确认" not in combined:
            raise CaseValidationError(
                f"{chunk['id']} 的需求含未定信息，但生成结果没有保留“待确认”标记"
            )
    return {"chunk_id": chunk["id"], "test_cases": normalized_cases}


def validate_file(
    input_path: Path,
    manifest_path: Path,
    schema_path: Path,
) -> dict[str, Any]:
    columns = load_schema(schema_path)
    chunks, _ = load_manifest(manifest_path)
    payload = load_json(input_path)
    if not isinstance(payload, dict):
        raise CaseValidationError("分块 JSON 顶层必须是对象")
    chunk_id = payload.get("chunk_id")
    if not isinstance(chunk_id, str) or chunk_id not in chunks:
        raise CaseValidationError(f"分块 JSON 包含未知 chunk_id：{chunk_id!r}")
    chunk = chunks[chunk_id]
    return validate_result(payload, columns, chunk, _source_markdown(manifest_path, chunk))


def validate_packet_result(
    payload: Any,
    columns: list[str],
    chunks: dict[str, dict[str, Any]],
    packet: dict[str, Any],
    manifest_path: Path,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {"packet_id", "chunk_results"}:
        raise CaseValidationError("Packet JSON 顶层必须且只能包含 packet_id 和 chunk_results")
    if payload["packet_id"] != packet["id"]:
        raise CaseValidationError(
            f"packet_id 不匹配：期望 {packet['id']}，实际 {payload['packet_id']!r}"
        )
    raw_results = payload["chunk_results"]
    if not isinstance(raw_results, list) or not raw_results:
        raise CaseValidationError(f"{packet['id']} 的 chunk_results 必须是非空数组")

    actual_ids: list[str] = []
    for index, raw_result in enumerate(raw_results, start=1):
        if not isinstance(raw_result, dict):
            raise CaseValidationError(f"{packet['id']} 第 {index} 个 chunk_result 不是对象")
        chunk_id = raw_result.get("chunk_id")
        if not isinstance(chunk_id, str):
            raise CaseValidationError(f"{packet['id']} 第 {index} 个 chunk_result 缺少 chunk_id")
        actual_ids.append(chunk_id)

    expected_ids = packet["chunk_ids"]
    duplicates = sorted({chunk_id for chunk_id in actual_ids if actual_ids.count(chunk_id) > 1})
    missing = [chunk_id for chunk_id in expected_ids if chunk_id not in actual_ids]
    extra = [chunk_id for chunk_id in actual_ids if chunk_id not in expected_ids]
    if duplicates:
        raise CaseValidationError(f"{packet['id']} 包含重复分块结果：{', '.join(duplicates)}")
    if missing or extra:
        raise CaseValidationError(
            f"{packet['id']} 分块结果不完整；缺少={missing}，多出={extra}"
        )
    if actual_ids != expected_ids:
        raise CaseValidationError(f"{packet['id']} 的 chunk_results 顺序必须与 Packet 一致")

    normalized: list[dict[str, Any]] = []
    for raw_result, chunk_id in zip(raw_results, expected_ids):
        chunk = chunks[chunk_id]
        normalized.append(
            validate_result(
                raw_result,
                columns,
                chunk,
                _source_markdown(manifest_path, chunk),
            )
        )
    return {"packet_id": packet["id"], "chunk_results": normalized}


def validate_packet_file(
    input_path: Path,
    manifest_path: Path,
    schema_path: Path,
    expected_packet_id: str | None = None,
) -> dict[str, Any]:
    columns = load_schema(schema_path)
    chunks, _ = load_manifest(manifest_path)
    packets, _ = load_packets(manifest_path)
    payload = load_json(input_path)
    if not isinstance(payload, dict):
        raise CaseValidationError("Packet JSON 顶层必须是对象")
    packet_id = payload.get("packet_id")
    if not isinstance(packet_id, str) or packet_id not in packets:
        raise CaseValidationError(f"Packet JSON 包含未知 packet_id：{packet_id!r}")
    if expected_packet_id is not None and packet_id != expected_packet_id:
        raise CaseValidationError(
            f"当前应生成 {expected_packet_id}，实际收到 {packet_id}"
        )
    return validate_packet_result(payload, columns, chunks, packets[packet_id], manifest_path)


def merge_files(
    manifest_path: Path,
    schema_path: Path,
    cases_dir: Path,
) -> dict[str, Any]:
    columns = load_schema(schema_path)
    chunks, order = load_manifest(manifest_path)
    if not cases_dir.is_dir():
        raise CaseValidationError(f"用例目录不存在：{cases_dir}")

    results: dict[str, dict[str, Any]] = {}
    packets, _ = load_packets(manifest_path)
    for path in sorted(cases_dir.glob("*.json")):
        payload = load_json(path)
        if not isinstance(payload, dict):
            raise CaseValidationError(f"{path} 顶层必须是对象")
        if "packet_id" in payload:
            packet_id = payload.get("packet_id")
            if not isinstance(packet_id, str) or packet_id not in packets:
                raise CaseValidationError(f"{path} 包含未知 packet_id：{packet_id!r}")
            packet_result = validate_packet_result(
                payload,
                columns,
                chunks,
                packets[packet_id],
                manifest_path,
            )
            for chunk_result in packet_result["chunk_results"]:
                chunk_id = chunk_result["chunk_id"]
                if chunk_id in results:
                    raise CaseValidationError(f"存在多个 {chunk_id} 结果文件")
                results[chunk_id] = chunk_result
        else:
            chunk_id = payload.get("chunk_id")
            if not isinstance(chunk_id, str) or chunk_id not in chunks:
                raise CaseValidationError(f"{path} 包含未知 chunk_id：{chunk_id!r}")
            if chunk_id in results:
                raise CaseValidationError(f"存在多个 {chunk_id} 结果文件")
            chunk = chunks[chunk_id]
            results[chunk_id] = validate_result(
                payload,
                columns,
                chunk,
                _source_markdown(manifest_path, chunk),
            )

    missing = [chunk_id for chunk_id in order if chunk_id not in results]
    if missing:
        raise CaseValidationError(f"缺少分块结果：{', '.join(missing)}")

    id_column = "用例ID" if "用例ID" in columns else None
    signature_columns = [column for column in columns if column != id_column]
    seen: set[tuple[str, ...]] = set()
    merged: list[dict[str, str]] = []
    for chunk_id in order:
        for case in results[chunk_id]["test_cases"]:
            signature = tuple(case[column] for column in signature_columns)
            if signature in seen:
                continue
            seen.add(signature)
            row = dict(case)
            if id_column:
                row[id_column] = f"TC-{len(merged) + 1:04d}"
            merged.append(row)

    if not merged:
        raise CaseValidationError("合并后没有可用测试用例")
    return {"columns": columns, "test_cases": merged}


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="校验并合并 AI 生成的测试用例 JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-chunk", help="校验单个分块结果")
    validate.add_argument("--manifest", type=Path, required=True)
    validate.add_argument("--schema", type=Path, required=True)
    validate.add_argument("--input", type=Path, required=True)

    validate_packet = subparsers.add_parser("validate-packet", help="校验一个 Packet 结果")
    validate_packet.add_argument("--manifest", type=Path, required=True)
    validate_packet.add_argument("--schema", type=Path, required=True)
    validate_packet.add_argument("--input", type=Path, required=True)

    merge = subparsers.add_parser("merge", help="校验并合并全部分块结果")
    merge.add_argument("--manifest", type=Path, required=True)
    merge.add_argument("--schema", type=Path, required=True)
    merge.add_argument("--cases-dir", type=Path, required=True)
    merge.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "validate-chunk":
            result = validate_file(args.input, args.manifest, args.schema)
            print(
                json.dumps(
                    {"chunk_id": result["chunk_id"], "case_count": len(result["test_cases"]), "valid": True},
                    ensure_ascii=False,
                )
            )
        elif args.command == "validate-packet":
            result = validate_packet_file(args.input, args.manifest, args.schema)
            print(
                json.dumps(
                    {
                        "packet_id": result["packet_id"],
                        "chunk_count": len(result["chunk_results"]),
                        "case_count": sum(
                            len(chunk_result["test_cases"])
                            for chunk_result in result["chunk_results"]
                        ),
                        "valid": True,
                    },
                    ensure_ascii=False,
                )
            )
        else:
            result = merge_files(args.manifest, args.schema, args.cases_dir)
            write_json(args.output, result)
            print(
                json.dumps(
                    {
                        "output": str(args.output),
                        "column_count": len(result["columns"]),
                        "case_count": len(result["test_cases"]),
                    },
                    ensure_ascii=False,
                )
            )
    except (CaseValidationError, OSError) as exc:
        print(f"校验失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
