import re
from datetime import timedelta

from flask import Blueprint, g, render_template, request

from .auth import login_required
from .constants import CATEGORY_KEYS
from .models import Meeting
from .recommend import for_you, my_upcoming, open_meetings
from .timeutil import now_kst, now_utc, to_kst

bp = Blueprint("discover", __name__)

DAY_OPTIONS = [("", "전체"), ("today", "오늘"), ("tomorrow", "내일"), ("week", "7일 안")]
PART_OPTIONS = [
    ("", "시간대 전체"),
    ("morning", "오전 (12시 전)"),
    ("afternoon", "오후 (12~18시)"),
    ("evening", "저녁 (18시 이후)"),
]
PART_HOURS = {"morning": (0, 12), "afternoon": (12, 18), "evening": (18, 24)}
DAY_VALUES = {value for value, _ in DAY_OPTIONS}
SOON_HOURS = 2
TOKEN_SPLIT_RE = re.compile(r"[\s#,]+")
SORT_OPTIONS = [("soon", "빨리 시작하는 순"), ("new", "새로 열린 순"), ("seats", "마감 임박 순")]


def filter_meetings(meetings, q="", category="", day="", part="", open_only=False, soon=False):
    """목록 필터. 데이터가 작아서 파이썬에서 거른다(SQLite와 PostgreSQL에서 똑같이 동작한다)."""
    today = now_kst().date()
    soon_limit = now_utc() + timedelta(hours=SOON_HOURS)
    # 검색어는 낱말로 쪼개 모두 들어 있는 모임을 찾는다. 카드에 보이는 "#초면환영"을 그대로 쳐도,
    # "점심 숭실대"처럼 두 낱말을 쳐도 찾을 수 있어야 한다.
    tokens = [token for token in TOKEN_SPLIT_RE.split(q.lower()) if token]
    result = []
    for meeting in meetings:
        start_kst = to_kst(meeting.start_at)
        if category and meeting.category != category:
            continue
        if tokens:
            text = meeting.search_text
            if not all(token in text for token in tokens):
                continue
        if soon and meeting.start_at > soon_limit:
            continue
        if day == "today" and start_kst.date() != today:
            continue
        if day == "tomorrow" and start_kst.date() != today + timedelta(days=1):
            continue
        if day == "week" and start_kst.date() > today + timedelta(days=7):
            continue
        if part in PART_HOURS:
            low, high = PART_HOURS[part]
            if not low <= start_kst.hour < high:
                continue
        if open_only and not meeting.joinable:
            continue
        result.append(meeting)
    return result


@bp.get("/")
@login_required
def home():
    q = request.args.get("q", "").strip()[:30]
    category = request.args.get("category", "")
    if category not in CATEGORY_KEYS:
        category = ""
    day = request.args.get("day", "")
    if day not in DAY_VALUES:
        day = ""
    part = request.args.get("part", "")
    if part not in PART_HOURS:
        part = ""
    sort = request.args.get("sort", "soon")
    open_only = request.args.get("open") == "1"
    soon = request.args.get("soon") == "1"

    upcoming = (
        Meeting.query.filter(Meeting.status != "cancelled", Meeting.start_at > now_utc())
        .order_by(Meeting.start_at)
        .limit(200)
        .all()
    )
    # 신고가 쌓여 모집이 멈춘 모임은 목록에서 내린다. 이미 참여한 사람과 호스트에게만 계속 보인다.
    upcoming = [m for m in upcoming if m.status != "blocked" or m.has_participant(g.user)]
    meetings = filter_meetings(upcoming, q, category, day, part, open_only, soon)
    if sort == "new":
        meetings.sort(key=lambda m: m.created_at, reverse=True)
    elif sort == "seats":
        # 참여할 수 있는 모임을 앞에, 그 안에서는 남은 자리가 적은 순으로.
        meetings.sort(key=lambda m: (not m.joinable, m.seats_left, m.start_at))

    filtered = bool(q or category or day or part or open_only or soon)
    mine = my_upcoming(g.user)
    # 푸시 알림을 보낼 수 없으니, 곧 시작하는 내 번개는 홈에 들어왔을 때 맨 위에서 알려 준다.
    soon_limit = now_utc() + timedelta(hours=2)
    starting_soon = [
        m for m in upcoming if m.start_at <= soon_limit and m.has_participant(g.user)
    ]
    recommended = []
    if not filtered and g.user.interest_list:
        # 위에서 읽은 목록을 추천에도 그대로 써서 같은 모임을 DB에서 두 번 읽지 않는다.
        recommended = for_you(g.user, pool=open_meetings(g.user, candidates=upcoming, mine=mine))
    return render_template(
        "home.html",
        meetings=meetings,
        starting_soon=starting_soon,
        # 이미 시작한 내 번개는 목록(앞으로 열릴 모임)에 없으므로 따로 알려 준다. 장소를 다시 보려고 들어온 사람을 위한 것이다.
        ongoing=[m for m in mine if m.has_started],
        for_you=recommended,
        filters={"q": q, "category": category, "day": day, "part": part, "sort": sort, "open": open_only, "soon": soon},
        filtered=filtered,
        day_options=DAY_OPTIONS,
        part_options=PART_OPTIONS,
        sort_options=SORT_OPTIONS,
        total_open=sum(1 for m in upcoming if m.joinable and not m.has_participant(g.user)),
        any_upcoming=bool(upcoming),
    )
