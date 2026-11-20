"""版本差异报告业务规则。

围绕对比任务（compare_job）沉淀两侧归因统计的对齐结果：

- 按触点把基准版本与对照版本的归因金额对齐，计算绝对差异并按阈值分类；
- 草稿阶段允许更正任一侧金额并重算，确认发布后整份报告冻结为只读快照；
- 发布过程在同一把锁内完成，外部任何时候读到的要么是完整草稿、要么是完整正式报告；
- 报表生成（report_gen）只能引用已发布报告自身冻结的两侧版本，不混入其他归因版本数据；
- 重跑上游归因管道只返回新结果，不改动已发布快照，历史金额始终可按原组合复查。
"""
from __future__ import annotations

import copy
import threading
from datetime import datetime
from typing import Any

from app import store as store_module
from app.services.pipeline_orchestrator import run_attribution_compare_pipeline

MODULE = "version_diff_report"
REPORT_GEN_MODULE = "report_gen"

DRAFT = "草稿"
PUBLISHED = "已发布"
STATUSES = [DRAFT, PUBLISHED]

OVER_THRESHOLD = "超阈值"
WITHIN_THRESHOLD = "未超阈值"

# 报告发布在同一把进程锁内完成状态翻转与快照落定，读接口也在锁内拷贝，
# 保证发布期间不会读到半份对比。
_lock = threading.RLock()

