from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills" / "xmind-to-testcase-excel"
SCRIPTS = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS))

import pipeline  # noqa: E402
from extract_xmind import (  # noqa: E402
    ChunkDraft,
    Sheet,
    Topic,
    build_chunks,
    build_packets,
    read_xmind,
)
from prepare_cases import (  # noqa: E402
    CaseValidationError,
    load_json,
    merge_files,
    validate_file,
    validate_packet_file,
    write_json,
)


DEFAULT_COLUMNS = load_json(SKILL_DIR / "references" / "default-schema.json")["columns"]
MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _json_topic(title: str, children: list[dict] | None = None) -> dict:
    topic = {"title": title}
    if children:
        topic["children"] = {"attached": children}
    return topic


def write_json_xmind(path: Path, modules: list[str]) -> None:
    payload = [
        {
            "title": "需求",
            "rootTopic": _json_topic(
                "测试需求",
                [_json_topic(module, [_json_topic("正常流程")]) for module in modules],
            ),
        }
    ]
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("content.json", json.dumps(payload, ensure_ascii=False))


def write_xml_xmind(path: Path) -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<xmap-content>
  <sheet>
    <title>需求</title>
    <topic><title>测试需求</title><children><topics>
      <topic><title>登录</title><children><topics><topic><title>正常流程</title></topic></topics></children></topic>
    </topics></children></topic>
  </sheet>
</xmap-content>
"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("content.xml", xml)


