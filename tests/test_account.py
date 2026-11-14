"""알림, 신고, 비밀번호 변경, 회원 탈퇴, 가입 동의."""

from datetime import timedelta

from werkzeug.security import check_password_hash

from bungae import db
from bungae.models import Comment, Meeting, Notification, Participation, Report, User

from .conftest import NOW_UTC, PASSWORD


def notes(app, user_id):
    with app.app_context():
        rows = Notification.query.filter_by(user_id=user_id).order_by(Notification.id).all()
        return [(row.message, row.meeting_id, row.is_read) for row in rows]


# --- 알림 -------------------------------------------------------------------


def test_join_notifies_host_but_not_the_joiner(app, data, as_user):
    host, guest = data.user("호스트"), data.user("게스트")
    meeting = data.meeting(host, title="보드게임", capacity=3)
    as_user(guest).post(f"/meetings/{meeting}/join")
    host_notes = notes(app, host)
    assert len(host_notes) == 1
    assert "게스트님이" in host_notes[0][0] and "(2/3명)" in host_notes[0][0]
    assert host_notes[0][1] == meeting
    assert notes(app, guest) == []


def test_join_that_fills_the_meeting_says_so(app, data, as_user):
    host, guest = data.user(), data.user()
    meeting = data.meeting(host, capacity=2)
    as_user(guest).post(f"/meetings/{meeting}/join")
    assert "정원이 다 찼어요" in notes(app, host)[0][0]


def test_refused_join_creates_no_notification(app, data, as_user):
    host, member, late = data.user(), data.user(), data.user()
    meeting = data.meeting(host, capacity=2, members=[member])
    as_user(late).post(f"/meetings/{meeting}/join")
    assert notes(app, host) == []


def test_leave_notifies_host(app, data, as_user):
    host, guest = data.user(), data.user("떠나는사람")
    meeting = data.meeting(host, members=[guest])
    as_user(guest).post(f"/meetings/{meeting}/leave")
    assert "떠나는사람님이" in notes(app, host)[0][0]


def test_comment_by_guest_notifies_host_and_host_comment_notifies_members(app, data, as_user):
    host, member, outsider = data.user(), data.user(), data.user()
    meeting = data.meeting(host, members=[member])
    as_user(outsider).post(f"/meetings/{meeting}/comments", data={"body": "몇 시에 끝나요?"})
    assert len(notes(app, host)) == 1
    assert notes(app, member) == []
    as_user(host).post(f"/meetings/{meeting}/comments", data={"body": "8시에 끝나요"})
    assert len(notes(app, host)) == 1  # 자기 댓글로는 알림이 오지 않는다
    assert "호스트가" in notes(app, member)[0][0]
    assert "호스트가" in notes(app, outsider)[0][0]  # 질문을 남긴 사람은 참여자가 아니어도 답을 알림으로 받는다
    bystander = data.user()
    assert notes(app, bystander) == []


def test_cancel_notifies_members(app, data, as_user):
    host, member = data.user(), data.user()
    meeting = data.meeting(host, title="취소될 모임", members=[member])
    as_user(host).post(f"/meetings/{meeting}/status", data={"action": "cancel"})
    assert "취소됐어요" in notes(app, member)[0][0]
    assert notes(app, host) == []


def edit_form(**over):
    form = {
        "title": "보드게임 한 판",
        "category": "game",
        "date": "2026-11-12",
        "time": "16:00",
        "duration": "60",
        "place": "학생회관",
        "capacity": "4",
        "description": "",
        "tags": "",
        "contact_link": "",
    }
    form.update(over)
    return form


def test_edit_notifies_members_only_when_time_or_place_changes(app, data, as_user):
    host, member = data.user(), data.user()
    meeting = data.meeting(host, members=[member])  # 오늘 16:00 KST, 60분, 학생회관
    client = as_user(host)
    assert client.post(f"/meetings/{meeting}/edit", data=edit_form(description="소개만 바꿈")).status_code == 302
    assert notes(app, member) == []
    assert client.post(f"/meetings/{meeting}/edit", data=edit_form(place="중앙도서관")).status_code == 302
    assert "바뀌었어요" in notes(app, member)[0][0]


