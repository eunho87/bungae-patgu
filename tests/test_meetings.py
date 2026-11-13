"""모임 만들기·참여·수정·상태·댓글 (meetings.py). 기준 시각 2026-11-12 14:00 KST."""

from datetime import timedelta

import pytest

from bungae import db, timeutil
from bungae.meetings import parse_tags, validate_meeting_form
from bungae.models import Comment, Meeting, Participation, User

from .conftest import NOW_UTC, kst, login_as

CONTACT = "https://open.kakao.com/o/gBungaeTest1"


def form(**over):
    values = {
        "title": "공강에 보드게임 한 판",
        "category": "game",
        "date": "2026-11-12",
        "time": "18:00",
        "duration": "120",
        "place": "학생회관 1층",
        "capacity": "4",
        "description": "초보 환영해요",
        "tags": "초면환영 가볍게",
        "contact_link": CONTACT,
    }
    values.update(over)
    return values


@pytest.fixture
def host(data):
    return data.user("host")


@pytest.fixture
def guest(data):
    return data.user("guest")


@pytest.fixture
def third(data):
    return data.user("third")


# ---------------------------------------------------------------------------
# validate_meeting_form (템플릿 없이 검증 규칙만)
# ---------------------------------------------------------------------------


def test_valid_form():
    cleaned, errors = validate_meeting_form(form())
    assert errors == {}
    assert cleaned == {
        "title": "공강에 보드게임 한 판",
        "category": "game",
        "start_at": kst(2026, 11, 12, 18, 0),  # 18:00 KST = 09:00 UTC
        "duration_min": 120,
        "place": "학생회관 1층",
        "capacity": 4,
        "description": "초보 환영해요",
        "tags": "초면환영,가볍게",
        "contact_link": CONTACT,
    }


def test_optional_fields_may_be_empty():
    _, errors = validate_meeting_form(form(description="", tags="", contact_link=""))
    assert errors == {}


@pytest.mark.parametrize(
    "over, field",
    [
        ({"date": "2026-11-12", "time": "13:59"}, "start"),  # 1분 전
        ({"date": "2026-11-12", "time": "14:00"}, "start"),  # 지금
        ({"date": "2026-11-11", "time": "18:00"}, "start"),  # 어제
        ({"date": "2025-11-12", "time": "18:00"}, "start"),
        ({"date": "2026-11-27", "time": "00:00"}, "start"),  # 달력에서 고를 수 있는 마지막 날(오늘+14일)의 다음 날
        ({"date": "2026-11-27", "time": "09:00"}, "start"),
        ({"date": "2027-01-01", "time": "09:00"}, "start"),
        ({"date": "", "time": ""}, "start"),
        ({"date": "2026-11-13", "time": ""}, "start"),
        ({"date": "2026-13-40", "time": "18:00"}, "start"),
        ({"date": "내일", "time": "저녁"}, "start"),
        ({"date": "2026-11-13", "time": "25:00"}, "start"),
        ({"capacity": "1"}, "capacity"),
        ({"capacity": "0"}, "capacity"),
        ({"capacity": "-3"}, "capacity"),
        ({"capacity": "21"}, "capacity"),
        ({"capacity": "999999999999999999999999"}, "capacity"),
        ({"capacity": "abc"}, "capacity"),
        ({"capacity": ""}, "capacity"),
        ({"capacity": "4.5"}, "capacity"),
        ({"category": "hacking"}, "category"),
        ({"category": ""}, "category"),
        ({"category": "GAME"}, "category"),
        ({"duration": "45"}, "duration"),
        ({"duration": "0"}, "duration"),
        ({"duration": "-60"}, "duration"),
        ({"duration": "100000"}, "duration"),
        ({"duration": "abc"}, "duration"),
        ({"duration": ""}, "duration"),
        ({"contact_link": "javascript:alert(1)"}, "contact_link"),
        ({"contact_link": "JaVaScRiPt:alert(1)"}, "contact_link"),
        ({"contact_link": "http://open.kakao.com/o/abc"}, "contact_link"),
        ({"contact_link": "data:text/html,<script>alert(1)</script>"}, "contact_link"),
        ({"contact_link": "//open.kakao.com/o/abc"}, "contact_link"),
        ({"contact_link": "open.kakao.com/o/abc"}, "contact_link"),
        ({"contact_link": "https://open.kakao.com/o/a b"}, "contact_link"),
        ({"contact_link": "javascript:alert(1)//https://open.kakao.com"}, "contact_link"),
        ({"contact_link": "https://open.kakao.com/" + "a" * 200}, "contact_link"),
        ({"tags": "하나 둘 셋 넷 다섯 여섯"}, "tags"),
        ({"tags": "#a,#b,#c,#d,#e,#f"}, "tags"),
        ({"title": "가"}, "title"),
        ({"title": "가" * 41}, "title"),
        ({"place": ""}, "place"),
        ({"place": "가" * 61}, "place"),
        ({"description": "가" * 501}, "description"),
    ],
)
def test_invalid_form(over, field):
    _, errors = validate_meeting_form(form(**over))
    assert field in errors
    assert set(errors) == {field}