def run_pipeline(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = pipeline.main(argv)
    return result, stdout.getvalue(), stderr.getvalue()


def prepare_job(base: Path, modules: list[str], extra: list[str] | None = None) -> tuple[Path, dict]:
    xmind = base / "requirements.xmind"
    job = base / "job"
    write_json_xmind(xmind, modules)
    argv = ["prepare", str(xmind), "--work-dir", str(job)]
    if extra:
        argv.extend(extra)
    code, stdout, stderr = run_pipeline(argv)
    if code != 0:
        raise AssertionError(stderr)
    return job, json.loads(stdout.splitlines()[0])


def default_case(chunk_id: str, pending: bool = False) -> dict[str, str]:
    case = {column: "" for column in DEFAULT_COLUMNS}
    case.update(
        {
            "模块": "测试模块",
            "功能点": "功能",
            "用例标题": f"{chunk_id} 正常流程",
            "前置条件": "系统可用",
            "测试步骤": "1. 执行操作",
            "测试数据": "有效数据",
            "预期结果": "操作成功",
            "优先级": "P1",
            "用例类型": "功能测试",
            "备注": "待确认：优惠规则" if pending else "",
        }
    )
    return case


def packet_payload(job: Path, packet_id: str, pending: bool = False) -> dict:
    manifest = load_json(job / "manifest.json")
    packet = next(item for item in manifest["packets"] if item["id"] == packet_id)
    return {
        "packet_id": packet_id,
        "chunk_results": [
            {"chunk_id": chunk_id, "test_cases": [default_case(chunk_id, pending)]}
            for chunk_id in packet["chunk_ids"]
        ],
    }


class ExtractionAndPacketTests(unittest.TestCase):
    def test_reads_json_and_xml_xmind(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            json_path = base / "json.xmind"
            xml_path = base / "xml.xmind"
            write_json_xmind(json_path, ["登录"])
            write_xml_xmind(xml_path)
            self.assertEqual(read_xmind(json_path)[0], "content.json")
            self.assertEqual(read_xmind(xml_path)[0], "content.xml")
            self.assertEqual(read_xmind(json_path)[1][0].root.children[0].title, "登录")
            self.assertEqual(read_xmind(xml_path)[1][0].root.children[0].title, "登录")

    def test_adaptive_packet_limits_and_twelve_chunk_budget(self) -> None:
        sheet = Sheet(index=1, title="需求", root=Topic("测试需求"))
        chunks = [ChunkDraft(sheet, f"模块{index}", ("测试需求",), f"- 模块{index}") for index in range(12)]
        packets = build_packets(chunks)
        self.assertEqual(len(packets), 4)
        self.assertTrue(all(len(packet.chunks) <= 3 for packet in packets))

        large = [ChunkDraft(sheet, str(index), ("测试需求",), "- " + "字" * 800) for index in range(3)]
        bounded = build_packets(large, packet_chars=1_800, max_chunks_per_packet=3)
        self.assertEqual(len(bounded), 3)

    def test_oversized_leaf_is_preserved_in_one_packet(self) -> None:
        sheet = Sheet(
            index=1,
            title="需求",
            root=Topic("测试需求", [Topic("模块", [Topic("字" * 2_000)])]),
        )
        chunks = build_chunks([sheet], max_chars=1_000)
        self.assertEqual(len(chunks), 1)
        self.assertTrue(chunks[0].oversized)
        self.assertEqual(len(build_packets(chunks)), 1)


class PrepareAndValidationTests(unittest.TestCase):
    def test_prepare_streams_first_packet_and_supports_custom_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            job, summary = prepare_job(base, ["登录", "支付"])
            self.assertEqual(summary["columns"], DEFAULT_COLUMNS)
            self.assertEqual(summary["packet_count"], 1)
            self.assertEqual(summary["next_packet"]["packet_id"], "packet-0001")
            self.assertTrue((job / "packets" / "packet-0001.md").is_file())

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            _, custom = prepare_job(
                base,
                ["登录"],
                ["--column", "标题", "--column", "结果"],
            )
            self.assertEqual(custom["columns"], ["标题", "结果"])

    def test_packet_contract_rejects_missing_duplicate_order_and_bad_cases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job, _ = prepare_job(Path(temporary), ["登录", "支付"])
            manifest = job / "manifest.json"
            schema = job / "schema.json"
            source = packet_payload(job, "packet-0001")
            draft = job / "draft.json"

            invalid = copy.deepcopy(source)
            invalid["chunk_results"] = invalid["chunk_results"][:1]
            write_json(draft, invalid)
            with self.assertRaisesRegex(CaseValidationError, "不完整"):
                validate_packet_file(draft, manifest, schema)

            invalid = copy.deepcopy(source)
            invalid["chunk_results"] = [invalid["chunk_results"][0], invalid["chunk_results"][0]]
            write_json(draft, invalid)
            with self.assertRaisesRegex(CaseValidationError, "重复分块"):
                validate_packet_file(draft, manifest, schema)

            invalid = copy.deepcopy(source)
            invalid["chunk_results"].reverse()
            write_json(draft, invalid)
            with self.assertRaisesRegex(CaseValidationError, "顺序"):
                validate_packet_file(draft, manifest, schema)

            invalid = copy.deepcopy(source)
            invalid["chunk_results"][0]["test_cases"][0].pop("备注")
            write_json(draft, invalid)
            with self.assertRaisesRegex(CaseValidationError, "schema 不一致"):
                validate_packet_file(draft, manifest, schema)

            invalid = copy.deepcopy(source)
            invalid["chunk_results"][0]["test_cases"][0]["优先级"] = "高"
            write_json(draft, invalid)
            with self.assertRaisesRegex(CaseValidationError, "P0"):
                validate_packet_file(draft, manifest, schema)

            chunk_file = job / load_json(manifest)["chunks"][0]["file"]
            chunk_file.write_text(chunk_file.read_text(encoding="utf-8") + "\n- 优惠规则需要确认\n", encoding="utf-8")
            write_json(draft, source)
            with self.assertRaisesRegex(CaseValidationError, "待确认"):
                validate_packet_file(draft, manifest, schema)

    def test_pipeline_validate_streams_next_packet(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job, summary = prepare_job(
                Path(temporary),
                ["模块1", "模块2", "模块3", "模块4"],
                ["--max-chunks-per-packet", "2"],
            )
            self.assertEqual(summary["packet_count"], 2)
            draft = job / "draft.json"
            write_json(draft, packet_payload(job, "packet-0001"))
            code, stdout, stderr = run_pipeline(
                ["validate", "--job-dir", str(job), "--input", str(draft)]
            )
            self.assertEqual((code, stderr), (0, ""))
            first = json.loads(stdout.splitlines()[0])
            self.assertEqual(first["next_packet"]["packet_id"], "packet-0002")
            self.assertIn(pipeline.PACKET_BEGIN, stdout)

            write_json(draft, packet_payload(job, "packet-0002"))
            code, stdout, stderr = run_pipeline(
                ["validate", "--job-dir", str(job), "--input", str(draft)]
            )
            self.assertEqual((code, stderr), (0, ""))
            self.assertIsNone(json.loads(stdout.splitlines()[0])["next_packet"])
            self.assertNotIn(pipeline.PACKET_BEGIN, stdout)

    def test_legacy_chunk_validation_and_merge_remain_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job, _ = prepare_job(Path(temporary), ["登录", "支付"])
            manifest = load_json(job / "manifest.json")
            legacy = job / "legacy"
            legacy.mkdir()
            for chunk in manifest["chunks"]:
                payload = {
                    "chunk_id": chunk["id"],
                    "test_cases": [default_case(chunk["id"])],
                }
                path = legacy / f"{chunk['id']}.json"
                write_json(path, payload)
                validate_file(path, job / "manifest.json", job / "schema.json")
            merged = merge_files(job / "manifest.json", job / "schema.json", legacy)
            self.assertEqual([case["用例ID"] for case in merged["test_cases"]], ["TC-0001", "TC-0002"])


@unittest.skipUnless(
    os.environ.get("WORKSPACE_NODE") and os.environ.get("WORKSPACE_NODE_MODULES"),
    "set workspace Node paths to run the artifact-tool integration test",
)
class WorkbookIntegrationTests(unittest.TestCase):
    def test_finalize_builds_verified_collision_safe_workbook(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            job, _ = prepare_job(base, ["登录", "支付"])
            draft = job / "draft.json"
            write_json(draft, packet_payload(job, "packet-0001"))
            code, _, stderr = run_pipeline(
                ["validate", "--job-dir", str(job), "--input", str(draft)]
            )
            self.assertEqual((code, stderr), (0, ""))

            requested = base / "测试用例.xlsx"
            requested.write_bytes(b"occupied")
            code, stdout, stderr = run_pipeline(
                [
                    "finalize",
                    "--job-dir",
                    str(job),
                    "--node",
                    os.environ["WORKSPACE_NODE"],
                    "--node-modules",
                    os.environ["WORKSPACE_NODE_MODULES"],
                    "--output",
                    str(requested),
                ]
            )
            self.assertEqual((code, stderr), (0, ""))
            result = json.loads(stdout)
            output = Path(result["output"])
            preview = Path(result["preview"])
            self.assertNotEqual(output, requested)
            self.assertTrue(output.is_file())
            self.assertEqual(preview.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(result["formula_error_count"], 0)
            self.assertTrue(result["first_row_frozen"])

            with zipfile.ZipFile(output) as workbook:
                workbook_root = ET.fromstring(workbook.read("xl/workbook.xml"))
                names = [element.get("name") for element in workbook_root.findall(f".//{{{MAIN_NS}}}sheet")]
                self.assertEqual(names, ["测试用例"])
                sheet_root = ET.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
                pane = sheet_root.find(f".//{{{MAIN_NS}}}pane")
                self.assertIsNotNone(pane)
                self.assertEqual((pane.get("state"), pane.get("ySplit")), ("frozen", "1"))


if __name__ == "__main__":
    unittest.main()
