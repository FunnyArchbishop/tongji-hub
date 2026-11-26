"""同济绩点换算（纯本地计算，不联网）。

两套官方口径，别混用：

* **本科**（教务处页面，5 分制）：优 A=5（89.5–100，折算 95）、良 B=4（79.5–89.4，折算 85）、
  中 C=3（69.5–79.4，折算 75）、及格 D=2（59.5–69.4，折算 65）、不及格 F=0（0–59.4，折算 30）。
  注意不及格折算值是 **30 不是 0**。GPA = Σ(学分 × 绩点) / Σ学分，全部课程纳入。
* **研究生**（研究生院页面）：90–100=5A、80–<90=4B、70–<80=3C、60–<70=2D、<60=0E。

分段边界不同：89.7 在本科是「优 = 5」，在研究生是「B = 4」。所以必须分两条分支算。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

UNDERGRADUATE = "undergraduate"
GRADUATE = "graduate"


@dataclass(frozen=True)
class Band:
    """一个分数段。"""

    label: str
    point: float
    low: float
    high: float
    #: 官方给出的「折算分」，只用于展示，不参与 GPA 计算。
    converted: float | None = None


# 本科：上界用 89.4 / 79.4 这种写法，是官方页面上的原样。
UNDERGRADUATE_BANDS: tuple[Band, ...] = (
    Band("优 A", 5.0, 89.5, 100.0, 95.0),
    Band("良 B", 4.0, 79.5, 89.4, 85.0),
    Band("中 C", 3.0, 69.5, 79.4, 75.0),
    Band("及格 D", 2.0, 59.5, 69.4, 65.0),
    Band("不及格 F", 0.0, 0.0, 59.4, 30.0),
)

# 研究生：左闭右开（90–100 含 100，80–<90 不含 90）。
GRADUATE_BANDS: tuple[Band, ...] = (
    Band("5A", 5.0, 90.0, 100.0),
    Band("4B", 4.0, 80.0, 89.999),
    Band("3C", 3.0, 70.0, 79.999),
    Band("2D", 2.0, 60.0, 69.999),
    Band("0E", 0.0, 0.0, 59.999),
)

BANDS = {UNDERGRADUATE: UNDERGRADUATE_BANDS, GRADUATE: GRADUATE_BANDS}


def band_of(score: float, level: str = UNDERGRADUATE) -> Band:
    """分数 → 分数段；超出范围时按两端处理，不抛异常。"""
    bands = BANDS.get(level, UNDERGRADUATE_BANDS)
    value = max(0.0, min(100.0, float(score)))
    for band in bands:
        if band.low <= value <= band.high:
            return band
    return bands[-1]


def point_of(score: float, level: str = UNDERGRADUATE) -> float:
    return band_of(score, level).point


@dataclass
class CourseScore:
    name: str
    score: float
    credit: float = 1.0

    @classmethod
    def from_mapping(cls, row: Any) -> "CourseScore | None":
        if not isinstance(row, dict):
            return None
        name = str(row.get("name") or row.get("course") or "").strip()
        try:
            score = float(row.get("score"))
        except (TypeError, ValueError):
            return None
        if not name:
            return None
        try:
            credit = float(row.get("credit") or 1.0)
        except (TypeError, ValueError):
            credit = 1.0
        if credit <= 0:
            credit = 1.0
        return cls(name=name, score=score, credit=credit)


def compute_gpa(rows: Iterable[Any], level: str = UNDERGRADUATE) -> dict[str, Any]:
    """算加权 GPA，并给出每门课的档位明细。空输入返回 gpa=None。"""
    courses: list[CourseScore] = []
    for row in rows:
        course = CourseScore.from_mapping(row) if not isinstance(row, CourseScore) else row
        if course is not None:
            courses.append(course)
    total_credit = sum(course.credit for course in courses)
    if not courses or total_credit <= 0:
        return {
            "level": level,
            "gpa": None,
            "credits": 0.0,
            "courses": [],
            "note": "没有可算的课程",
        }
    weighted = 0.0
    details: list[dict[str, Any]] = []
    for course in courses:
        band = band_of(course.score, level)
        weighted += course.credit * band.point
        details.append({
            "name": course.name,
            "score": course.score,
            "credit": course.credit,
            "label": band.label,
            "point": band.point,
            "converted": band.converted,
        })
    return {
        "level": level,
        "gpa": round(weighted / total_credit, 3),
        "credits": round(total_credit, 3),
        "courses": details,
        "note": "",
    }