@pytest.mark.parametrize(
    "over",
    [
        {"date": "2026-11-12", "time": "14:01"},
        {"date": "2026-11-26", "time": "14:00"},  # 정확히 14일 뒤
        {"capacity": "2"},
        {"capacity": "20"},
        {"duration": "30"},
        {"duration": "240"},
        {"tags": "하나 둘 셋 넷 다섯"},
        {"tags": "#하나, #하나, 하나"},
        {"title": "가나"},
        {"title": "가" * 40},
        {"place": "가" * 60},
        {"description": "가" * 500},
        {"contact_link": "https://open.kakao.com/" + "a" * 177},
    ],
)
def test_boundary_values_are_accepted(over):
    _, errors = validate_meeting_form(form(**over))
    assert errors == {}


def test_missing_fields_do_not_raise():
    cleaned, errors = validate_meeting_form({})
    assert {"title", "category", "start", "duration", "place", "capacity"} <= set(errors)


@pytest.mark.parametrize("date, time", [("0001-01-01", "00:00"), ("0001-01-01", "08:59")])
def test_year_one_date_is_a_validation_error_not_a_crash(date, time):
    """strptime은 0001년도 받아들이는데, KST->UTC로 9시간을 빼면서 OverflowError가 난다(요청은 500)."""
    _, errors = validate_meeting_form(form(date=date, time=time))
    assert "start" in errors


def test_parse_tags():
    assert parse_tags("초면환영 가볍게") == (["초면환영", "가볍게"], [])
    assert parse_tags("#초면환영,#가볍게  ,, 가볍게") == (["초면환영", "가볍게"], [])
    assert parse_tags("") == ([], []) and parse_tags(None) == ([], [])
    # 허용하지 않는 글자가 섞인 태그와 12자를 넘는 태그는 따로 돌려준다(조용히 버리지 않고 알려 주기 위해).
    assert parse_tags("<script> ok 가나다라마바사아자차카타파") == (["ok"], ["<script>", "가나다라마바사아자차카타파"])


def test_rejected_tags_are_reported_not_silently_dropped():
    _, errors = validate_meeting_form(form(tags="가볍게 c++ 😀"))
    assert "c++" in errors["tags"]


@pytest.mark.parametrize("title", ["", "   "])
def test_empty_title_is_filled_from_category_and_place(title):
    data, errors = validate_meeting_form(form(title=title, category="study", place="중앙도서관"))
    assert errors == {}
    assert data["title"] == "공부 번개 · 중앙도서관"


def test_empty_title_is_not_invented_when_category_is_missing():
    _, errors = validate_meeting_form(form(title="", category="nope"))
    assert {"title", "category"} <= set(errors)


@pytest.mark.parametrize("link", ["https://evil.example/login", "https://open.kakao.com/", "https://open.kakao.com/o/a\tb"])
def test_contact_link_must_be_a_kakao_open_chat_address(link):
    _, errors = validate_meeting_form(form(contact_link=link))
    assert "contact_link" in errors


# ---------------------------------------------------------------------------
# 만들기
# ---------------------------------------------------------------------------


