import pytest

from catalog import inspect as inspect_svc
from catalog.models import (
    INSPECT_BLOCKED,
    INSPECT_CONTENDED,
    INSPECT_PASS,
    INSPECT_REVIEW,
    InspectPolicy,
    Inspection,
    ParseJob,
    ROUTE_BLOCK,
    ROUTE_REVIEW,
    STATUS_LANDED,
    STATUS_PARSED,
    STATUS_REVIEW,
    STATUS_RETURNED,
)
from catalog.seed import ensure_seed


def login(client):
    client.post("/login", {"username": "admin", "password": "123456"})


def make_parsed_job(code="t_job", parsed=40):
    job = ParseJob.objects.create(
        title="测试任务", code=code, status=STATUS_PARSED, source_uri="file://x", parsed=parsed
    )
    job.parse_round = 1
    job.save(update_fields=["parse_round"])
    return job


def set_policy(ratio=10, route=ROUTE_BLOCK):
    policy = inspect_svc.policy_for_today()
    policy.ratio = ratio
    policy.fail_route = route
    policy.save(update_fields=["ratio", "fail_route"])
    return policy


@pytest.mark.django_db
def test_land_requires_inspection_first(client):
    login(client)
    job = make_parsed_job()
    r = client.post(f"/jobs/{job.id}/land/", follow=True)
    job.refresh_from_db()
    assert job.status == STATUS_PARSED
    assert job.landed == 0
    assert "未抽检不许落档" in r.content.decode("utf-8")


@pytest.mark.django_db
def test_inspection_pass_then_land(client):
    login(client)
    set_policy(ratio=100, route=ROUTE_BLOCK)
    job = make_parsed_job(code="pass_job", parsed=1)  # 1 条样，确定性规则下必为通过
    client.post(f"/jobs/{job.id}/inspect/")
    insp = Inspection.objects.get(job=job, is_decider=True)
    assert insp.status == INSPECT_PASS
    assert insp.sampled == 1 and insp.passed == 1

    client.post(f"/jobs/{job.id}/land/")
    job.refresh_from_db()
    assert job.status == STATUS_LANDED
    assert job.landed == job.parsed


@pytest.mark.django_db
def test_block_route_returns_job_and_blocks_land(client):
    login(client)
    set_policy(ratio=100, route=ROUTE_BLOCK)
    job = make_parsed_job(code="block_job", parsed=12)
    client.post(f"/jobs/{job.id}/inspect/")
    insp = Inspection.objects.get(job=job, is_decider=True)
    assert insp.status == INSPECT_BLOCKED
    assert insp.flagged > 0

    job.refresh_from_db()
    assert job.status == STATUS_RETURNED
    r = client.post(f"/jobs/{job.id}/land/", follow=True)
    job.refresh_from_db()
    assert job.landed == 0
    assert job.status == STATUS_RETURNED
    assert "抽检未通过" in r.content.decode("utf-8")


@pytest.mark.django_db
def test_review_route_lands_readonly(client):
    login(client)
    set_policy(ratio=100, route=ROUTE_REVIEW)
    job = make_parsed_job(code="review_job", parsed=30)
    client.post(f"/jobs/{job.id}/inspect/")
    insp = Inspection.objects.get(job=job, is_decider=True)
    assert insp.status == INSPECT_REVIEW

    client.post(f"/jobs/{job.id}/land/")
    job.refresh_from_db()
    assert job.status == STATUS_REVIEW
    assert job.landed == job.parsed

    # 待复核只读：解析、抽检、落档都不再执行
    client.post(f"/jobs/{job.id}/parse/")
    client.post(f"/jobs/{job.id}/inspect/")
    client.post(f"/jobs/{job.id}/land/")
    job.refresh_from_db()
    assert job.status == STATUS_REVIEW
    assert Inspection.objects.filter(job=job).count() == 1