def test_bell_shows_unread_count_and_opening_the_inbox_clears_it(app, data, as_user):
    host, guest = data.user(), data.user()
    meeting = data.meeting(host)
    as_user(guest).post(f"/meetings/{meeting}/join")
    client = as_user(host)
    assert "새 알림 1개" in client.get("/").get_data(as_text=True)
    inbox = client.get("/notifications").get_data(as_text=True)
    assert "참여했어요" in inbox and "새 알림" in inbox
    assert notes(app, host)[0][2] is True
    assert "새 알림 1개" not in client.get("/").get_data(as_text=True)


def test_inbox_requires_login_and_shows_only_my_notifications(app, data, as_user, client):
    host, guest = data.user(), data.user()
    meeting = data.meeting(host, title="남의알림")
    as_user(guest).post(f"/meetings/{meeting}/join")
    assert client.get("/notifications").status_code == 302
    assert "남의알림" not in as_user(guest).get("/notifications").get_data(as_text=True)


def test_home_reminds_me_of_my_meeting_starting_soon(data, as_user):
    host, other = data.user(), data.user()
    data.meeting(host, title="곧시작모임", start_at=NOW_UTC + timedelta(minutes=50))
    data.meeting(host, title="내일모임", start_at=NOW_UTC + timedelta(hours=20))
    assert "곧 시작해요 · 곧시작모임" in as_user(host).get("/").get_data(as_text=True)
    assert "곧 시작해요 · 내일모임" not in as_user(host).get("/").get_data(as_text=True)
    assert "곧 시작해요" not in as_user(other).get("/").get_data(as_text=True)


# --- 신고 -------------------------------------------------------------------


def test_report_is_stored_once_per_user(app, data, as_user):
    host, guest = data.user(), data.user()
    meeting = data.meeting(host)
    client = as_user(guest)
    client.post(f"/meetings/{meeting}/report", data={"reason": "spam"})
    client.post(f"/meetings/{meeting}/report", data={"reason": "money"})
    assert data.count(Report) == 1
    assert "이미 신고한 모임" in client.get(f"/meetings/{meeting}").get_data(as_text=True)


def test_report_rejects_unknown_reason_and_self_report(data, as_user):
    host, guest = data.user(), data.user()
    meeting = data.meeting(host)
    as_user(guest).post(f"/meetings/{meeting}/report", data={"reason": "<script>"})
    as_user(host).post(f"/meetings/{meeting}/report", data={"reason": "spam"})
    assert data.count(Report) == 0


def age_accounts(app, user_ids, days=2):
    """가입한 지 오래된 계정으로 만든다(신고는 가입 24시간이 지난 계정이나 참여자의 것만 센다)."""
    with app.app_context():
        for user_id in user_ids:
            db.session.get(User, user_id).created_at = NOW_UTC - timedelta(days=days)
        db.session.commit()


def test_reports_from_fresh_accounts_do_not_close_a_meeting(app, data, as_user):
    host = data.user()
    meeting = data.meeting(host)
    for _ in range(5):
        as_user(data.user()).post(f"/meetings/{meeting}/report", data={"reason": "spam"})
    assert data.count(Report) == 5
    assert data.meeting_field(meeting, "status") == "open"
    assert "모집이 중단된 모임" not in as_user(host).get(f"/meetings/{meeting}").get_data(as_text=True)


def test_reports_from_participants_count_even_if_the_account_is_new(app, data, as_user):
    host = data.user()
    members = [data.user() for _ in range(3)]
    meeting = data.meeting(host, capacity=6, members=members)
    for member in members:
        as_user(member).post(f"/meetings/{meeting}/report", data={"reason": "money"})
    assert data.meeting_field(meeting, "status") == "blocked"