def test_create_meeting(app, as_user, host):
    response = as_user(host).post("/meetings/new", data=form())
    assert response.status_code == 302
    with app.app_context():
        meeting = Meeting.query.one()
        assert response.headers["Location"] == f"/meetings/{meeting.id}"
        assert meeting.host_id == host
        assert meeting.status == "open" and meeting.state == "open"
        assert meeting.start_at == kst(2026, 11, 12, 18, 0)
        assert meeting.end_at == kst(2026, 11, 12, 20, 0)
        assert meeting.tag_list == ["초면환영", "가볍게"]
        assert meeting.contact_link == CONTACT
        # 호스트가 첫 번째 참여자이고 정원에 포함된다.
        assert [p.user_id for p in meeting.participations] == [host]
        assert meeting.joined_count == 1 and meeting.seats_left == 3


@pytest.mark.needs_template
@pytest.mark.parametrize(
    "over, field",
    [
        ({"date": "2026-11-12", "time": "13:00"}, "start"),
        ({"date": "2026-11-27", "time": "09:00"}, "start"),
        ({"capacity": "1"}, "capacity"),
        ({"capacity": "21"}, "capacity"),
        ({"category": "hacking"}, "category"),
        ({"duration": "45"}, "duration"),
        ({"contact_link": "javascript:alert(1)"}, "contact_link"),
        ({"contact_link": "http://open.kakao.com/o/abc"}, "contact_link"),
        ({"tags": "하나 둘 셋 넷 다섯 여섯"}, "tags"),
    ],
)
def test_create_rejects_invalid_input(app, as_user, host, rendered, over, field):
    response = as_user(host).post("/meetings/new", data=form(**over))
    assert response.status_code == 400
    assert field in rendered[-1][1]["errors"]
    with app.app_context():
        assert Meeting.query.count() == 0
        assert Participation.query.count() == 0


@pytest.mark.needs_template
def test_create_with_year_one_date_is_400(app, as_user, host):
    response = as_user(host).post("/meetings/new", data=form(date="0001-01-01", time="00:00"))
    assert response.status_code == 400
    with app.app_context():
        assert Meeting.query.count() == 0


@pytest.mark.needs_template
def test_create_form_renders_with_prefill(as_user, host, rendered):
    response = as_user(host).get("/meetings/new?category=game&date=2026-11-13&time=15:00&duration=90")
    assert response.status_code == 200
    values = rendered[-1][1]["values"]
    assert (values["category"], values["date"], values["time"], values["duration"]) == ("game", "2026-11-13", "15:00", "90")
    assert values["capacity"] == "4"


@pytest.mark.needs_template
def test_rejected_javascript_link_is_not_rendered_as_a_link(as_user, host):
    response = as_user(host).post("/meetings/new", data=form(contact_link="javascript:alert(1)"))
    assert 'href="javascript:' not in response.get_data(as_text=True).lower()


# ---------------------------------------------------------------------------
# 참여 / 취소
# ---------------------------------------------------------------------------


def test_join(app, data, as_user, host, guest):
    meeting = data.meeting(host)
    response = as_user(guest).post(f"/meetings/{meeting}/join")
    assert response.status_code == 302
    assert response.headers["Location"] == f"/meetings/{meeting}"
    assert data.member_ids(meeting) == [host, guest]
    assert data.meeting_field(meeting, "joined_count") == 2
    assert data.meeting_field(meeting, "seats_left") == 2


def test_duplicate_join_adds_no_second_row(data, as_user, host, guest):
    meeting = data.meeting(host)
    client = as_user(guest)
    for _ in range(3):
        assert client.post(f"/meetings/{meeting}/join").status_code == 302
    assert data.member_ids(meeting) == [host, guest]


def test_host_join_is_a_no_op(data, as_user, host):
    meeting = data.meeting(host)
    as_user(host).post(f"/meetings/{meeting}/join")
    assert data.member_ids(meeting) == [host]


def test_join_full_meeting_is_refused(data, as_user, host, guest, third):
    meeting = data.meeting(host, capacity=2)
    as_user(guest).post(f"/meetings/{meeting}/join")
    assert data.meeting_field(meeting, "state") == "full"
    as_user(third).post(f"/meetings/{meeting}/join")
    assert data.member_ids(meeting) == [host, guest]


@pytest.mark.parametrize("status", ["closed", "cancelled"])
def test_join_closed_or_cancelled_meeting_is_refused(data, as_user, host, guest, status):
    meeting = data.meeting(host, status=status)
    assert as_user(guest).post(f"/meetings/{meeting}/join").status_code == 302
    assert data.member_ids(meeting) == [host]