# 归因管道产物里的模型键 → 接口侧可能出现的版本叫法。
_MODEL_ALIASES = {
    "first_touch": {"first_touch", "首次归因", "首次", "首次触点"},
    "last_touch": {"last_touch", "末次归因", "末次", "末次触点"},
    "linear": {"linear", "线性归因", "线性", "线性触点"},
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _to_float(value: Any) -> float | None:
    """宽松数值解析；空值与无法解析的内容返回 None，便于标记单侧缺失。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _model_key(version_name: str | None) -> str | None:
    if not version_name:
        return None
    text = str(version_name).strip()
    for key, aliases in _MODEL_ALIASES.items():
        if text in aliases:
            return key
    return None


def _parse_amount_maps(values: dict[str, Any]) -> tuple[dict[str, float], dict[str, float], list[str]]:
    """从请求载荷里解析两侧金额，返回 (基准金额表, 对照金额表, 错误说明)。

    支持三种入口：
    1. touches / 触点明细：[{触点类型, 基准金额, 对照金额}]；
    2. baseline_amounts / control_amounts：两侧各自的 {触点: 金额} 字典；
    3. 单触点简写：触点类型 + 基准金额 + 对照金额。
    """
    baseline_map: dict[str, float] = {}
    control_map: dict[str, float] = {}
    errors: list[str] = []

    def put(target: dict[str, float], name: Any, amount: Any) -> None:
        touch_name = str(name).strip() if name is not None else ""
        if not touch_name:
            errors.append("触点类型不能为空")
            return
        parsed = _to_float(amount)
        if parsed is None:
            errors.append(f"触点「{touch_name}」金额无法解析：{amount!r}")
            return
        target[touch_name] = parsed

    touches = values.get("touches") or values.get("触点明细")
    if isinstance(touches, list) and touches:
        for row in touches:
            if not isinstance(row, dict):
                errors.append(f"触点行必须是对象：{row!r}")
                continue
            name = row.get("触点类型") or row.get("触点") or row.get("name")
            put(baseline_map, name, row.get("基准金额", row.get("baseline_amount")))
            put(control_map, name, row.get("对照金额", row.get("control_amount")))
    else:
        raw_baseline = values.get("baseline_amounts") or values.get("基准金额表")
        raw_control = values.get("control_amounts") or values.get("对照金额表")
        if isinstance(raw_baseline, dict) and isinstance(raw_control, dict):
            for name, amount in raw_baseline.items():
                put(baseline_map, name, amount)
            for name, amount in raw_control.items():
                put(control_map, name, amount)
        elif values.get("基准金额") is not None or values.get("对照金额") is not None:
            name = values.get("触点类型") or "全触点"
            put(baseline_map, name, values.get("基准金额"))
            put(control_map, name, values.get("对照金额"))

    return baseline_map, control_map, errors


def _load_amounts_from_pipeline(
    values: dict[str, Any],
) -> tuple[dict[str, float], dict[str, float], list[str]]:
    """未显式给触点金额时，重跑上游归因管道并按两侧版本取数对齐。"""
    errors: list[str] = []
    result = run_attribution_compare_pipeline(values.get("链路编号"))
    if "error" in result:
        return {}, {}, [f"上游归因管道无可用数据：{result['error']}"]
    models = result.get("models", {})
    baseline_key = _model_key(values.get("基准版本") or values.get("基准数据"))
    control_key = _model_key(values.get("对照版本") or values.get("对比数据"))
    if baseline_key is None:
        baseline_key = "first_touch"
    if control_key is None:
        control_key = "last_touch"
    baseline_map = {str(k): float(v) for k, v in models.get(baseline_key, {}).items()}
    control_map = {str(k): float(v) for k, v in models.get(control_key, {}).items()}
    if not baseline_map and not control_map:
        errors.append("上游归因管道未返回任何触点金额")
    return baseline_map, control_map, errors


def _align_rows(
    baseline_map: dict[str, float],
    control_map: dict[str, float],
    threshold: float | None,
) -> list[dict[str, Any]]:
    """按触点名称对齐两侧金额并计算差异；只在一侧出现的触点保留并标记单侧缺失。"""
    names = list(dict.fromkeys([*baseline_map.keys(), *control_map.keys()]))
    rows: list[dict[str, Any]] = []
    for name in names:
        baseline = baseline_map.get(name)
        control = control_map.get(name)
        single_side = baseline is None or control is None
        if single_side:
            diff: float | None = None
            classification = "单侧缺失"
        else:
            diff = round(abs(control - baseline), 4)  # type: ignore[arg-type]
            if threshold is None:
                classification = "待设阈值"
            elif diff > threshold:
                classification = OVER_THRESHOLD
            else:
                classification = WITHIN_THRESHOLD
        rows.append({
            "触点类型": name,
            "基准金额": baseline,
            "对照金额": control,
            "差异": diff,
            "差异分类": classification,
            "单侧缺失": single_side,
        })
    return rows


def _recalc(report: dict[str, Any]) -> None:
    """依据当前触点明细与阈值重算全部派生结论，保证超阈值结论可回溯到两侧金额。"""
    threshold = _to_float(report.get("差异阈值"))
    report["差异阈值"] = threshold
    rows = report["触点明细"]
    over_names: list[str] = []
    over_amount = 0.0
    baseline_total = 0.0
    control_total = 0.0
    for row in rows:
        baseline = _to_float(row.get("基准金额"))
        control = _to_float(row.get("对照金额"))
        row["基准金额"] = baseline
        row["对照金额"] = control
        row["单侧缺失"] = baseline is None or control is None
        if baseline is not None:
            baseline_total += baseline
        if control is not None:
            control_total += control
        if row["单侧缺失"]:
            row["差异"] = None
            row["差异分类"] = "单侧缺失"
            continue
        diff = round(abs(control - baseline), 4)  # type: ignore[arg-type]
        row["差异"] = diff
        if threshold is None:
            row["差异分类"] = "待设阈值"
        elif diff > threshold:
            row["差异分类"] = OVER_THRESHOLD
            over_names.append(str(row["触点类型"]))
            over_amount += diff
        else:
            row["差异分类"] = WITHIN_THRESHOLD
    report["触点总数"] = len(rows)
    report["基准金额合计"] = round(baseline_total, 4)
    report["对照金额合计"] = round(control_total, 4)
    report["超阈值触点"] = over_names
    report["超阈值触点数"] = len(over_names)
    report["超阈值金额"] = round(over_amount, 4)
    report["abnormal"] = len(over_names) > 0


class VersionDiffReportService:
    """版本差异报告的生成、更正、发布、导出与报表同步。"""

    # -- 查询 ----------------------------------------------------------------

    def list_reports(
        self,
        *,
        status: str | None = None,
        compare_job_id: int | None = None,
        version: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with _lock:
            rows = [copy.deepcopy(row) for row in store_module.store.rows(MODULE)]
        if status:
            rows = [row for row in rows if row.get("状态") == status or row.get("status") == status]
        if compare_job_id is not None:
            rows = [row for row in rows if int(row.get("compare_job_id", -1)) == compare_job_id]
        if version:
            rows = [row for row in rows if str(row.get("版本号")) == str(version)]
        if keyword:
            rows = [
                row for row in rows
                if keyword.casefold() in str(row.get("报告编号", "")).casefold()
            ]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def list_published(self) -> list[dict[str, Any]]:
        """已发布报告查询入口：只暴露正式报告，草稿不会被报表侧读到。"""
        items, _ = self.list_reports(status=PUBLISHED, page=1, size=1_000_000)
        return items

    def get_report(self, report_id: int) -> dict[str, Any] | None:
        with _lock:
            row = store_module.store.find(MODULE, report_id)
            return copy.deepcopy(row) if row is not None else None

    # -- 生成 ----------------------------------------------------------------

    def generate(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        errors: list[str] = []
        job_id_raw = values.get("compare_job_id") or values.get("对比任务id") or values.get("对比任务ID")
        try:
            compare_job_id = int(job_id_raw)
        except (TypeError, ValueError):
            return None, ["compare_job_id 必须是对比任务的整数 id"]
        compare_job = store_module.store.find("compare_job", compare_job_id)
        if compare_job is None:
            return None, [f"对比任务 {compare_job_id} 不存在或已归档"]

        threshold = _to_float(values.get("差异阈值"))
        if threshold is None:
            threshold = _to_float(compare_job.get("差异阈值"))

        baseline_map, control_map, parse_errors = _parse_amount_maps(values)
        if parse_errors:
            return None, parse_errors
        if not baseline_map and not control_map:
            pipeline_baseline, pipeline_control, pipeline_errors = _load_amounts_from_pipeline(values)
            errors.extend(pipeline_errors)
            baseline_map, control_map = pipeline_baseline, pipeline_control
        if not baseline_map and not control_map:
            return None, errors or ["缺少两侧触点金额，无法生成版本差异报告"]

        with _lock:
            rows = store_module.store.rows(MODULE)
            report_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
            sibling_versions = [
                int(str(row.get("版本号", "V0")).lstrip("V") or 0)
                for row in rows
                if int(row.get("compare_job_id", -1)) == compare_job_id
            ]
            version_no = max(sibling_versions, default=0) + 1
            version_label = f"V{version_no}"
            report: dict[str, Any] = {
                "id": report_id,
                "报告编号": f"VDR-{compare_job_id}-{version_label}",
                "compare_job_id": compare_job_id,
                "版本号": version_label,
                "状态": DRAFT,
                "status": DRAFT,
                "基准版本": str(values.get("基准版本") or compare_job.get("基准数据") or "基准版本"),
                "对照版本": str(values.get("对照版本") or compare_job.get("对比数据") or "对照版本"),
                "链路编号": values.get("链路编号"),
                "差异阈值": threshold,
                "触点明细": _align_rows(baseline_map, control_map, threshold),
                "created_at": _now(),
                "published_at": None,
                "pending": True,
            }
            _recalc(report)
            rows.append(report)
            return copy.deepcopy(report), errors

    # -- 动作 ----------------------------------------------------------------

    def run_action(
        self,
        report_id: int,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        payload = payload or {}
        with _lock:
            report = store_module.store.find(MODULE, report_id)
            if report is None:
                return None, f"版本差异报告 {report_id} 不存在或已归档"

            if action in ("更正基准", "更正对照"):
                if report["状态"] != DRAFT:
                    return None, "已发布报告是只读快照，金额更正只能在确认发布前进行"
                amount_map = payload.get("values")
                touch_name = payload.get("触点类型")
                amount = payload.get("金额", payload.get("基准金额", payload.get("对照金额")))
                updates: dict[str, Any] = {}
                if isinstance(amount_map, dict) and amount_map:
                    updates = {str(k): _to_float(v) for k, v in amount_map.items()}
                elif touch_name is not None:
                    updates = {str(touch_name): _to_float(amount)}
                if not updates or any(v is None for v in updates.values()):
                    return None, "更正内容必须包含可解析的触点金额"
                target_side = "基准金额" if action == "更正基准" else "对照金额"
                existing = {str(row["触点类型"]): row for row in report["触点明细"]}
                for name, parsed_amount in updates.items():
                    if name in existing:
                        existing[name][target_side] = parsed_amount
                    else:
                        new_row = {"触点类型": name, "基准金额": None, "对照金额": None}
                        new_row[target_side] = parsed_amount
                        report["触点明细"].append(new_row)
                _recalc(report)
                report["updated_at"] = _now()
                return copy.deepcopy(report), f"已{action}并重算差异"

            if action == "确认发布":
                if report["状态"] != DRAFT:
                    return None, "只有草稿状态的报告可以确认发布"
                if _to_float(report.get("差异阈值")) is None:
                    return None, "差异阈值缺失，无法确认发布"
                missing = [
                    str(row["触点类型"]) for row in report["触点明细"]
                    if row.get("基准金额") is None or row.get("对照金额") is None
                ]
                if missing:
                    return None, f"触点 {'、'.join(missing)} 缺少一侧金额，不能发布半份对比"
                # 状态翻转、时间戳与对比任务回写在同一把锁内一次完成。
                report["状态"] = PUBLISHED
                report["status"] = PUBLISHED
                report["published_at"] = _now()
                report["pending"] = False
                compare_job = store_module.store.find("compare_job", int(report["compare_job_id"]))
                if compare_job is not None:
                    over_count = int(report["超阈值触点数"])
                    compare_job["差异行数"] = str(over_count)
                    compare_job["处理结论"] = (
                        f"已发布{report['报告编号']}：超阈值触点 {over_count} 个，"
                        f"超阈值差异金额合计 {report['超阈值金额']}"
                    )
                    if over_count > 0:
                        compare_job["status"] = "差异显著"
                        compare_job["abnormal"] = True
                    else:
                        compare_job["status"] = "已完成"
                        compare_job["abnormal"] = False
                    compare_job["pending"] = False
                return copy.deepcopy(report), "版本差异报告已确认发布并冻结快照"

            return None, f"动作「{action}」不属于版本差异报告可执行范围"

    # -- 报表同步 ------------------------------------------------------------

    def sync_to_report_gen(self, report_id: int) -> tuple[dict[str, Any] | None, str]:
        """把已发布报告同步到报表生成模块；只引用本报告冻结的两侧版本。"""
        with _lock:
            report = store_module.store.find(MODULE, report_id)
            if report is None:
                return None, f"版本差异报告 {report_id} 不存在或已归档"
            if report["状态"] != PUBLISHED:
                return None, "只有已发布的版本差异报告才能同步到报表生成"
            snapshot = copy.deepcopy(report)

        report_rows = store_module.store.rows(REPORT_GEN_MODULE)
        report_code = f"REPO-{snapshot['报告编号']}"
        entry = next((row for row in report_rows if row.get("报表编号") == report_code), None)
        if entry is None:
            entry = {"id": max((int(row.get("id", 0)) for row in report_rows), default=0) + 1}
            report_rows.append(entry)
        # 全部字段取自该报告自身的冻结快照，不读取其他归因版本的任何单侧数据。
        entry.update({
            "报表编号": report_code,
            "报表类型": "版本差异报告",
            "数据范围": f"{snapshot['基准版本']} 对比 {snapshot['对照版本']}",
            "包含图表": "触点差异明细表",
            "生成周期": "按需",
            "输出格式": "csv",
            "分发渠道": "平台下载",
            "下次生成": "—",
            "status": "已生成",
            "pending": False,
            "abnormal": bool(snapshot["abnormal"]),
            "差异报告ID": snapshot["id"],
            "差异报告编号": snapshot["报告编号"],
            "版本号": snapshot["版本号"],
            "基准版本": snapshot["基准版本"],
            "对照版本": snapshot["对照版本"],
            "差异阈值": snapshot["差异阈值"],
            "超阈值触点": "、".join(snapshot["超阈值触点"]),
            "超阈值触点数": snapshot["超阈值触点数"],
            "超阈值金额": snapshot["超阈值金额"],
            "基准金额合计": snapshot["基准金额合计"],
            "对照金额合计": snapshot["对照金额合计"],
            "published_at": snapshot["published_at"],
            "触点明细": copy.deepcopy(snapshot["触点明细"]),
        })
        return copy.deepcopy(entry), f"已同步{snapshot['报告编号']}到报表生成模块"

    # -- 上游重跑 ------------------------------------------------------------

    def rerun_upstream(self, report_id: int) -> tuple[dict[str, Any] | None, str]:
        """重跑上游归因管道；已发布快照保持不变，只返回新一轮管道结果用于复查。"""
        with _lock:
            report = store_module.store.find(MODULE, report_id)
            if report is None:
                return None, f"版本差异报告 {report_id} 不存在或已归档"
            link_code = report.get("链路编号")
            snapshot_before = copy.deepcopy(report["触点明细"])
            threshold_before = report.get("差异阈值")

        fresh = run_attribution_compare_pipeline(link_code)
        with _lock:
            report_now = store_module.store.find(MODULE, report_id)
            snapshot_unchanged = (
                report_now is not None
                and report_now.get("触点明细") == snapshot_before
                and report_now.get("差异阈值") == threshold_before
            )
        return {
            "report_id": report_id,
            "报告编号": report_now.get("报告编号") if report_now else None,
            "link_code": link_code,
            "snapshot_unchanged": snapshot_unchanged,
            "fresh_pipeline": fresh,
        }, "上游归因管道已重跑，已发布报告快照保持不变"


service = VersionDiffReportService()