def test_three_reports_stop_recruiting_and_block_reopen(app, data, as_user):
    host = data.user()
    reporters = [data.user() for _ in range(3)]
    newcomer = data.user()
    age_accounts(app, reporters)
    meeting = data.meeting(host)
    for index, reporter in enumerate(reporters):
        as_user(reporter).post(f"/meetings/{meeting}/report", data={"reason": "money"})
        expected = "blocked" if index == 2 else "open"
        assert data.meeting_field(meeting, "status") == expected
    assert "신고가 여러 건" in notes(app, host)[0][0]
    as_user(host).post(f"/meetings/{meeting}/status", data={"action": "reopen"})
    assert data.meeting_field(meeting, "status") == "blocked"
    as_user(host).post(f"/meetings/{meeting}/status", data={"action": "close"})
    assert data.meeting_field(meeting, "status") == "blocked"  # 마감으로 바꿨다가 다시 여는 우회도 막는다
    as_user(newcomer).post(f"/meetings/{meeting}/join")
    assert newcomer not in data.member_ids(meeting)
    page = as_user(newcomer).get(f"/meetings/{meeting}").get_data(as_text=True)
    assert "모집이 중단된 모임" in page


def test_host_does_not_see_the_report_form(data, as_user):
    host = data.user()
    meeting = data.meeting(host)
    assert "신고하기" not in as_user(host).get(f"/meetings/{meeting}").get_data(as_text=True)


# --- 비밀번호 변경 ----------------------------------------------------------


def password_of(app, user_id):
    with app.app_context():
        return db.session.get(User, user_id).password_hash


def test_change_password_requires_the_current_one(app, data, as_user):
    user = data.user()
    client = as_user(user)
    before = password_of(app, user)
    client.post("/me/password", data={"current_password": "wrong", "new_password": "new-pass-123", "new_password_confirm": "new-pass-123"})
    assert password_of(app, user) == before
    client.post("/me/password", data={"current_password": PASSWORD, "new_password": "short", "new_password_confirm": "short"})
    assert password_of(app, user) == before
    client.post("/me/password", data={"current_password": PASSWORD, "new_password": "new-pass-123", "new_password_confirm": "different-123"})
    assert password_of(app, user) == before
    client.post("/me/password", data={"current_password": PASSWORD, "new_password": "new-pass-123", "new_password_confirm": "new-pass-123"})
    assert check_password_hash(password_of(app, user), "new-pass-123")


# --- 회원 탈퇴 --------------------------------------------------------------


def test_delete_account_needs_the_password(data, as_user):
    user = data.user()
    as_user(user).post("/me/delete", data={"password": "wrong"})
    assert data.count(User) == 1


def test_delete_account_removes_everything_and_tells_affected_people(app, data, as_user):
    leaver, member, other_host = data.user("탈퇴자"), data.user(), data.user()
    hosted = data.meeting(leaver, title="탈퇴자의 모임", members=[member])
    joined = data.meeting(other_host, title="남의 모임", members=[leaver], start_at=NOW_UTC + timedelta(hours=5))
    data.comment(joined, leaver, "탈퇴자가 쓴 댓글")
    data.comment(hosted, member, "탈퇴자 모임에 달린 댓글")
    as_user(member).post(f"/meetings/{joined}/report", data={"reason": "etc"})
    as_user(leaver).post(f"/meetings/{joined}/report", data={"reason": "etc"})

    client = as_user(leaver)
    response = client.post("/me/delete", data={"password": PASSWORD})
    assert response.status_code == 302 and response.headers["Location"].endswith("/login")
    assert client.get("/").status_code == 302  # 로그아웃됨

    with app.app_context():
        assert db.session.get(User, leaver) is None
        assert db.session.get(Meeting, hosted) is None
        assert Participation.query.filter_by(user_id=leaver).count() == 0
        assert Comment.query.filter_by(user_id=leaver).count() == 0
        assert Comment.query.filter_by(meeting_id=hosted).count() == 0
        assert Report.query.filter_by(user_id=leaver).count() == 0
        assert Report.query.filter_by(meeting_id=joined).count() == 1
        assert Notification.query.filter_by(user_id=leaver).count() == 0
    assert leaver not in data.member_ids(joined)

    member_notes = notes(app, member)
    assert any("탈퇴자의 모임" in message and "취소" in message and meeting_id is None for message, meeting_id, _ in member_notes)
    host_notes = [message for message, _, _ in notes(app, other_host)]
    assert any("참여자 한 명이 탈퇴해" in message for message in host_notes)
    assert not any("탈퇴자" in message.replace("탈퇴해", "") for message in host_notes)  # 닉네임이 남지 않는다
    # 남은 사람의 화면이 깨지지 않는다
    assert as_user(member).get("/notifications").status_code == 200
    assert as_user(other_host).get(f"/meetings/{joined}").status_code == 200