@pytest.mark.parametrize("after", [timedelta(0), timedelta(minutes=1), timedelta(hours=3)])
def test_join_started_meeting_is_refused(data, as_user, host, guest, after):
    start = NOW_UTC + timedelta(hours=2)
    meeting = data.meeting(host, start_at=start, duration_min=60)
    timeutil.set_fixed_now(start + after)
    as_user(guest).post(f"/meetings/{meeting}/join")
    assert data.member_ids(meeting) == [host]


def test_join_one_minute_before_start_is_allowed(data, as_user, host, guest):
    start = NOW_UTC + timedelta(hours=2)
    meeting = data.meeting(host, start_at=start)
    timeutil.set_fixed_now(start - timedelta(minutes=1))
    as_user(guest).post(f"/meetings/{meeting}/join")
    assert data.member_ids(meeting) == [host, guest]


@pytest.mark.needs_template
def test_join_missing_meeting_is_404(as_user, guest):
    assert as_user(guest).post("/meetings/999/join").status_code == 404


def test_join_is_post_only(data, as_user, host, guest):
    meeting = data.meeting(host)
    assert as_user(guest).get(f"/meetings/{meeting}/join").status_code == 405
    assert data.member_ids(meeting) == [host]


def test_leave_frees_a_seat(data, as_user, host, guest, third):
    meeting = data.meeting(host, capacity=2)
    as_user(guest).post(f"/meetings/{meeting}/join")
    as_user(third).post(f"/meetings/{meeting}/join")
    assert data.member_ids(meeting) == [host, guest]

    assert as_user(guest).post(f"/meetings/{meeting}/leave").status_code == 302
    assert data.member_ids(meeting) == [host]
    assert data.meeting_field(meeting, "state") == "open"

    as_user(third).post(f"/meetings/{meeting}/join")
    assert data.member_ids(meeting) == [host, third]


def test_host_cannot_leave(data, as_user, host, guest):
    meeting = data.meeting(host, members=[guest])
    assert as_user(host).post(f"/meetings/{meeting}/leave").status_code == 302
    assert data.member_ids(meeting) == [host, guest]
    assert data.meeting_field(meeting, "host_id") == host


def test_cannot_leave_after_start(data, as_user, host, guest):
    start = NOW_UTC + timedelta(hours=2)
    meeting = data.meeting(host, start_at=start, members=[guest])
    timeutil.set_fixed_now(start + timedelta(minutes=5))
    as_user(guest).post(f"/meetings/{meeting}/leave")
    assert data.member_ids(meeting) == [host, guest]


def test_leave_by_non_member_changes_nothing(data, as_user, host, guest, third):
    meeting = data.meeting(host, members=[guest])
    as_user(third).post(f"/meetings/{meeting}/leave")
    assert data.member_ids(meeting) == [host, guest]


def test_rejoin_after_leaving(data, as_user, host, guest):
    meeting = data.meeting(host)
    client = as_user(guest)
    client.post(f"/meetings/{meeting}/join")
    client.post(f"/meetings/{meeting}/leave")
    client.post(f"/meetings/{meeting}/join")
    assert data.member_ids(meeting) == [host, guest]


# ---------------------------------------------------------------------------
# 수정
# ---------------------------------------------------------------------------


def test_host_can_edit(app, data, as_user, host):
    meeting = data.meeting(host, title="옛 제목", capacity=4)
    response = as_user(host).post(
        f"/meetings/{meeting}/edit",
        data=form(title="새 제목", capacity="6", date="2026-11-13", time="10:30", duration="90", category="study"),
    )
    assert response.status_code == 302
    with app.app_context():
        row = db.session.get(Meeting, meeting)
        assert (row.title, row.capacity, row.duration_min, row.category) == ("새 제목", 6, 90, "study")
        assert row.start_at == kst(2026, 11, 13, 10, 30)
        assert row.host_id == host and row.status == "open"
        assert [p.user_id for p in row.participations] == [host]


@pytest.mark.needs_template
def test_non_host_cannot_edit(data, as_user, host, guest):
    meeting = data.meeting(host, title="옛 제목", members=[guest])
    client = as_user(guest)
    assert client.get(f"/meetings/{meeting}/edit").status_code == 403
    assert client.post(f"/meetings/{meeting}/edit", data=form(title="가로챈 제목")).status_code == 403
    assert data.meeting_field(meeting, "title") == "옛 제목"


