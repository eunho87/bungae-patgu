"""홈 목록 필터와 정렬 (discover.py). 기준 시각 2026-11-12(목) 14:00 KST."""

from datetime import timedelta

import pytest

from bungae.discover import filter_meetings
from bungae.models import Meeting

from .conftest import NOW_UTC, kst


@pytest.fixture
def host(data):
    return data.user("host")


@pytest.fixture
def viewer(data):
    return data.user("viewer")


@pytest.fixture
def seeded(data, host):
    """이름 -> 모임 id."""
    filler = data.user("filler")
    filler2 = data.user("filler2")
    base = NOW_UTC - timedelta(days=1)
    return {
        # 오늘 16:00, 보드게임, 4명 중 1명
        "game_today": data.meeting(
            host, title="보드게임 한 판", category="game", start_at=kst(2026, 11, 12, 16), capacity=4,
            created_at=base + timedelta(minutes=1),
        ),
        # 오늘 19:00, 식사, 3명 중 2명(한 자리 남음)
        "meal_tonight": data.meeting(
            host, title="마라탕 저녁", category="meal", start_at=kst(2026, 11, 12, 19), capacity=3, tags="가볍게,초면환영",
            members=[filler], created_at=base + timedelta(minutes=5),
        ),
        # 내일 01:00 KST (UTC로는 아직 11/12) - 날짜·시간대 계산이 KST 기준인지 확인용
        "late_night": data.meeting(
            host, title="심야 Overwatch", category="game", start_at=kst(2026, 11, 13, 1), capacity=6,
            description="PC방에서 만나요", created_at=base + timedelta(minutes=3),
        ),
        # 내일 10:00, 공부
        "study_tomorrow": data.meeting(
            host, title="Algorithm 스터디", category="study", start_at=kst(2026, 11, 13, 10), place="중앙도서관 3층",
            capacity=5, created_at=base + timedelta(minutes=2),
        ),
        # 내일 15:00, 호스트가 마감
        "closed_tomorrow": data.meeting(
            host, title="마감한 보드게임", category="game", start_at=kst(2026, 11, 13, 15), status="closed",
            created_at=base + timedelta(minutes=6),
        ),
        # 모레 13:00, 정원 참
        "full_cafe": data.meeting(
            host, title="카페 수다", category="cafe", start_at=kst(2026, 11, 14, 13), capacity=3,
            members=[filler, filler2], created_at=base + timedelta(minutes=4),
        ),
        # 7일 뒤 23:30 (week에 포함되는 마지막 날)
        "week_edge": data.meeting(
            host, title="일주일 뒤 러닝", category="sports", start_at=kst(2026, 11, 19, 23, 30),
            created_at=base + timedelta(minutes=7),
        ),
        # 10일 뒤 20:00
        "far_sports": data.meeting(
            host, title="풋살", category="sports", start_at=kst(2026, 11, 22, 20), created_at=base + timedelta(minutes=8),
        ),
    }


def run_filter(app, seeded, **kwargs):
    names = {meeting_id: name for name, meeting_id in seeded.items()}
    with app.app_context():
        meetings = Meeting.query.order_by(Meeting.start_at).all()
        return [names[m.id] for m in filter_meetings(meetings, **kwargs)]


ALL = ["game_today", "meal_tonight", "late_night", "study_tomorrow", "closed_tomorrow", "full_cafe", "week_edge", "far_sports"]


def test_no_filter_keeps_everything_in_order(app, seeded):
    assert run_filter(app, seeded) == ALL


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"category": "game"}, ["game_today", "late_night", "closed_tomorrow"]),
        ({"category": "study"}, ["study_tomorrow"]),
        ({"category": "drink"}, []),
        ({"q": "마라탕"}, ["meal_tonight"]),  # 제목
        ({"q": "초면환영"}, ["meal_tonight"]),  # 태그
        ({"q": "pc방"}, ["late_night"]),  # 소개, 대소문자 무시
        ({"q": "도서관"}, ["study_tomorrow"]),  # 장소
        ({"q": "ALGORITHM"}, ["study_tomorrow"]),
        # 종류 이름으로도 찾는다. "보드게임·게임" 종류라 제목에 그 말이 없는 late_night도 나온다.
        ({"q": "보드게임"}, ["game_today", "late_night", "closed_tomorrow"]),
        ({"q": "#초면환영"}, ["meal_tonight"]),  # 카드에 보이는 그대로 #을 붙여도
        ({"q": "마라탕  초면환영"}, ["meal_tonight"]),  # 낱말 여러 개는 모두 들어 있어야
        ({"q": "마라탕 도서관"}, []),
        ({"q": "공부"}, ["study_tomorrow"]),  # 종류 이름
        ({"soon": True}, ["game_today"]),  # 2시간 안에 시작
        ({"q": "없는말"}, []),
        ({"day": "today"}, ["game_today", "meal_tonight"]),
        ({"day": "tomorrow"}, ["late_night", "study_tomorrow", "closed_tomorrow"]),
        ({"day": "week"}, ["game_today", "meal_tonight", "late_night", "study_tomorrow", "closed_tomorrow", "full_cafe", "week_edge"]),
        ({"day": "nonsense"}, ALL),
        ({"part": "morning"}, ["late_night", "study_tomorrow"]),
        ({"part": "afternoon"}, ["game_today", "closed_tomorrow", "full_cafe"]),
        ({"part": "evening"}, ["meal_tonight", "week_edge", "far_sports"]),
        ({"part": "nonsense"}, ALL),
        ({"open_only": True}, ["game_today", "meal_tonight", "late_night", "study_tomorrow", "week_edge", "far_sports"]),
        ({"category": "game", "day": "tomorrow", "open_only": True}, ["late_night"]),
        ({"category": "game", "day": "tomorrow", "part": "afternoon"}, ["closed_tomorrow"]),
        ({"q": "스터디", "category": "study", "day": "tomorrow", "part": "morning", "open_only": True}, ["study_tomorrow"]),
    ],
)
def test_filter_meetings(app, seeded, kwargs, expected):
    assert run_filter(app, seeded, **kwargs) == expected


