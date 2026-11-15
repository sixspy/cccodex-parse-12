from django.conf import settings
from django.contrib import messages
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_http_methods, require_POST

from catalog import inspect as inspect_svc
from catalog.models import (
    INSPECT_BLOCKED,
    INSPECT_CONTENDED,
    INSPECT_PASS,
    INSPECT_REVIEW,
    MAX_RATIO,
    ROUTE_CHOICES,
    ROUTE_LABELS,
    STATUS_LANDED,
    STATUS_PARSED,
    STATUS_PENDING,
    STATUS_REVIEW,
    STATUS_REVIEW_PENDING,
    STATUS_RETURNED,
    InspectPolicy,
    Inspection,
    ParseJob,
)


def _authed(request) -> bool:
    return request.session.get("user") == settings.ADMIN_USER


def _ctx(request, **extra):
    base = {"lab": settings.LAB_NAME, "user": request.session["user"]}
    base.update(extra)
    return base


def root(request):
    return HttpResponseRedirect("/jobs/" if _authed(request) else "/login")


@require_http_methods(["GET", "POST"])
def login_page(request):
    err = ""
    if request.method == "POST":
        user = request.POST.get("username", "").strip()
        pwd = request.POST.get("password", "")
        if user == settings.ADMIN_USER and pwd == settings.ADMIN_PASSWORD:
            request.session["user"] = user
            return HttpResponseRedirect("/jobs/")
        err = "账号或密码错误"
    return render(request, "login.html", {"error": err, "lab": settings.LAB_NAME})


def logout_view(request):
    request.session.flush()
    return HttpResponseRedirect("/login")


def job_list(request):
    if not _authed(request):
        return HttpResponseRedirect("/login")
    items = list(ParseJob.objects.all())
    return render(request, "jobs_list.html", _ctx(request, items=items))


def _readonly(job) -> bool:
    return job.status == STATUS_REVIEW


def job_detail(request, job_id: int):
    if not _authed(request):
        return HttpResponseRedirect("/login")
    job = get_object_or_404(ParseJob, pk=job_id)
    summary = inspect_svc.summarize(job)
    policy = inspect_svc.policy_for_today()
    return render(
        request,
        "job_detail.html",
        _ctx(request, job=job, summary=summary, policy=policy, readonly=_readonly(job)),
    )


@require_POST
def job_parse(request, job_id: int):
    if not _authed(request):
        return HttpResponseRedirect("/login")
    job = get_object_or_404(ParseJob, pk=job_id)
    if _readonly(job):
        messages.error(request, "待复核任务只读，不能重新解析。")
        return HttpResponseRedirect(f"/jobs/{job.id}/")
    if job.status in (STATUS_PENDING, STATUS_RETURNED, "解析失败"):
        job.parsed = 12 + (job.id * 2) % 9
        job.landed = 0
        job.parse_round += 1
        job.status = STATUS_PARSED
        job.save(update_fields=["parsed", "landed", "parse_round", "status"])
        messages.info(request, f"解析完成，进入第 {job.parse_round} 轮，需先抽检才许落档。")
    return HttpResponseRedirect(f"/jobs/{job.id}/")


@require_POST
def job_inspect(request, job_id: int):
    if not _authed(request):
        return HttpResponseRedirect("/login")
    job = get_object_or_404(ParseJob, pk=job_id)
    if _readonly(job):
        messages.error(request, "待复核任务只读，不能再次抽检。")
        return HttpResponseRedirect(f"/jobs/{job.id}/")
    already_decided = Inspection.objects.filter(
        job=job, parse_round=job.parse_round, is_decider=True
    ).exists()
    if not already_decided and (job.status != STATUS_PARSED or job.parse_round <= 0):
        messages.error(request, "当前状态不能抽检：解析后才能抽检；被挡回的任务请退回重析。")
        return HttpResponseRedirect(f"/jobs/{job.id}/")
    _, message = inspect_svc.attempt_inspection(job, request.session["user"])
    messages.info(request, message)
    return HttpResponseRedirect(f"/jobs/{job.id}/")