@pytest.mark.needs_template
@pytest.mark.parametrize("action", ["close", "cancel", "reopen"])
def test_non_host_cannot_change_status(data, as_user, host, guest, action):
    meeting = data.meeting(host, members=[guest], status="closed" if action == "reopen" else "open")
    before = data.meeting_field(meeting, "status")
    response = as_user(guest).post(f"/meetings/{meeting}/status", data={"action": action})
    assert response.status_code == 403
    assert data.meeting_field(meeting, "status") == before


@pytest.mark.needs_template
def test_capacity_cannot_be_edited_below_participant_count(data, as_user, host, guest, third, rendered):
    meeting = data.meeting(host, capacity=5, members=[guest, third])
    response = as_user(host).post(f"/meetings/{meeting}/edit", data=form(capacity="2"))
    assert response.status_code == 400
    assert "capacity" in rendered[-1][1]["errors"]
    assert data.meeting_field(meeting, "capacity") == 5


def test_capacity_rule_in_validator(app, data, host, guest, third):
    meeting = data.meeting(host, capacity=5, members=[guest, third])
    with app.app_context():
        existing = db.session.get(Meeting, meeting)
        assert "capacity" in validate_meeting_form(form(capacity="2"), existing=existing)[1]
        assert validate_meeting_form(form(capacity="3"), existing=existing)[1] == {}


def test_capacity_can_be_lowered_to_exactly_the_participant_count(data, as_user, host, guest, third):
    meeting = data.meeting(host, capacity=5, members=[guest, third])
    assert as_user(host).post(f"/meetings/{meeting}/edit", data=form(capacity="3")).status_code == 302
    assert data.meeting_field(meeting, "capacity") == 3
    assert data.meeting_field(meeting, "state") == "full"


def test_cancelled_or_started_meeting_cannot_be_edited(data, as_user, host):
    cancelled = data.meeting(host, title="옛 제목", status="cancelled")
    started = data.meeting(host, title="옛 제목", start_at=NOW_UTC - timedelta(minutes=5), duration_min=120)
    client = as_user(host)
    for meeting in (cancelled, started):
        assert client.post(f"/meetings/{meeting}/edit", data=form(title="새 제목")).status_code == 302
        assert data.meeting_field(meeting, "title") == "옛 제목"


@pytest.mark.needs_template
def test_edit_form_shows_current_values(data, as_user, host, rendered):
    meeting = data.meeting(host, title="옛 제목", start_at=kst(2026, 11, 13, 9, 30), duration_min=90, capacity=6, tags="가볍게,수다")
    assert as_user(host).get(f"/meetings/{meeting}/edit").status_code == 200
    values = rendered[-1][1]["values"]
    assert (values["title"], values["date"], values["time"]) == ("옛 제목", "2026-11-13", "09:30")
    assert (values["duration"], values["capacity"], values["tags"]) == ("90", "6", "가볍게 수다")


# ---------------------------------------------------------------------------
# 상태 바꾸기
# ---------------------------------------------------------------------------


def test_close_then_reopen(data, as_user, host, guest):
    meeting = data.meeting(host)
    owner = as_user(host)
    assert owner.post(f"/meetings/{meeting}/status", data={"action": "close"}).status_code == 302
    assert data.meeting_field(meeting, "status") == "closed"
    assert data.meeting_field(meeting, "state") == "closed"
    as_user(guest).post(f"/meetings/{meeting}/join")
    assert data.member_ids(meeting) == [host]

    assert owner.post(f"/meetings/{meeting}/status", data={"action": "reopen"}).status_code == 302
    assert data.meeting_field(meeting, "status") == "open"
    as_user(guest).post(f"/meetings/{meeting}/join")
    assert data.member_ids(meeting) == [host, guest]


def test_cancel_is_final(data, as_user, host, guest):
    meeting = data.meeting(host, members=[guest])
    owner = as_user(host)
    owner.post(f"/meetings/{meeting}/status", data={"action": "cancel"})
    assert data.meeting_field(meeting, "status") == "cancelled"
    assert data.meeting_field(meeting, "state") == "cancelled"
    for action in ("reopen", "close"):
        owner.post(f"/meetings/{meeting}/status", data={"action": action})
        assert data.meeting_field(meeting, "status") == "cancelled"