def test_delete_account_scrubs_my_nickname_from_other_peoples_notifications(app, data, as_user):
    host, leaver = data.user(), data.user("곧탈퇴")
    meeting = data.meeting(host)
    as_user(leaver).post(f"/meetings/{meeting}/comments", data={"body": "안녕하세요"})
    assert any("곧탈퇴님이" in message for message, _, _ in notes(app, host))
    as_user(leaver).post("/me/delete", data={"password": PASSWORD})
    assert not any("곧탈퇴" in message for message, _, _ in notes(app, host))


def test_password_change_logs_out_other_devices(app, data, client):
    user = data.user(email="twodev@ssu.ac.kr")
    phone, laptop = app.test_client(), app.test_client()
    for device in (phone, laptop):
        device.post("/login", data={"email": "twodev@ssu.ac.kr", "password": PASSWORD})
        assert device.get("/me").status_code == 200
    phone.post("/me/password", data={"current_password": PASSWORD, "new_password": "brand-new-123", "new_password_confirm": "brand-new-123"})
    assert phone.get("/me").status_code == 200  # 바꾼 기기는 그대로
    assert laptop.get("/me").status_code == 302  # 다른 기기는 로그인 풀림


# --- 남용 방지 한도 ---------------------------------------------------------


def test_repeated_join_and_leave_does_not_flood_the_host(app, data, as_user):
    host, guest = data.user(), data.user()
    meeting = data.meeting(host)
    client = as_user(guest)
    for _ in range(5):
        client.post(f"/meetings/{meeting}/join")
        client.post(f"/meetings/{meeting}/leave")
    assert len(notes(app, host)) == 2  # 참여 1건, 취소 1건


def test_active_meetings_per_host_are_capped(data, as_user):
    host = data.user()
    for index in range(5):
        data.meeting(host, title=f"모임{index}", start_at=NOW_UTC + timedelta(hours=index + 1, minutes=1))
    form = edit_form(title="여섯 번째", time="20:00")
    response = as_user(host).post("/meetings/new", data=form)
    assert response.status_code == 400
    assert "5개까지" in response.get_data(as_text=True)
    assert data.count(Meeting) == 5


def test_comments_per_user_are_capped(data, as_user):
    host, guest = data.user(), data.user()
    meeting = data.meeting(host)
    client = as_user(guest)
    for index in range(22):
        client.post(f"/meetings/{meeting}/comments", data={"body": f"댓글 {index}"})
    assert data.count(Comment) == 20


def test_nul_byte_in_form_is_rejected(data, as_user):
    host = data.user()
    response = as_user(host).post("/meetings/new", data=edit_form(title="보드\x00게임"))
    assert response.status_code == 400
    assert data.count(Meeting) == 0


def test_signup_keeps_the_page_the_visitor_came_for(client, data):
    host = data.user()
    meeting = data.meeting(host)
    login_page = client.get(f"/meetings/{meeting}", follow_redirects=True).get_data(as_text=True)
    assert f"/signup?next=/meetings/{meeting}" in login_page.replace("%2F", "/")
    form = {"email": "new@ssu.ac.kr", "nickname": "신입", "password": "password-123", "password_confirm": "password-123", "agree": "1", "next": f"/meetings/{meeting}"}
    response = client.post("/signup", data=form)
    assert response.headers["Location"].endswith(f"/meetings/{meeting}")
    evil = client.application.test_client().post("/signup", data={**form, "email": "new2@ssu.ac.kr", "nickname": "신입2", "next": "https://evil.example"})
    assert evil.headers["Location"].endswith("/")


