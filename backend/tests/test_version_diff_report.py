"""版本差异报告测试：覆盖生成、更正、发布、导出、报表同步与上游重跑全流程。"""
from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import store as store_module
from app.store import reset_store


@pytest.fixture()
def client() -> TestClient:
    reset_store()
    return TestClient(app)


def _parse_csv(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text.lstrip("﻿"))))


def _generate(client: TestClient, payload: dict) -> dict:
    response = client.post("/api/version_diff_report", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True, body
    return body["entry"]


def _action(client: TestClient, report_id: int, payload: dict) -> dict:
    response = client.post(f"/api/version_diff_report/{report_id}/actions", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True, body
    return body["entry"]


class TestGenerateAndRecalculate:
    def test_align_touches_and_classify_over_threshold(self, client: TestClient) -> None:
        """基准 100、对照 115、阈值 10：差异 15 > 10，应判超阈值并回溯两侧金额。"""
        report = _generate(client, {
            "compare_job_id": 1,
            "基准版本": "首次归因",
            "对照版本": "末次归因",
            "差异阈值": 10,
            "触点类型": "搜索广告",
            "基准金额": 100,
            "对照金额": 115,
        })
        assert report["状态"] == "草稿"
        assert report["版本号"] == "V1"
        row = report["触点明细"][0]
        assert row["基准金额"] == 100.0
        assert row["对照金额"] == 115.0
        assert row["差异"] == 15.0
        assert row["差异分类"] == "超阈值"
        assert report["超阈值触点"] == ["搜索广告"]
        assert report["超阈值触点数"] == 1
        assert report["超阈值金额"] == 15.0
        assert report["abnormal"] is True

    def test_within_threshold_after_correction(self, client: TestClient) -> None:
        """确认前把基准 100 更正为 112：差异变为 3，不再超阈值。"""
        report = _generate(client, {
            "compare_job_id": 1,
            "差异阈值": 10,
            "触点类型": "搜索广告",
            "基准金额": 100,
            "对照金额": 115,
        })
        assert report["触点明细"][0]["差异分类"] == "超阈值"

        corrected = _action(client, report["id"], {
            "action": "更正基准",
            "触点类型": "搜索广告",
            "金额": 112,
        })
        row = corrected["触点明细"][0]
        assert row["基准金额"] == 112.0
        assert row["对照金额"] == 115.0
        assert row["差异"] == 3.0
        assert row["差异分类"] == "未超阈值"
        assert corrected["超阈值触点"] == []
        assert corrected["超阈值金额"] == 0.0
        assert corrected["abnormal"] is False

    def test_second_submission_gets_new_version(self, client: TestClient) -> None:
        """同一对比任务再次提交生成，应形成 V2 且与 V1 各自独立。"""
        first = _generate(client, {
            "compare_job_id": 1, "差异阈值": 10,
            "触点类型": "搜索广告", "基准金额": 100, "对照金额": 115,
        })
        second = _generate(client, {
            "compare_job_id": 1, "差异阈值": 10,
            "触点类型": "搜索广告", "基准金额": 112, "对照金额": 115,
        })
        assert first["版本号"] == "V1"
        assert second["版本号"] == "V2"
        assert first["id"] != second["id"]
        assert first["触点明细"][0]["基准金额"] == 100.0
        assert second["触点明细"][0]["基准金额"] == 112.0

    def test_multi_touch_alignment_keeps_single_side(self, client: TestClient) -> None:
        """按触点对齐：只在一侧出现的触点保留并标记单侧缺失，禁止发布半份对比。"""
        report = _generate(client, {
            "compare_job_id": 1,
            "差异阈值": 10,
            "touches": [
                {"触点类型": "搜索广告", "基准金额": 100, "对照金额": 115},
                {"触点类型": "推荐流", "基准金额": 50, "对照金额": 50},
            ],
        })
        # 再制造一个单侧缺失触点
        _action(client, report["id"], {
            "action": "更正对照", "触点类型": "直播", "金额": 80,
        })
        detail = client.get(f"/api/version_diff_report/{report['id']}").json()
        live = next(row for row in detail["触点明细"] if row["触点类型"] == "直播")
        assert live["单侧缺失"] is True
        assert live["差异分类"] == "单侧缺失"

        response = client.post(
            f"/api/version_diff_report/{report['id']}/actions",
            json={"action": "确认发布"},
        )
        assert response.json()["ok"] is False
        assert "半份对比" in response.json()["message"]


class TestPublishAndSnapshot:
    def test_publish_freezes_snapshot_and_compare_job(self, client: TestClient) -> None:
        """确认发布：冻结两侧版本与阈值，回写对比任务结论，发布后只读。"""
        report = _generate(client, {
            "compare_job_id": 1, "差异阈值": 10,
            "触点类型": "搜索广告", "基准金额": 112, "对照金额": 115,
        })
        published = _action(client, report["id"], {"action": "确认发布"})
        assert published["状态"] == "已发布"
        assert published["published_at"] is not None
        assert published["基准版本"] and published["对照版本"]
        assert published["触点明细"][0]["差异分类"] == "未超阈值"

        job = client.get("/api/compare_job/1").json()
        assert job["status"] == "已完成"
        assert "VDR-1-V1" in job["处理结论"]
        assert job["差异行数"] == "0"

        # 已发布报告不能再更正
        response = client.post(
            f"/api/version_diff_report/{report['id']}/actions",
            json={"action": "更正基准", "触点类型": "搜索广告", "金额": 1},
        )
        assert response.json()["ok"] is False
        assert "只读快照" in response.json()["message"]

    def test_publish_over_threshold_marks_compare_job(self, client: TestClient) -> None:
        report = _generate(client, {
            "compare_job_id": 2, "差异阈值": 10,
            "触点类型": "搜索广告", "基准金额": 100, "对照金额": 115,
        })
        _action(client, report["id"], {"action": "确认发布"})
        job = client.get("/api/compare_job/2").json()
        assert job["status"] == "差异显著"
        assert job["abnormal"] is True

    def test_published_endpoint_excludes_drafts(self, client: TestClient) -> None:
        draft = _generate(client, {
            "compare_job_id": 1, "差异阈值": 10,
            "触点类型": "搜索广告", "基准金额": 100, "对照金额": 115,
        })
        published = _generate(client, {
            "compare_job_id": 1, "差异阈值": 10,
            "触点类型": "搜索广告", "基准金额": 112, "对照金额": 115,
        })
        _action(client, published["id"], {"action": "确认发布"})

        items = client.get("/api/version_diff_report/published").json()["items"]
        ids = {item["id"] for item in items}
        assert draft["id"] not in ids
        assert published["id"] in ids

    def test_no_half_report_during_publish(self, client: TestClient) -> None:
        """发布期间的任何读取都只能看到完整草稿或完整正式报告。"""
        report = _generate(client, {
            "compare_job_id": 1, "差异阈值": 10,
            "触点类型": "搜索广告", "基准金额": 112, "对照金额": 115,
        })
        seen: list[str] = []

        def probe() -> None:
            for _ in range(50):
                data = client.get(f"/api/version_diff_report/{report['id']}").json()
                seen.append(data["状态"])
                # 任一状态下两侧金额与分类都必须齐全
                for row in data["触点明细"]:
                    assert row["基准金额"] is not None and row["对照金额"] is not None
                    assert row["差异"] is not None

        probe()
        _action(client, report["id"], {"action": "确认发布"})
        probe()
        assert set(seen) <= {"草稿", "已发布"}
        assert "已发布" in seen


class TestReportGenSync:
    def test_sync_uses_only_frozen_two_side_versions(self, client: TestClient) -> None:
        """报表入口同步已确认报告数据并汇总超阈值触点，不混入其他归因版本。"""
        report = _generate(client, {
            "compare_job_id": 1, "差异阈值": 10,
            "touches": [
                {"触点类型": "搜索广告", "基准金额": 100, "对照金额": 115},
                {"触点类型": "推荐流", "基准金额": 80, "对照金额": 82},
            ],
        })
        _action(client, report["id"], {"action": "确认发布"})
        entry = _action(client, report["id"], {"action": "同步报表"})

        assert entry["报表类型"] == "版本差异报告"
        assert entry["差异报告编号"] == "VDR-1-V1"
        assert entry["基准版本"] == report["基准版本"]
        assert entry["对照版本"] == report["对照版本"]
        assert entry["超阈值触点"] == "搜索广告"
        assert entry["超阈值触点数"] == 1
        assert entry["超阈值金额"] == 15.0
        assert entry["status"] == "已生成"
        # 明细行只来自该报告自身快照
        assert {row["触点类型"] for row in entry["触点明细"]} == {"搜索广告", "推荐流"}

        # 报表生成列表能查到同步条目，且只有一份、幂等
        _action(client, report["id"], {"action": "同步报表"})
        repo_rows = [r for r in store_module.store.rows("report_gen") if r.get("差异报告ID") == report["id"]]
        assert len(repo_rows) == 1

    def test_sync_rejects_draft(self, client: TestClient) -> None:
        report = _generate(client, {
            "compare_job_id": 1, "差异阈值": 10,
            "触点类型": "搜索广告", "基准金额": 100, "对照金额": 115,
        })
        response = client.post(
            f"/api/version_diff_report/{report['id']}/actions", json={"action": "同步报表"})
        assert response.json()["ok"] is False


class TestRerunUpstream:
    def test_rerun_keeps_published_snapshot(self, client: TestClient) -> None:
        """报告确认后重跑上游，历史金额与差异仍按原组合可复查。"""
        report = _generate(client, {
            "compare_job_id": 1,
            "链路编号": "ATTR-001",
            "基准版本": "首次归因",
            "对照版本": "末次归因",
            "差异阈值": 10,
            "触点类型": "搜索广告", "基准金额": 112, "对照金额": 115,
        })
        _action(client, report["id"], {"action": "确认发布"})
        before = client.get(f"/api/version_diff_report/{report['id']}").json()

        result = _action(client, report["id"], {"action": "重跑上游"})
        assert result["snapshot_unchanged"] is True
        assert "fresh_pipeline" in result and "models" in result["fresh_pipeline"]

        after = client.get(f"/api/version_diff_report/{report['id']}").json()
        assert after["触点明细"] == before["触点明细"]
        assert after["差异阈值"] == before["差异阈值"]
        assert after["状态"] == "已发布"


class TestExport:
    def test_single_report_export_is_recomputable(self, client: TestClient) -> None:
        """下载文件含两侧金额与阈值，可按文件内容独立复算差异与分类。"""
        report = _generate(client, {
            "compare_job_id": 1, "差异阈值": 10,
            "touches": [
                {"触点类型": "搜索广告", "基准金额": 100, "对照金额": 115},
                {"触点类型": "推荐流", "基准金额": 80, "对照金额": 82},
            ],
        })
        response = client.get(f"/api/version_diff_report/{report['id']}/export")
        assert response.status_code == 200
        assert "VDR-1-V1.csv" in response.headers["content-disposition"]
        rows = _parse_csv(response.text)

        meta = {r[0]: r[1] for r in rows if len(r) == 2 and r[0]}
        assert float(meta["差异阈值"]) == 10.0
        assert meta["基准版本"] and meta["对照版本"]

        detail = [r for r in rows if len(r) == 7]
        header, data_rows = detail[0], detail[1:]
        assert header == ["触点类型", "基准金额", "对照金额", "差异", "差异阈值", "差异分类", "单侧缺失"]
        for row in data_rows:
            baseline, control, diff, threshold = float(row[1]), float(row[2]), float(row[3]), float(row[4])
            assert abs(abs(control - baseline) - diff) < 1e-9
            expected = "超阈值" if diff > threshold else "未超阈值"
            assert row[5] == expected

    def test_export_two_versions_keep_own_figures(self, client: TestClient) -> None:
        """两版文件各自保留两侧金额与阈值，分别复算互不串版。"""
        v1 = _generate(client, {
            "compare_job_id": 1, "差异阈值": 10,
            "触点类型": "搜索广告", "基准金额": 100, "对照金额": 115,
        })
        v2 = _generate(client, {
            "compare_job_id": 1, "差异阈值": 10,
            "触点类型": "搜索广告", "基准金额": 112, "对照金额": 115,
        })
        _action(client, v2["id"], {"action": "确认发布"})

        files = {}
        for report in (v1, v2):
            response = client.get(f"/api/version_diff_report/{report['id']}/export")
            files[report["版本号"]] = _parse_csv(response.text)

        v1_rows = [r for r in files["V1"] if len(r) == 7][1:]
        v2_rows = [r for r in files["V2"] if len(r) == 7][1:]
        assert float(v1_rows[0][1]) == 100.0 and v1_rows[0][5] == "超阈值"
        assert float(v2_rows[0][1]) == 112.0 and v2_rows[0][5] == "未超阈值"

        # 汇总导出同样带两侧金额与阈值
        merged = client.get("/api/version_diff_report/export").text
        assert "V1" in merged and "V2" in merged
        assert "100.0" in merged and "112.0" in merged


class TestValidation:
    def test_missing_compare_job(self, client: TestClient) -> None:
        body = client.post("/api/version_diff_report", json={"compare_job_id": 999}).json()
        assert body["ok"] is False
        assert "对比任务 999" in body["message"]

    def test_unparseable_amounts(self, client: TestClient) -> None:
        bad = client.post("/api/version_diff_report", json={
            "compare_job_id": 1, "差异阈值": 10,
            "触点类型": "搜索广告", "基准金额": "abc", "对照金额": 1,
        }).json()
        assert bad["ok"] is False

    def test_pipeline_fallback_when_amounts_omitted(self, client: TestClient) -> None:
        """未显式提交金额时回退上游归因管道取数，按触点并集对齐成草稿。"""
        report = _generate(client, {
            "compare_job_id": 1,
            "链路编号": "ATTR-001",
            "基准版本": "首次归因",
            "对照版本": "末次归因",
        })
        assert report["触点明细"]
        names = {row["触点类型"] for row in report["触点明细"]}
        assert names == {"搜索广告", "推荐流"}
        # 管道两侧的金额都被保留在对应触点上，单侧触点显式标记而不是静默丢弃
        amounts = {row["触点类型"]: row for row in report["触点明细"]}
        assert amounts["搜索广告"]["基准金额"] == 320.0
        assert amounts["推荐流"]["对照金额"] == 320.0
        assert all(row["单侧缺失"] for row in report["触点明细"])

    def test_publish_requires_threshold(self, client: TestClient) -> None:
        report = _generate(client, {
            "compare_job_id": 3,
            "触点类型": "搜索广告", "基准金额": 1, "对照金额": 2,
        })
        # 对比任务 3 自带阈值 0.10，这里显式覆盖为缺失场景
        report_row = store_module.store.find("version_diff_report", report["id"])
        report_row["差异阈值"] = None
        response = client.post(
            f"/api/version_diff_report/{report['id']}/actions", json={"action": "确认发布"})
        assert response.json()["ok"] is False
        assert "阈值" in response.json()["message"]