def test_status_cannot_change_after_start(data, as_user, host):
    meeting = data.meeting(host, start_at=NOW_UTC - timedelta(minutes=1), duration_min=120)
    as_user(host).post(f"/meetings/{meeting}/status", data={"action": "cancel"})
    assert data.meeting_field(meeting, "status") == "open"
    assert data.meeting_field(meeting, "state") == "ongoing"


@pytest.mark.parametrize("action", ["", "delete", "open", "CLOSE", None])
def test_unknown_status_action_is_400(data, as_user, host, action):
    meeting = data.meeting(host)
    payload = {} if action is None else {"action": action}
    assert as_user(host).post(f"/meetings/{meeting}/status", data=payload).status_code == 400
    assert data.meeting_field(meeting, "status") == "open"


def test_state_is_derived(app, data, host, guest):
    meeting = data.meeting(host, capacity=2, start_at=NOW_UTC + timedelta(hours=3))
    assert data.meeting_field(meeting, "state") == "open"
    assert data.meeting_field(meeting, "closing_soon") is False  # 2명짜리는 처음부터 한 자리라 임박이 아니다
    data.add_member(meeting, guest)
    assert data.meeting_field(meeting, "state") == "full"
    assert data.meeting_field(meeting, "status") == "open"  # 저장된 값은 호스트 의사뿐
    timeutil.set_fixed_now(NOW_UTC + timedelta(hours=3))  # 시작 시각
    assert data.meeting_field(meeting, "state") == "ongoing"
    assert data.meeting_field(meeting, "joinable") is False
    timeutil.set_fixed_now(NOW_UTC + timedelta(hours=3, minutes=59))
    assert data.meeting_field(meeting, "state") == "ongoing"
    timeutil.set_fixed_now(NOW_UTC + timedelta(hours=4))  # 끝나는 시각
    assert data.meeting_field(meeting, "state") == "ended"
    assert data.meeting_field(meeting, "status") == "open"


def test_cancelled_wins_over_time_based_states(data, host):
    meeting = data.meeting(host, start_at=NOW_UTC - timedelta(hours=5), duration_min=60, status="cancelled")
    assert data.meeting_field(meeting, "state") == "cancelled"
    closed = data.meeting(host, capacity=2, status="closed")
    assert data.meeting_field(closed, "state") == "closed"


# ---------------------------------------------------------------------------
# 댓글
# ---------------------------------------------------------------------------


def test_add_comment(app, data, as_user, host, guest):
    meeting = data.meeting(host)
    response = as_user(guest).post(f"/meetings/{meeting}/comments", data={"body": "  저도   가도\n될까요? "})
    assert response.status_code == 302
    assert response.headers["Location"] == f"/meetings/{meeting}#comments"
    with app.app_context():
        comment = Comment.query.one()
        assert (comment.meeting_id, comment.user_id, comment.body) == (meeting, guest, "저도 가도 될까요?")


def test_comment_length_limit(data, as_user, host, guest):
    meeting = data.meeting(host)
    client = as_user(guest)
    client.post(f"/meetings/{meeting}/comments", data={"body": "가" * 200})
    assert data.count(Comment) == 1
    client.post(f"/meetings/{meeting}/comments", data={"body": "가" * 201})
    assert data.count(Comment) == 1


@pytest.mark.parametrize("body", ["", "   ", "\n\t"])
def test_empty_comment_is_refused(data, as_user, host, guest, body):
    meeting = data.meeting(host)
    as_user(guest).post(f"/meetings/{meeting}/comments", data={"body": body})
    assert data.count(Comment) == 0


def test_no_comment_on_cancelled_meeting(data, as_user, host, guest):
    meeting = data.meeting(host, status="cancelled", members=[guest])
    as_user(guest).post(f"/meetings/{meeting}/comments", data={"body": "아쉽네요"})
    assert data.count(Comment) == 0