@require_POST
def job_land(request, job_id: int):
    if not _authed(request):
        return HttpResponseRedirect("/login")
    job = get_object_or_404(ParseJob, pk=job_id)
    if _readonly(job):
        messages.error(request, "待复核任务只读，不能重复落档。")
        return HttpResponseRedirect(f"/jobs/{job.id}/")
    ok, message, new_status = inspect_svc.attempt_land(job)
    if not ok:
        messages.error(request, message)
        return HttpResponseRedirect(f"/jobs/{job.id}/")
    job.landed = job.parsed
    job.status = new_status or STATUS_LANDED
    job.save(update_fields=["landed", "status"])
    messages.info(request, message)
    return HttpResponseRedirect(f"/jobs/{job.id}/")


def inspection_list(request):
    if not _authed(request):
        return HttpResponseRedirect("/login")
    deciders = list(Inspection.objects.filter(is_decider=True).select_related("job"))
    contended = list(Inspection.objects.filter(status=INSPECT_CONTENDED).select_related("job"))
    passed = [i for i in deciders if i.status == INSPECT_PASS]
    review = [i for i in deciders if i.status == INSPECT_REVIEW]
    blocked = [i for i in deciders if i.status == INSPECT_BLOCKED]
    policy = inspect_svc.policy_for_today()
    for i in deciders + contended:
        i.sample_rows = list(i.samples.all())
        i.hit_samples = [s for s in i.sample_rows if s.state == inspect_svc.HIT_SAMPLE]
        i.deviated = i.is_decider and (i.route != policy.fail_route or i.route_deviated)
    for i in contended:
        # 后到请求本身不产样例，展开的是本轮先到裁定的命中样例
        winner = (
            Inspection.objects.filter(job_id=i.job_id, parse_round=i.parse_round, is_decider=True)
            .first()
        )
        i.failure_samples = (
            [s for s in winner.samples.all() if s.state == inspect_svc.HIT_SAMPLE] if winner else []
        )
    return render(
        request,
        "inspections.html",
        _ctx(
            request,
            passed=passed,
            review=review,
            blocked=blocked,
            contended=contended,
            policy=policy,
        ),
    )


@require_http_methods(["GET", "POST"])
def policy_page(request):
    if not _authed(request):
        return HttpResponseRedirect("/login")
    policy = inspect_svc.policy_for_today()
    error = ""
    if request.method == "POST":
        error = _apply_policy(policy, request.POST)
        if not error:
            messages.info(request, "抽检策略已更新。")
            return HttpResponseRedirect("/policy/")
    return render(
        request,
        "policy.html",
        _ctx(
            request,
            policy=policy,
            error=error,
            routes=ROUTE_CHOICES,
            max_ratio=MAX_RATIO,
        ),
    )


def _apply_policy(policy: InspectPolicy, post) -> str:
    raw_ratio = (post.get("ratio") or "").strip()
    try:
        ratio = int(raw_ratio)
    except ValueError:
        return "抽检比例必须是整数。"
    if ratio < 0 or ratio > MAX_RATIO:
        return f"抽检比例须在 0 到 {MAX_RATIO} 之间。"

    route = post.get("fail_route") or policy.fail_route
    if route not in ROUTE_LABELS:
        return "抽检失败走法不合法。"

    pin_route = post.get("route_pinned") == "on"
    pin_ratio = post.get("ratio_pinned") == "on"

    if policy.route_pinned:
        if not pin_route:
            return f"走法已钉死为当日唯一：「{policy.route_label}」，不能解除钉死。"
        if route != policy.fail_route:
            return f"走法已钉死为当日唯一：「{policy.route_label}」，不能再改。"
    if policy.ratio_pinned:
        if not pin_ratio:
            return f"抽检比例已钉死为 {policy.ratio}%，不能解除钉死。"
        if ratio != policy.ratio:
            return f"抽检比例已钉死为 {policy.ratio}%，不能再改。"

    if not policy.route_pinned and route != policy.fail_route and not pin_route:
        return "改走法时必须同时钉死，走法只能留一种。"

    policy.ratio = ratio
    policy.fail_route = route
    policy.route_pinned = pin_route
    policy.ratio_pinned = pin_ratio
    policy.save(update_fields=["ratio", "fail_route", "route_pinned", "ratio_pinned", "updated_at"])
    return ""