def test_home_wording_when_only_my_own_meeting_exists(data, as_user):
    host = data.user()
    data.meeting(host)
    page = as_user(host).get("/").get_data(as_text=True)
    assert "아직 열린 번개가 없어요" not in page
    assert "새로 참여할 수 있는 번개는 지금 없어요" in page


# --- 가입 동의, 이용 안내 ---------------------------------------------------


def test_signup_requires_consent(client, data):
    form = {"email": "new@ssu.ac.kr", "nickname": "신입", "password": "password-123", "password_confirm": "password-123"}
    assert client.post("/signup", data=form).status_code == 400
    assert data.count(User) == 0
    assert client.post("/signup", data={**form, "agree": "1"}).status_code == 302
    assert data.count(User) == 1


def test_about_page_is_public(client):
    response = client.get("/about")
    assert response.status_code == 200
    assert "저장하는 정보" in response.get_data(as_text=True)


# --- 2차 기능 점검에서 고친 동작 --------------------------------------------


def test_report_block_survives_reporters_leaving(app, data, as_user):
    host = data.user()
    members = [data.user() for _ in range(3)]
    meeting = data.meeting(host, capacity=6, members=members)
    for member in members:
        as_user(member).post(f"/meetings/{meeting}/report", data={"reason": "money"})
    as_user(members[0]).post(f"/meetings/{meeting}/leave")  # 신고하고 나가는 것이 가장 자연스러운 행동이다
    as_user(host).post(f"/meetings/{meeting}/status", data={"action": "reopen"})
    assert data.meeting_field(meeting, "status") == "blocked"
    page = as_user(host).get(f"/meetings/{meeting}").get_data(as_text=True)
    assert "모집이 중단된 모임" in page and "다시 열기" not in page


def test_reports_that_start_counting_later_block_on_the_next_visit(app, data, as_user):
    host, visitor = data.user(), data.user()
    meeting = data.meeting(host, start_at=NOW_UTC + timedelta(days=3))
    for _ in range(3):
        as_user(data.user()).post(f"/meetings/{meeting}/report", data={"reason": "spam"})
    assert data.meeting_field(meeting, "status") == "open"  # 방금 만든 계정의 신고는 아직 세지 않는다
    from bungae import timeutil

    timeutil.set_fixed_now(NOW_UTC + timedelta(hours=25))  # 하루가 지나 신고가 세어지기 시작
    response = as_user(visitor).post(f"/meetings/{meeting}/join")
    assert response.status_code == 302
    assert visitor not in data.member_ids(meeting)
    assert data.meeting_field(meeting, "status") == "blocked"
    assert any("신고가 여러 건" in message for message, _, _ in notes(app, host))


def test_rejoin_puts_the_latest_event_on_top(app, data, as_user):
    host, guest = data.user(), data.user("가람")
    meeting = data.meeting(host)
    client = as_user(guest)
    for path in ("join", "leave", "join"):
        client.post(f"/meetings/{meeting}/{path}")
    inbox = as_user(host).get("/notifications").get_data(as_text=True)
    assert inbox.index("참여했어요") < inbox.index("참여를 취소했어요")


def test_edit_notification_says_what_changed(app, data, as_user):
    host, member = data.user(), data.user()
    meeting = data.meeting(host, title="보드게임", members=[member])
    as_user(host).post(f"/meetings/{meeting}/edit", data=edit_form(title="보드게임", time="17:30", place="정보과학관"))
    message = notes(app, member)[0][0]
    assert "17:30" in message and "정보과학관" in message


def test_adding_a_chat_link_notifies_members(app, data, as_user):
    host, member = data.user(), data.user()
    meeting = data.meeting(host, members=[member])
    as_user(host).post(f"/meetings/{meeting}/edit", data=edit_form(contact_link="https://open.kakao.com/o/abc"))
    assert "오픈채팅 링크" in notes(app, member)[0][0]