@pytest.mark.django_db
def test_zero_ratio_fails_and_blocks_land(client):
    login(client)
    set_policy(ratio=0, route=ROUTE_REVIEW)
    job = make_parsed_job(code="zero_job", parsed=50)
    client.post(f"/jobs/{job.id}/inspect/")
    insp = Inspection.objects.get(job=job, is_decider=True)
    assert insp.ratio == 0
    assert insp.status == INSPECT_BLOCKED
    assert "零比例失败" in insp.reason

    client.post(f"/jobs/{job.id}/land/")
    job.refresh_from_db()
    assert job.landed == 0
    assert job.status == STATUS_RETURNED


@pytest.mark.django_db
def test_route_deviation_blocks_land_and_is_visible(client):
    login(client)
    set_policy(ratio=100, route=ROUTE_REVIEW)
    job = make_parsed_job(code="dev_job", parsed=30)
    client.post(f"/jobs/{job.id}/inspect/")
    assert Inspection.objects.get(job=job, is_decider=True).route == ROUTE_REVIEW

    # 抽检后策略改走法：抽检走法偏离当日策略
    client.post("/policy/", {"ratio": 100, "fail_route": ROUTE_BLOCK, "route_pinned": "on"})
    InspectPolicy.objects.filter(day=inspect_svc.policy_for_today().day).update(ratio=100)
    r = client.get(f"/jobs/{job.id}/")
    assert "走法偏了" in r.content.decode("utf-8")

    r = client.post(f"/jobs/{job.id}/land/", follow=True)
    job.refresh_from_db()
    assert job.landed == 0
    assert "走法偏了" in r.content.decode("utf-8")


@pytest.mark.django_db
def test_second_inspection_is_contended_failure(client):
    login(client)
    set_policy(ratio=100, route=ROUTE_BLOCK)
    job = make_parsed_job(code="race_job", parsed=1)

    client.post(f"/jobs/{job.id}/inspect/")
    winner = Inspection.objects.get(job=job, is_decider=True)
    assert winner.status == INSPECT_PASS

    # 后到请求：同一轮次再次抽检
    other = __import__("django.test", fromlist=["Client"]).Client()
    login(other)
    other.post(f"/jobs/{job.id}/inspect/")

    loser = Inspection.objects.get(job=job, is_decider=False)
    assert loser.status == INSPECT_CONTENDED
    assert "争用失败" in loser.reason
    assert Inspection.objects.filter(job=job, is_decider=True).count() == 1

    # 争用失败不推翻先到裁定，落档照旧
    job.refresh_from_db()
    assert job.status == STATUS_PARSED
    listing = client.get("/inspections/").content.decode("utf-8")
    assert "抽检争用失败" in listing


@pytest.mark.django_db
def test_detail_metrics_side_by_side(client):
    login(client)
    set_policy(ratio=50, route=ROUTE_BLOCK)
    job = make_parsed_job(code="metric_job", parsed=40)
    client.post(f"/jobs/{job.id}/inspect/")
    insp = Inspection.objects.get(job=job, is_decider=True)
    body = client.get(f"/jobs/{job.id}/").content.decode("utf-8")
    for label in ("抽检条数", "通过占比", "待复核数", "走法名"):
        assert label in body
    assert f"{insp.sampled}" in body
    assert f"{insp.pass_percent}%" in body
    assert insp.route_label in body


@pytest.mark.django_db
def test_inspection_list_separates_pass_and_review(client):
    login(client)
    set_policy(ratio=100, route=ROUTE_BLOCK)
    ok_job = make_parsed_job(code="list_pass", parsed=1)
    client.post(f"/jobs/{ok_job.id}/inspect/")

    set_policy(ratio=100, route=ROUTE_REVIEW)
    rv_job = make_parsed_job(code="list_review", parsed=30)
    client.post(f"/jobs/{rv_job.id}/inspect/")

    body = client.get("/inspections/").content.decode("utf-8")
    assert "<h2>通过态" in body and "<h2>待复核态" in body
    # 命中样例可展开
    assert "命中" in body
    # 两组各自出现在对应区段
    pass_idx = body.index("<h2>通过态")
    review_idx = body.index("<h2>待复核态")
    assert "list_pass" in body[pass_idx:review_idx]
    assert "list_pass" not in body[review_idx:]
    assert "list_review" in body[review_idx:]