def test_comment_cap_per_meeting(app, data, as_user, host, guest):
    meeting = data.meeting(host)
    with app.app_context():
        db.session.add_all(Comment(meeting_id=meeting, user_id=host, body=f"댓글 {n}") for n in range(99))
        db.session.commit()
    client = as_user(guest)
    client.post(f"/meetings/{meeting}/comments", data={"body": "100번째"})
    assert data.count(Comment) == 100
    client.post(f"/meetings/{meeting}/comments", data={"body": "101번째"})
    assert data.count(Comment) == 100


@pytest.mark.needs_template
def test_comment_on_missing_meeting_is_404(data, as_user, guest):
    assert as_user(guest).post("/meetings/999/comments", data={"body": "hi"}).status_code == 404
    assert data.count(Comment) == 0


def test_author_can_delete_comment(data, as_user, host, guest):
    meeting = data.meeting(host)
    comment = data.comment(meeting, guest)
    response = as_user(guest).post(f"/comments/{comment}/delete")
    assert response.status_code == 302
    assert response.headers["Location"] == f"/meetings/{meeting}#comments"
    assert data.count(Comment) == 0


def test_host_can_delete_any_comment(data, as_user, host, guest):
    meeting = data.meeting(host)
    comment = data.comment(meeting, guest)
    assert as_user(host).post(f"/comments/{comment}/delete").status_code == 302
    assert data.count(Comment) == 0


@pytest.mark.needs_template
def test_third_user_cannot_delete_comment(data, as_user, host, guest, third):
    meeting = data.meeting(host, members=[third])
    comment = data.comment(meeting, guest)
    assert as_user(third).post(f"/comments/{comment}/delete").status_code == 403
    assert data.count(Comment) == 1


@pytest.mark.needs_template
def test_host_of_another_meeting_cannot_delete_comment(data, as_user, host, guest, third):
    meeting = data.meeting(host)
    data.meeting(third, title="다른 모임", start_at=NOW_UTC + timedelta(hours=8))
    comment = data.comment(meeting, guest)
    assert as_user(third).post(f"/comments/{comment}/delete").status_code == 403
    assert data.count(Comment) == 1


@pytest.mark.needs_template
def test_delete_missing_comment_is_404(as_user, guest):
    assert as_user(guest).post("/comments/999/delete").status_code == 404


def test_comment_delete_is_post_only(data, as_user, host, guest):
    meeting = data.meeting(host)
    comment = data.comment(meeting, guest)
    assert as_user(guest).get(f"/comments/{comment}/delete").status_code == 405
    assert data.count(Comment) == 1


# ---------------------------------------------------------------------------
# 상세 화면
# ---------------------------------------------------------------------------


@pytest.mark.needs_template
def test_contact_link_is_shown_to_members_only(data, as_user, host, guest, third):
    meeting = data.meeting(host, contact_link=CONTACT, members=[guest])

    for member in (host, guest):
        response = as_user(member).get(f"/meetings/{meeting}")
        assert response.status_code == 200
        assert CONTACT in response.get_data(as_text=True)

    outsider = as_user(third)
    response = outsider.get(f"/meetings/{meeting}")
    assert response.status_code == 200
    assert CONTACT not in response.get_data(as_text=True)
    assert "open.kakao.com" not in response.get_data(as_text=True)

    # 참여하면 보이고, 나가면 다시 안 보인다.
    outsider.post(f"/meetings/{meeting}/join")
    assert CONTACT in outsider.get(f"/meetings/{meeting}").get_data(as_text=True)
    outsider.post(f"/meetings/{meeting}/leave")
    assert CONTACT not in outsider.get(f"/meetings/{meeting}").get_data(as_text=True)


@pytest.mark.needs_template
def test_contact_link_is_not_leaked_on_other_pages(data, as_user, host, third):
    data.meeting(host, contact_link=CONTACT)
    outsider = as_user(third)
    for path in ("/", "/me", "/chat"):
        response = outsider.get(path)
        assert response.status_code == 200
        assert "open.kakao.com" not in response.get_data(as_text=True)