def test_leaving_a_cancelled_meeting_is_refused(app, data, as_user):
    host, member = data.user(), data.user()
    meeting = data.meeting(host, members=[member], status="cancelled")
    as_user(member).post(f"/meetings/{meeting}/leave")
    assert member in data.member_ids(meeting)
    assert notes(app, host) == []


def test_huge_ids_are_404_not_500(data, as_user):
    client = as_user(data.user())
    assert client.get("/meetings/9223372036854775808").status_code == 404
    assert client.post("/meetings/99999999999999999999999/join").status_code == 404
    assert client.post("/comments/9223372036854775808/delete").status_code == 404


def test_error_flash_is_not_sent_below_the_fold(data, as_user):
    host = data.user()
    meeting = data.meeting(host)
    client = as_user(host)
    response = client.post(f"/meetings/{meeting}/comments", data={"body": ""})
    assert "#" not in response.headers["Location"]
    response = client.post("/me/password", data={"current_password": "wrong", "new_password": "x", "new_password_confirm": "x"})
    assert "#" not in response.headers["Location"]
    response = client.post(f"/meetings/{meeting}/comments", data={"body": "안녕하세요"})
    assert response.headers["Location"].endswith("#comments")


def test_limit_is_announced_before_filling_the_form(data, as_user):
    host = data.user()
    for index in range(5):
        data.meeting(host, title=f"모임{index}", start_at=NOW_UTC + timedelta(hours=index + 1, minutes=1))
    page = as_user(host).get("/meetings/new").get_data(as_text=True)
    assert "5개까지" in page and 'name="title"' not in page


def test_new_form_defaults_to_thirty_minutes_later_and_my_first_interest(data, as_user, rendered):
    user = data.user(interests="study,cafe")
    as_user(user).get("/meetings/new")
    values = rendered[-1][1]["values"]
    assert (values["date"], values["time"], values["category"]) == ("2026-11-12", "14:30", "study")


def test_prefill_ignores_free_text_and_malformed_values(data, as_user, rendered):
    user = data.user()
    as_user(user).get("/meetings/new?title=당첨&contact_link=https://evil.example&duration=45&time=14:30:15&category=game")
    values = rendered[-1][1]["values"]
    assert values["title"] == "" and values["contact_link"] == ""
    assert (values["duration"], values["category"]) == ("60", "game")
    assert values["time"] == "14:30"  # 형식이 틀린 시각은 버리고 기본값(30분 뒤)을 쓴다


def test_nickname_change_and_case_insensitive_uniqueness(app, data, as_user):
    user, other = data.user("Minsu"), data.user("지현")
    client = as_user(other)
    client.post("/me/nickname", data={"nickname": "minsu"})
    with app.app_context():
        assert db.session.get(User, other).nickname == "지현"
    client.post("/me/nickname", data={"nickname": "새이름"})
    with app.app_context():
        assert db.session.get(User, other).nickname == "새이름"


def test_same_password_is_refused(app, data, as_user):
    user = data.user()
    before = password_of(app, user)
    as_user(user).post("/me/password", data={"current_password": PASSWORD, "new_password": PASSWORD, "new_password_confirm": PASSWORD})
    assert password_of(app, user) == before


def test_home_shows_my_ongoing_meeting_and_honest_wording(data, as_user):
    host = data.user()
    data.meeting(host, title="진행중모임", start_at=NOW_UTC - timedelta(minutes=10), duration_min=60)
    page = as_user(host).get("/").get_data(as_text=True)
    assert "지금 진행 중 · 진행중모임" in page
    assert "첫 번개" not in page


def test_csrf_error_page_keeps_the_logged_in_frame(make_app, data):
    app = make_app(WTF_CSRF_ENABLED=True)
    from .conftest import Data, login_as

    user = Data(app).user("프레임확인")
    client = app.test_client()
    login_as(client, user)
    response = client.post("/meetings/1/join")
    assert response.status_code == 400
    assert "프레임확인" in response.get_data(as_text=True)


def test_logged_in_pages_are_not_cached(data, as_user, client):
    assert as_user(data.user()).get("/").headers.get("Cache-Control") == "no-store"
    assert client.get("/login").headers.get("Cache-Control") != "no-store"
