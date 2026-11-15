"""抽样抽检与落档裁定。

规则钉在这里，视图只负责收发请求：

* 解析后必须先抽检才许落档；
* 抽检比例取自当日抽检策略（可钉死），比例为零直接判失败；
* 抽检失败走法取自当日策略并钉死一种，落档时再核对一次；
* 同一解析轮次只有先到的抽检请求能裁定，后到的一律记争用失败。
"""

from math import ceil

from django.db import IntegrityError, transaction
from django.utils import timezone

from catalog.models import (
    INSPECT_BLOCKED,
    INSPECT_CONTENDED,
    INSPECT_PASS,
    INSPECT_REVIEW,
    InspectPolicy,
    Inspection,
    InspectionSample,
    ParseJob,
    ROUTE_BLOCK,
    ROUTE_REVIEW,
    STATUS_LANDED,
    STATUS_REVIEW,
    STATUS_REVIEW_PENDING,
    STATUS_RETURNED,
)

PASS_SAMPLE = "通过"
HIT_SAMPLE = "命中"


def policy_for_today():
    return InspectPolicy.for_day(timezone.localdate())


def _sample_count(parsed: int, ratio: int) -> int:
    """按比例抽条数：有比例时至少抽 1 条，零比例不抽。"""
    if ratio <= 0:
        return 0
    return max(1, ceil(parsed * ratio / 100))


def _sample_hit(job: ParseJob, idx: int) -> bool:
    """后台裁定用确定性规则，保证同一任务同一轮次结果可复现。"""
    return (job.id * 3 + idx * 5) % 11 < 2


def _build_samples(job: ParseJob, count: int):
    rows = []
    for idx in range(1, count + 1):
        hit = _sample_hit(job, idx)
        rows.append(
            InspectionSample(
                idx=idx,
                content=f"第 {idx} 条 · {job.code} · 字段抽取结果 {job.parsed + idx * 7}",
                state=HIT_SAMPLE if hit else PASS_SAMPLE,
                reason="字段值偏离源页" if hit else "",
            )
        )
    return rows


def _decider(job: ParseJob, parse_round: int):
    return (
        Inspection.objects.filter(job=job, parse_round=parse_round, is_decider=True)
        .order_by("-id")
        .first()
    )


def attempt_inspection(job: ParseJob, inspector: str):
    """执行一次抽检，返回 (inspection, 提示语)。过不过由这里裁定。"""
    policy = policy_for_today()
    parse_round = job.parse_round

    try:
        with transaction.atomic():
            insp = Inspection.objects.create(
                job=job,
                parse_round=parse_round,
                policy_day=policy.day,
                ratio=policy.ratio,
                route=policy.fail_route,
                inspector=inspector,
                is_decider=True,
                status="裁定中",
            )
    except IntegrityError:
        insp = _record_contended(job, policy, inspector, parse_round)
        return insp, f"抽检争用失败：轮次 {parse_round} 已由先到请求裁定，本次为后到请求。"

    if policy.ratio <= 0:
        insp.status = INSPECT_BLOCKED
        insp.reason = "零比例失败：策略比例为 0，抽检直接判失败"
        insp.save(update_fields=["status", "reason"])
        job.status = STATUS_RETURNED
        job.landed = 0
        job.save(update_fields=["status", "landed"])
        return insp, "零比例失败：抽检未通过，整批挡住落档，退回重析。"

    samples = _build_samples(job, _sample_count(job.parsed, policy.ratio))
    for s in samples:
        s.inspection = insp
    InspectionSample.objects.bulk_create(samples)
    insp.sampled = len(samples)
    insp.flagged = sum(1 for s in samples if s.state == HIT_SAMPLE)
    insp.passed = insp.sampled - insp.flagged

    if not insp.flagged:
        insp.status = INSPECT_PASS
        insp.reason = "抽检通过"
        message = f"抽检通过：抽检 {insp.sampled} 条，全部通过。"
    elif policy.fail_route == ROUTE_BLOCK:
        insp.status = INSPECT_BLOCKED
        insp.reason = "抽检未通过：整批挡住落档，退回重析"
        job.status = STATUS_RETURNED
        job.landed = 0
        job.save(update_fields=["status", "landed"])
        message = f"抽检未通过：命中 {insp.flagged} 条，整批挡住落档，退回重析。"
    else:
        insp.status = INSPECT_REVIEW
        insp.reason = "抽检未通过：放行落档但标待复核，只读"
        job.status = STATUS_REVIEW_PENDING
        job.save(update_fields=["status"])
        message = f"抽检未通过：命中 {insp.flagged} 条，按策略放行落档但标待复核（只读）。"

    insp.save(update_fields=["sampled", "passed", "flagged", "status", "reason"])
    return insp, message


