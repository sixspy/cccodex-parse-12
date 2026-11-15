from django.db import models
from django.utils import timezone


# 抽检失败走法：当日钉死其一，不允许两种并存
ROUTE_BLOCK = "block"
ROUTE_REVIEW = "review"
ROUTE_LABELS = {
    ROUTE_BLOCK: "整批挡住落档退回重析",
    ROUTE_REVIEW: "放行落档但标待复核只读",
}
ROUTE_CHOICES = [(key, ROUTE_LABELS[key]) for key in (ROUTE_BLOCK, ROUTE_REVIEW)]

# 后台裁定的抽检结论
INSPECT_PASS = "通过"
INSPECT_BLOCKED = "挡住"
INSPECT_REVIEW = "待复核"
INSPECT_CONTENDED = "争用失败"

# 任务状态
STATUS_PENDING = "待解析"
STATUS_PARSED = "已解析待入库"
STATUS_RETURNED = "退回重析"
STATUS_REVIEW_PENDING = "抽检待复核"
STATUS_REVIEW = "待复核"
STATUS_LANDED = "已入库"

DEFAULT_RATIO = 10
MAX_RATIO = 100


class ParseJob(models.Model):
    title = models.CharField(max_length=120)
    code = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=32, default=STATUS_PENDING)
    source_uri = models.CharField(max_length=255)
    parsed = models.IntegerField(default=0)
    landed = models.IntegerField(default=0)
    parse_round = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]


class InspectPolicy(models.Model):
    """抽检策略：一天一条，走法钉死一种，比例一并钉死。"""

    day = models.DateField(unique=True)
    ratio = models.IntegerField(default=DEFAULT_RATIO)  # 抽检比例，百分比，0..100
    fail_route = models.CharField(max_length=16, choices=ROUTE_CHOICES, default=ROUTE_BLOCK)
    route_pinned = models.BooleanField(default=False)  # 走法钉死为当日唯一
    ratio_pinned = models.BooleanField(default=False)  # 比例是否一并钉死
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-day"]

    @classmethod
    def for_day(cls, day):
        obj, _ = cls.objects.get_or_create(
            day=day,
            defaults={"ratio": DEFAULT_RATIO, "fail_route": ROUTE_BLOCK, "route_pinned": False, "ratio_pinned": False},
        )
        return obj

    @property
    def route_label(self) -> str:
        return ROUTE_LABELS.get(self.fail_route, self.fail_route)

    @property
    def route_pin_label(self) -> str:
        return "已钉死·当日唯一" if self.route_pinned else "未钉死"

    @property
    def ratio_pin_label(self) -> str:
        return "已钉死" if self.ratio_pinned else "未钉死"


class Inspection(models.Model):
    """一次抽检裁定。is_decider 的一行是本轮唯一裁定者，后到请求记为争用失败。"""

    job = models.ForeignKey(ParseJob, on_delete=models.CASCADE, related_name="inspections")
    parse_round = models.IntegerField(default=0)
    policy_day = models.DateField(default=timezone.localdate)
    ratio = models.IntegerField(default=0)
    route = models.CharField(max_length=16, choices=ROUTE_CHOICES, default=ROUTE_BLOCK)
    status = models.CharField(max_length=16, default="裁定中")
    reason = models.CharField(max_length=240, blank=True, default="")
    route_deviated = models.BooleanField(default=False)  # 抽检走法偏离当日策略
    is_decider = models.BooleanField(default=False)
    sampled = models.IntegerField(default=0)
    passed = models.IntegerField(default=0)
    flagged = models.IntegerField(default=0)
    inspector = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["job", "parse_round"],
                condition=models.Q(is_decider=True),
                name="uniq_inspection_decider_per_round",
            )
        ]

    @property
    def route_label(self) -> str:
        return ROUTE_LABELS.get(self.route, self.route)

    @property
    def pass_percent(self) -> int:
        if self.sampled <= 0:
            return 0
        return int(round(self.passed * 100 / self.sampled))


class InspectionSample(models.Model):
    inspection = models.ForeignKey(Inspection, on_delete=models.CASCADE, related_name="samples")
    idx = models.IntegerField(default=0)
    content = models.TextField()
    state = models.CharField(max_length=8, default="通过")
    reason = models.CharField(max_length=120, blank=True, default="")

    class Meta:
        ordering = ["idx"]