@pytest.mark.django_db
def test_policy_pins_route_and_ratio(client):
    login(client)
    policy = inspect_svc.policy_for_today()
    client.post("/policy/", {"ratio": 20, "fail_route": ROUTE_BLOCK, "route_pinned": "on", "ratio_pinned": "on"})
    policy.refresh_from_db()
    assert policy.route_pinned and policy.ratio_pinned
    assert policy.fail_route == ROUTE_BLOCK and policy.ratio == 20

    r = client.post(
        "/policy/",
        {"ratio": 30, "fail_route": ROUTE_REVIEW, "route_pinned": "on", "ratio_pinned": "on"},
        follow=True,
    )
    policy.refresh_from_db()
    assert policy.fail_route == ROUTE_BLOCK
    assert policy.ratio == 20
    assert "不能再改" in r.content.decode("utf-8")

    # 也不能通过省略勾选解除钉死
    r = client.post("/policy/", {"ratio": 20, "fail_route": ROUTE_BLOCK}, follow=True)
    policy.refresh_from_db()
    assert policy.route_pinned and policy.ratio_pinned
    assert "不能解除钉死" in r.content.decode("utf-8")

    # 钉死状态下页面锁定两项
    page = client.get("/policy/").content.decode("utf-8")
    assert "已钉死" in page
    assert '<select name="fail_route">' not in page


@pytest.mark.django_db
def test_seed_still_works(client):
    ensure_seed()
    assert ParseJob.objects.count() == 4


@pytest.mark.django_db
def test_contended_failure_samples_expand_in_list(client):
    login(client)
    set_policy(ratio=100, route=ROUTE_BLOCK)
    job = make_parsed_job(code="race_samples", parsed=12)
    client.post(f"/jobs/{job.id}/inspect/")  # 先到：命中 2 条，挡住
    other = __import__("django.test", fromlist=["Client"]).Client()
    login(other)
    other.post(f"/jobs/{job.id}/inspect/")  # 后到：争用失败

    body = other.get("/inspections/").content.decode("utf-8")
    section = body[body.index("抽检争用失败"):]
    assert "争用失败" in section
    assert "条命中" in section
    assert "字段值偏离源页" in section


@pytest.mark.django_db
def test_detail_shows_hit_samples_and_review_count(client):
    login(client)
    set_policy(ratio=100, route=ROUTE_BLOCK)
    job = make_parsed_job(code="detail_hit", parsed=12)
    client.post(f"/jobs/{job.id}/inspect/")
    insp = Inspection.objects.get(job=job, is_decider=True)
    body = client.get(f"/jobs/{job.id}/").content.decode("utf-8")
    assert f"{insp.flagged}" in body
    assert "抽检逐条" in body
    assert "字段值偏离源页" in body


@pytest.mark.django_db
def test_land_blocked_after_policy_route_change_is_persisted(client):
    login(client)
    set_policy(ratio=100, route=ROUTE_BLOCK)
    # 1 条样例、确定性规则下通过，避免被「抽检未通过」先拦
    job = make_parsed_job(code="persist_dev", parsed=1)
    client.post(f"/jobs/{job.id}/inspect/")
    insp = Inspection.objects.get(job=job, is_decider=True)
    assert insp.route == ROUTE_BLOCK
    assert insp.status == INSPECT_PASS

    client.post("/policy/", {"ratio": 100, "fail_route": ROUTE_REVIEW, "route_pinned": "on"})
    client.post(f"/jobs/{job.id}/land/")
    insp = Inspection.objects.get(job=job, is_decider=True)
    assert insp.route_deviated is True
    assert "走法偏了" in insp.reason

    listing = client.get("/inspections/").content.decode("utf-8")
    assert "走法偏了" in listing