@pytest.mark.needs_template
def test_detail_context(data, as_user, host, guest, third, rendered):
    meeting = data.meeting(host, members=[guest])
    as_user(host).get(f"/meetings/{meeting}")
    assert (rendered[-1][1]["is_host"], rendered[-1][1]["is_member"]) == (True, True)
    as_user(guest).get(f"/meetings/{meeting}")
    assert (rendered[-1][1]["is_host"], rendered[-1][1]["is_member"]) == (False, True)
    as_user(third).get(f"/meetings/{meeting}")
    assert (rendered[-1][1]["is_host"], rendered[-1][1]["is_member"]) == (False, False)
    assert rendered[-1][0] == "meeting_detail.html"
    # 호스트가 실제로 치른 모임만 센다: 취소한 것과 아직 시작하지 않은 것은 빼고, 남의 모임에 참여한 수도 같은 기준이다.
    assert rendered[-1][1]["host_stats"] == {"hosted": 0, "joined": 0}
    data.meeting(host, title="취소한 모임", start_at=NOW_UTC - timedelta(hours=9), status="cancelled")
    data.meeting(host, title="치른 모임", start_at=NOW_UTC - timedelta(hours=30))
    data.meeting(third, title="남의 지난 모임", start_at=NOW_UTC - timedelta(hours=12), members=[host])
    data.meeting(third, title="남의 예정 모임", start_at=NOW_UTC + timedelta(hours=12), members=[host])
    as_user(third).get(f"/meetings/{meeting}")
    assert rendered[-1][1]["host_stats"] == {"hosted": 1, "joined": 1}


@pytest.mark.needs_template
def test_detail_warns_about_schedule_conflict(data, as_user, host, guest, third, rendered):
    mine = data.meeting(guest, title="내 모임", start_at=NOW_UTC + timedelta(hours=2), duration_min=120)
    clash = data.meeting(host, title="겹치는 모임", start_at=NOW_UTC + timedelta(hours=3), duration_min=60)
    free = data.meeting(host, title="안 겹치는 모임", start_at=NOW_UTC + timedelta(hours=6), duration_min=60)
    client = as_user(guest)
    client.get(f"/meetings/{clash}")
    assert rendered[-1][1]["conflict"].id == mine
    client.get(f"/meetings/{free}")
    assert rendered[-1][1]["conflict"] is None


@pytest.mark.needs_template
def test_detail_escapes_user_text(data, as_user, host, guest):
    meeting = data.meeting(
        host,
        title="<script>alert(1)</script>",
        description="<img src=x onerror=alert(2)>",
        place="<b>학생회관</b>",
    )
    data.comment(meeting, guest, body="<svg onload=alert(3)>")
    html = as_user(guest).get(f"/meetings/{meeting}").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "<img src=x onerror=alert(2)>" not in html
    assert "<b>학생회관</b>" not in html
    assert "<svg onload=alert(3)>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


@pytest.mark.needs_template
def test_detail_missing_meeting_is_404(as_user, guest):
    assert as_user(guest).get("/meetings/999").status_code == 404


# ---------------------------------------------------------------------------
# 내 모임
# ---------------------------------------------------------------------------


@pytest.mark.needs_template
def test_me_page_groups_meetings(data, as_user, host, guest, rendered):
    hosting = data.meeting(guest, title="내가 연 모임", start_at=NOW_UTC + timedelta(hours=2))
    joining = data.meeting(host, title="참여 중", start_at=NOW_UTC + timedelta(hours=5), members=[guest])
    ongoing = data.meeting(host, title="진행 중", start_at=NOW_UTC - timedelta(minutes=30), duration_min=120, members=[guest])
    ended = data.meeting(host, title="끝난 모임", start_at=NOW_UTC - timedelta(hours=5), duration_min=60, members=[guest])
    cancelled = data.meeting(host, title="취소된 모임", start_at=NOW_UTC + timedelta(hours=9), members=[guest], status="cancelled")
    data.meeting(host, title="남의 모임", start_at=NOW_UTC + timedelta(hours=12))

    assert as_user(guest).get("/me").status_code == 200
    name, context = rendered[-1]
    assert name == "me.html"
    assert [m.id for m in context["hosting"]] == [hosting]
    assert [m.id for m in context["joining"]] == [ongoing, joining]
    assert {m.id for m in context["past"]} == {ended, cancelled}


def test_update_interests_keeps_only_known_keys(app, client, guest):
    login_as(client, guest)
    response = client.post("/me/interests", data={"interests": ["study", "hacking", "meal", "<script>"]})
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(User, guest).interest_list == ["meal", "study"]
    client.post("/me/interests", data={})
    with app.app_context():
        assert db.session.get(User, guest).interest_list == []