def _record_contended(job, policy, inspector, parse_round):
    winner = _decider(job, parse_round)
    return Inspection.objects.create(
        job=job,
        parse_round=parse_round,
        policy_day=policy.day,
        ratio=policy.ratio,
        route=policy.fail_route,
        inspector=inspector,
        is_decider=False,
        status=INSPECT_CONTENDED,
        sampled=winner.sampled if winner else 0,
        passed=winner.passed if winner else 0,
        flagged=winner.flagged if winner else 0,
        reason=f"争用失败：轮次 {parse_round} 已由先到请求裁定，本次后到作废",
    )


def attempt_land(job: ParseJob):
    """落档裁决，返回 (是否放行, 提示语, 落档后状态)。"""
    policy = policy_for_today()
    insp = _decider(job, job.parse_round)
    if insp is None:
        return False, "未抽检不许落档：先做抽样抽检，过不过由后台裁定。", None
    if policy.ratio <= 0 or insp.ratio <= 0:
        return False, "零比例失败：抽检比例为零，拦下落档。", None
    if insp.status == INSPECT_CONTENDED:
        return False, "抽检争用失败：本轮裁定已由先到请求占用，拦下落档。", None
    if insp.status == INSPECT_BLOCKED:
        return False, "抽检未通过：整批挡住落档，先退回重析。", None
    if insp.route != policy.fail_route:
        if not insp.route_deviated:
            insp.route_deviated = True
            insp.reason = f"走法偏了：抽检走法「{insp.route_label}」与当日策略不符；{insp.reason}"
            insp.save(update_fields=["route_deviated", "reason"])
        return (
            False,
            f"走法偏了：抽检走法「{insp.route_label}」与当日策略「{policy.route_label}」不符，拦下落档。",
            None,
        )
    if insp.status == INSPECT_REVIEW:
        if policy.fail_route != ROUTE_REVIEW:
            return False, "抽检待复核：策略并非放行走法，拦下落档。", None
        return True, "抽检未通过但按策略放行：已落档并标待复核，只读。", STATUS_REVIEW
    if insp.status != INSPECT_PASS:
        return False, "抽检未裁定：拦下落档。", None
    return True, f"落档完成：抽检 {insp.sampled} 条全部通过，已入库。", STATUS_LANDED


def summarize(job: ParseJob):
    """读盘详情并排指标：抽检条数、通过占比、待复核数、走法名。"""
    insp = _decider(job, job.parse_round)
    if insp is None:
        return {
            "inspection": None,
            "sampled": 0,
            "pass_percent": 0,
            "review_count": 0,
            "route_name": "未抽检",
            "samples": [],
            "hit_samples": [],
            "deviation": False,
        }
    samples = list(insp.samples.all())
    policy = policy_for_today()
    return {
        "inspection": insp,
        "sampled": insp.sampled,
        "pass_percent": insp.pass_percent,
        "review_count": insp.flagged,
        "route_name": insp.route_label,
        "samples": samples,
        "hit_samples": [s for s in samples if s.state == HIT_SAMPLE],
        "deviation": insp.route != policy.fail_route or insp.route_deviated,
    }
