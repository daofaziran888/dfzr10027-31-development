"""版本差异报告接口：生成、更正、确认发布、导出、报表同步与上游重跑。"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.schemas import ActionResult, PageResult
from app.services.version_diff_report import PUBLISHED, service

router = APIRouter(prefix="/api/version_diff_report", tags=["版本差异报告"])

DETAIL_HEADER = ["触点类型", "基准金额", "对照金额", "差异", "差异阈值", "差异分类", "单侧缺失"]
META_FIELDS = [
    "报告编号", "compare_job_id", "版本号", "状态", "基准版本", "对照版本",
    "链路编号", "差异阈值", "触点总数", "基准金额合计", "对照金额合计",
    "超阈值触点数", "超阈值金额", "超阈值触点", "created_at", "published_at",
]


def _csv_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(("=", "+", "-", "@")) else text


def _report_csv(report: dict) -> str:
    """把一份完整报告落成可核对 CSV：先两侧版本与阈值，再逐触点金额与分类。"""
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["字段", "值"])
    for field in META_FIELDS:
        value = report.get(field)
        if isinstance(value, list):
            value = "、".join(str(item) for item in value)
        writer.writerow([field, _csv_cell(value)])
    writer.writerow([])
    writer.writerow(DETAIL_HEADER)
    for row in report.get("触点明细", []):
        writer.writerow([
            _csv_cell(row.get("触点类型")),
            _csv_cell(row.get("基准金额")),
            _csv_cell(row.get("对照金额")),
            _csv_cell(row.get("差异")),
            _csv_cell(report.get("差异阈值")),
            _csv_cell(row.get("差异分类")),
            _csv_cell("是" if row.get("单侧缺失") else "否"),
        ])
    return output.getvalue()


@router.get("", response_model=PageResult[dict])
def list_reports(
    status: str | None = Query(default=None, description="草稿、已发布"),
    compare_job_id: int | None = Query(default=None, description="按对比任务 id 过滤"),
    version: str | None = Query(default=None, description="按版本号过滤，如 V1"),
    keyword: str | None = Query(default=None, description="按报告编号检索"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """列出版本差异报告，支持状态、对比任务、版本号与关键字过滤。"""
    items, total = service.list_reports(
        status=status, compare_job_id=compare_job_id, version=version,
        keyword=keyword, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/published")
def list_published() -> dict[str, object]:
    """已发布报告查询入口，供报表生成等下游引用同一份已确认结果。"""
    items = service.list_published()
    return {"items": items, "total": len(items)}


@router.get("/export")
def export_reports(
    status: str | None = Query(default=None),
    compare_job_id: int | None = Query(default=None),
) -> StreamingResponse:
    """按筛选条件导出多份报告汇总 CSV，逐报告保留两侧版本、阈值与触点明细。"""
    items, _ = service.list_reports(
        status=status, compare_job_id=compare_job_id, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["报告编号", "版本号", "状态", "基准版本", "对照版本", *DETAIL_HEADER])
    for report in items:
        for row in report.get("触点明细", []):
            writer.writerow([
                _csv_cell(report.get("报告编号")),
                _csv_cell(report.get("版本号")),
                _csv_cell(report.get("状态")),
                _csv_cell(report.get("基准版本")),
                _csv_cell(report.get("对照版本")),
                *[_csv_cell(row.get(col)) for col in DETAIL_HEADER],
            ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="version_diff_reports.csv"'})


@router.get("/{report_id}", response_model=dict)
def get_report(report_id: int) -> dict:
    """读取单份版本差异报告；不存在时给出可读错误说明。"""
    report = service.get_report(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"版本差异报告 {report_id} 不存在或已归档")
    return report


@router.get("/{report_id}/export")
def export_report(report_id: int) -> StreamingResponse:
    """下载单份报告文件，文件内含两侧完整版本、阈值与逐触点金额，可独立复算。"""
    report = service.get_report(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"版本差异报告 {report_id} 不存在或已归档")
    filename = f"{report.get('报告编号', f'VDR-{report_id}')}.csv"
    return StreamingResponse(
        iter([_report_csv(report)]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.post("", response_model=ActionResult)
def generate_report(payload: dict[str, object]) -> ActionResult:
    """为对比任务生成一版草稿报告；可直接提交触点金额，也可回退上游管道取数。"""
    report, errors = service.generate(payload)
    if report is None:
        return ActionResult(ok=False, message="；".join(errors) or "版本差异报告生成失败")
    message = "版本差异报告草稿已生成"
    if errors:
        message = f"{message}（提示：{'、'.join(errors)}）"
    return ActionResult(ok=True, message=message, entry=report)


@router.post("/{report_id}/actions", response_model=ActionResult)
def run_action(report_id: int, payload: dict[str, object]) -> ActionResult:
    """执行更正基准、更正对照、确认发布、同步报表、重跑上游等动作。"""
    action = str(payload.get("action", "")).strip()
    if action == "同步报表":
        entry, message = service.sync_to_report_gen(report_id)
        return ActionResult(ok=entry is not None, message=message, entry=entry)
    if action == "重跑上游":
        entry, message = service.rerun_upstream(report_id)
        return ActionResult(ok=entry is not None, message=message, entry=entry)
    entry, message = service.run_action(report_id, action, payload)
    return ActionResult(ok=entry is not None, message=message, entry=entry)