# ---------------------------------------------------------------------------
# GET /
# ---------------------------------------------------------------------------


def home(as_user, viewer, rendered, seeded, query=""):
    names = {meeting_id: name for name, meeting_id in seeded.items()}
    response = as_user(viewer).get("/" + query)
    assert response.status_code == 200
    name, context = rendered[-1]
    assert name == "home.html"
    return [names[m.id] for m in context["meetings"]], context, response


@pytest.mark.needs_template
def test_home_default_sort_is_soonest_first(as_user, viewer, rendered, seeded):
    listed, context, response = home(as_user, viewer, rendered, seeded)
    assert listed == ALL
    assert context["filtered"] is False
    assert context["filters"]["sort"] == "soon"
    assert context["total_open"] == 6
    html = response.get_data(as_text=True)
    assert "보드게임 한 판" in html and "Algorithm 스터디" in html


@pytest.mark.needs_template
def test_home_sort_new(as_user, viewer, rendered, seeded):
    listed, _, _ = home(as_user, viewer, rendered, seeded, "?sort=new")
    assert listed == [
        "far_sports", "week_edge", "closed_tomorrow", "meal_tonight", "full_cafe", "late_night", "study_tomorrow", "game_today",
    ]


@pytest.mark.needs_template
def test_home_sort_seats(as_user, viewer, rendered, seeded):
    listed, _, _ = home(as_user, viewer, rendered, seeded, "?sort=seats")
    # 참여 가능한 모임이 먼저(남은 자리 1, 3, 3, 3, 4, 5), 그 뒤에 마감·정원 찬 모임.
    assert listed == [
        "meal_tonight", "game_today", "week_edge", "far_sports", "study_tomorrow", "late_night", "full_cafe", "closed_tomorrow",
    ]


@pytest.mark.needs_template
@pytest.mark.parametrize(
    "query, expected",
    [
        ("?category=game", ["game_today", "late_night", "closed_tomorrow"]),
        ("?category=hacking", ALL),  # 모르는 종류는 무시
        ("?q=%EB%A7%88%EB%9D%BC%ED%83%95", ["meal_tonight"]),
        ("?q=++algorithm++", ["study_tomorrow"]),
        ("?day=today", ["game_today", "meal_tonight"]),
        ("?day=tomorrow", ["late_night", "study_tomorrow", "closed_tomorrow"]),
        ("?part=evening", ["meal_tonight", "week_edge", "far_sports"]),
        ("?open=1", ["game_today", "meal_tonight", "late_night", "study_tomorrow", "week_edge", "far_sports"]),
        ("?open=0", ALL),
        ("?category=game&open=1&sort=new", ["late_night", "game_today"]),
        ("?category=sports&sort=seats", ["week_edge", "far_sports"]),
    ],
)
def test_home_filters(as_user, viewer, rendered, seeded, query, expected):
    listed, context, _ = home(as_user, viewer, rendered, seeded, query)
    assert listed == expected


@pytest.mark.needs_template
def test_home_hides_cancelled_and_started(data, as_user, host, viewer, rendered):
    shown = data.meeting(host, title="보이는 모임", start_at=NOW_UTC + timedelta(minutes=1))
    data.meeting(host, title="취소된 모임", status="cancelled")
    data.meeting(host, title="진행 중인 모임", start_at=NOW_UTC - timedelta(minutes=10), duration_min=120)
    data.meeting(host, title="방금 시작한 모임", start_at=NOW_UTC)
    data.meeting(host, title="끝난 모임", start_at=NOW_UTC - timedelta(hours=5), duration_min=60)
    response = as_user(viewer).get("/")
    assert [m.id for m in rendered[-1][1]["meetings"]] == [shown]
    html = response.get_data(as_text=True)
    assert "보이는 모임" in html
    for title in ("취소된 모임", "진행 중인 모임", "방금 시작한 모임", "끝난 모임"):
        assert title not in html


@pytest.mark.needs_template
def test_home_for_you_uses_interests_only_when_unfiltered(data, as_user, rendered, seeded):
    fan = data.user("fan", interests="study,sports")
    client = as_user(fan)
    names = {meeting_id: name for name, meeting_id in seeded.items()}
    client.get("/")
    context = rendered[-1][1]
    assert {names[rec.meeting.id] for rec in context["for_you"]} <= {"study_tomorrow", "week_edge", "far_sports"}
    assert len(context["for_you"]) == 3
    client.get("/?category=game")
    assert rendered[-1][1]["for_you"] == []
    assert rendered[-1][1]["filtered"] is True


@pytest.mark.needs_template
def test_home_total_open_excludes_meetings_i_am_in(data, as_user, host, viewer, rendered, seeded):
    data.add_member(seeded["game_today"], viewer)
    as_user(viewer).get("/")
    assert rendered[-1][1]["total_open"] == 5


@pytest.mark.needs_template
def test_home_search_text_is_escaped(as_user, viewer):
    response = as_user(viewer).get("/?q=%3Cscript%3Ealert(1)%3C/script%3E")
    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.get_data(as_text=True)


@pytest.mark.needs_template
def test_other_pages_render(as_user, viewer):
    client = as_user(viewer)
    for path in ("/chat", "/me", "/meetings/new"):
        assert client.get(path).status_code == 200
