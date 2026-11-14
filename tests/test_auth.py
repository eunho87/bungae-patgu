"""가입·로그인·로그아웃·접근 제어 (auth.py)."""

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from werkzeug.security import check_password_hash

from bungae import db, timeutil
from bungae.models import LoginThrottle, User

from .conftest import NOW_UTC, PASSWORD, login_as

GENERIC_LOGIN_ERROR = "이메일 또는 비밀번호가 맞지 않아요."
LOCK_ERROR = "로그인 시도가 너무 많아요"


def signup(client, **over):
    form = {
        "email": "newbie@ssu.ac.kr",
        "nickname": "새내기",
        "password": "password-123",
        "password_confirm": "password-123",
        "agree": "1",
    }
    form.update(over)
    return client.post("/signup", data=form)


def login(client, email, password, next_url=None, **kwargs):
    form = {"email": email, "password": password}
    if next_url is not None:
        form["next"] = next_url
    return client.post("/login", data=form, **kwargs)


def session_user(client):
    with client.session_transaction() as sess:
        return sess.get("user_id")


def path_of(response):
    return urlparse(response.headers["Location"]).path


# ---------------------------------------------------------------------------
# 가입
# ---------------------------------------------------------------------------


def test_signup_success_logs_in(app, client):
    response = signup(client, interests=["game", "meal", "hacking"])
    assert response.status_code == 302
    assert path_of(response) == "/"
    with app.app_context():
        user = User.query.filter_by(email="newbie@ssu.ac.kr").one()
        assert session_user(client) == user.id
        assert user.nickname == "새내기"
        assert user.password_hash != "password-123"
        assert check_password_hash(user.password_hash, "password-123")
        assert user.interest_list == ["meal", "game"]  # 모르는 값은 버린다
    # 로그인된 상태라 보호된 API가 열린다.
    assert client.post("/api/chat/reset").status_code == 200


def test_signup_normalizes_email_case(app, client):
    assert signup(client, email="  NewBie@SSU.AC.KR ").status_code == 302
    with app.app_context():
        assert User.query.one().email == "newbie@ssu.ac.kr"


@pytest.mark.needs_template
@pytest.mark.parametrize(
    "email",
    [
        "newbie@gmail.com",
        "newbie@soongsil.ac.kr",
        "newbie@ssu.ac.kr.evil.com",
        "newbie@evil.com@ssu.ac.kr",
        "newbie@sub.ssu.ac.kr",
        "newbie@xssu.ac.kr",
        "newbie@ssuXacXkr",
        "@ssu.ac.kr",
        "new bie@ssu.ac.kr",
        "newbie@ssu.ac.kr\nx@evil.com",
        "newbie",
        "",
    ],
)
def test_signup_rejects_non_ssu_email(app, client, rendered, email):
    response = signup(client, email=email)
    assert response.status_code == 400
    assert "email" in rendered[-1][1]["errors"]
    assert session_user(client) is None
    with app.app_context():
        assert User.query.count() == 0


@pytest.mark.needs_template
@pytest.mark.parametrize("password", ["", "1234567", "a" * 65])
def test_signup_rejects_bad_password_length(app, client, rendered, password):
    response = signup(client, password=password, password_confirm=password)
    assert response.status_code == 400
    assert "password" in rendered[-1][1]["errors"]
    with app.app_context():
        assert User.query.count() == 0


def test_signup_accepts_boundary_password_lengths(app, client, make_app):
    assert signup(client, password="a" * 8, password_confirm="a" * 8).status_code == 302
    other = make_app().test_client()
    assert signup(other, password="a" * 64, password_confirm="a" * 64).status_code == 302


@pytest.mark.needs_template
def test_signup_rejects_mismatched_confirmation(app, client, rendered):
    response = signup(client, password_confirm="password-124")
    assert response.status_code == 400
    assert "password_confirm" in rendered[-1][1]["errors"]
    assert session_user(client) is None
    with app.app_context():
        assert User.query.count() == 0


@pytest.mark.needs_template
@pytest.mark.parametrize("nickname", ["", "a", "가" * 13, "nick name", "nick!", "<b>x</b>", "닉-네임"])
def test_signup_rejects_bad_nickname(app, client, rendered, nickname):
    response = signup(client, nickname=nickname)
    assert response.status_code == 400
    assert "nickname" in rendered[-1][1]["errors"]
    with app.app_context():
        assert User.query.count() == 0


@pytest.mark.needs_template
@pytest.mark.parametrize("email", ["taken@ssu.ac.kr", "TAKEN@ssu.ac.kr"])
def test_signup_rejects_duplicate_email(app, client, data, rendered, email):
    data.user("기존회원", email="taken@ssu.ac.kr")
    response = signup(client, email=email)
    assert response.status_code == 400
    assert "email" in rendered[-1][1]["errors"]
    assert session_user(client) is None
    with app.app_context():
        assert User.query.count() == 1


@pytest.mark.needs_template
def test_signup_rejects_duplicate_nickname(app, client, data, rendered):
    data.user("새내기", email="other@ssu.ac.kr")
    response = signup(client)
    assert response.status_code == 400
    assert "nickname" in rendered[-1][1]["errors"]
    assert session_user(client) is None
    with app.app_context():
        assert User.query.count() == 1


@pytest.mark.needs_template
def test_signup_error_page_does_not_echo_password(client):
    response = signup(client, password="s3cret-pass-XYZ", password_confirm="different-XYZ")
    assert "s3cret-pass-XYZ" not in response.get_data(as_text=True)
    assert "different-XYZ" not in response.get_data(as_text=True)


@pytest.mark.needs_template
def test_signup_and_login_pages_render(client):
    assert client.get("/signup").status_code == 200
    assert client.get("/login").status_code == 200


def test_logged_in_user_is_redirected_away_from_signup_and_login(client, data):
    login_as(client, data.user("me"))
    for path in ("/signup", "/login"):
        response = client.get(path)
        assert response.status_code == 302 and path_of(response) == "/"


# ---------------------------------------------------------------------------
# 로그인
# ---------------------------------------------------------------------------


def test_login_success(client, data):
    user_id = data.user("me")
    response = login(client, "me@ssu.ac.kr", PASSWORD)
    assert response.status_code == 302 and path_of(response) == "/"
    assert session_user(client) == user_id


def test_login_email_is_case_insensitive(client, data):
    user_id = data.user("me")
    assert login(client, " ME@SSU.ac.kr ", PASSWORD).status_code == 302
    assert session_user(client) == user_id


@pytest.mark.needs_template
def test_login_wrong_password_is_generic(client, data, rendered):
    data.user("me")
    wrong = login(client, "me@ssu.ac.kr", "wrong-password")
    unknown = login(client, "nobody@ssu.ac.kr", "wrong-password")
    assert wrong.status_code == unknown.status_code == 400
    assert session_user(client) is None
    assert GENERIC_LOGIN_ERROR in wrong.get_data(as_text=True)
    assert GENERIC_LOGIN_ERROR in unknown.get_data(as_text=True)
    # 계정이 있는지 없는지는 메시지로 구분되지 않는다.
    assert rendered[-2][1]["error"] == rendered[-1][1]["error"] == GENERIC_LOGIN_ERROR
    assert "wrong-password" not in wrong.get_data(as_text=True)


@pytest.mark.needs_template
@pytest.mark.parametrize("email, password", [("", ""), ("me@ssu.ac.kr", ""), ("", PASSWORD)])
def test_login_empty_fields(client, data, email, password):
    data.user("me")
    assert login(client, email, password).status_code == 400
    assert session_user(client) is None


@pytest.mark.needs_template
def test_five_failures_lock_the_account_for_five_minutes(app, client, data):
    user_id = data.user("me")
    for _ in range(5):
        response = login(client, "me@ssu.ac.kr", "wrong-password")
        assert response.status_code == 400
        assert GENERIC_LOGIN_ERROR in response.get_data(as_text=True)

    # 잠긴 동안에는 맞는 비밀번호도 거부된다.
    locked = login(client, "me@ssu.ac.kr", PASSWORD)
    assert locked.status_code == 400
    assert LOCK_ERROR in locked.get_data(as_text=True)
    assert session_user(client) is None

    timeutil.set_fixed_now(NOW_UTC + timedelta(minutes=4, seconds=59))
    assert login(client, "me@ssu.ac.kr", PASSWORD).status_code == 400
    assert session_user(client) is None

    timeutil.set_fixed_now(NOW_UTC + timedelta(minutes=5, seconds=1))
    unlocked = login(client, "me@ssu.ac.kr", PASSWORD)
    assert unlocked.status_code == 302
    assert session_user(client) == user_id
    with app.app_context():
        assert db.session.get(LoginThrottle, "me@ssu.ac.kr") is None


@pytest.mark.needs_template
def test_lock_applies_across_clients_and_is_per_email(app, data):
    data.user("me")
    other_id = data.user("other")
    attacker = app.test_client()
    for _ in range(5):
        login(attacker, "me@ssu.ac.kr", "wrong-password")
    victim = app.test_client()
    assert login(victim, "me@ssu.ac.kr", PASSWORD).status_code == 400
    bystander = app.test_client()
    assert login(bystander, "other@ssu.ac.kr", PASSWORD).status_code == 302
    assert session_user(bystander) == other_id


@pytest.mark.needs_template
def test_four_failures_do_not_lock_and_success_resets_the_count(client, data, app):
    data.user("me")
    for _ in range(4):
        login(client, "me@ssu.ac.kr", "wrong-password")
    assert login(client, "me@ssu.ac.kr", PASSWORD).status_code == 302
    client.post("/logout")
    for _ in range(4):
        login(client, "me@ssu.ac.kr", "wrong-password")
    # 연속 5회가 아니므로(4회 실패 -> 성공 -> 4회 실패) 잠기지 않는다.
    assert login(client, "me@ssu.ac.kr", PASSWORD).status_code == 302


@pytest.mark.needs_template
def test_failures_after_a_lock_expires_start_a_new_count(client, data):
    user_id = data.user("me")
    for _ in range(5):
        login(client, "me@ssu.ac.kr", "wrong-password")
    timeutil.set_fixed_now(NOW_UTC + timedelta(minutes=6))
    for _ in range(4):
        assert GENERIC_LOGIN_ERROR in login(client, "me@ssu.ac.kr", "wrong-password").get_data(as_text=True)
    assert login(client, "me@ssu.ac.kr", PASSWORD).status_code == 302
    assert session_user(client) == user_id


@pytest.mark.needs_template
def test_attempts_while_locked_do_not_extend_the_lock(client, data):
    data.user("me")
    for _ in range(5):
        login(client, "me@ssu.ac.kr", "wrong-password")
    timeutil.set_fixed_now(NOW_UTC + timedelta(minutes=4))
    for _ in range(3):
        login(client, "me@ssu.ac.kr", "wrong-password")
    timeutil.set_fixed_now(NOW_UTC + timedelta(minutes=5, seconds=1))
    assert login(client, "me@ssu.ac.kr", PASSWORD).status_code == 302


# ---------------------------------------------------------------------------
# next 매개변수
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["/meetings/1", "/me", "/meetings/new?category=game&time=15:00", "/chat"])
def test_next_accepts_local_paths(client, data, target):
    data.user("me")
    response = login(client, "me@ssu.ac.kr", PASSWORD, next_url=target)
    assert response.status_code == 302
    assert response.headers["Location"] == target


def test_next_is_read_from_the_query_string_too(client, data):
    data.user("me")
    response = client.post("/login?next=/meetings/1", data={"email": "me@ssu.ac.kr", "password": PASSWORD})
    assert response.headers["Location"] == "/meetings/1"


@pytest.mark.parametrize(
    "target",
    [
        "//evil.com",
        "//evil.com/meetings/1",
        "https://evil.com",
        "http://evil.com/",
        "HTTPS://evil.com",
        "javascript:alert(1)",
        "evil.com",
        "meetings/1",
        "///evil.com",
        "\\\\evil.com",
        "/\t/evil.com",
        " //evil.com",
        "",
    ],
)
def test_next_ignores_external_targets(client, data, target):
    data.user("me")
    response = login(client, "me@ssu.ac.kr", PASSWORD, next_url=target)
    assert response.status_code == 302
    assert response.headers["Location"] == "/"


@pytest.mark.parametrize("target", ["/\\evil.com", "/\\/evil.com", "/\\\\evil.com"])
def test_next_with_backslash_stays_on_this_site(client, data, target):
    """브라우저는 '/\\evil.com'을 '//evil.com'처럼 다룬다.

    _safe_next 는 이 값을 통과시키지만 Werkzeug가 Location 헤더에서 역슬래시를 %5C로 바꿔 주기 때문에
    실제로는 사이트 안에 머문다. 헤더에 역슬래시가 그대로 나가지 않는지만 확인한다.
    """
    data.user("me")
    location = login(client, "me@ssu.ac.kr", PASSWORD, next_url=target).headers["Location"]
    assert location.startswith("/") and not location.startswith("//")
    assert "\\" not in location
    assert urlparse(location).netloc == ""


# ---------------------------------------------------------------------------
# 접근 제어
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/me", "/chat", "/meetings/new", "/meetings/1", "/meetings/1/edit"])
def test_anonymous_pages_redirect_to_login(client, path):
    response = client.get(path)
    assert response.status_code == 302
    location = urlparse(response.headers["Location"])
    assert location.path == "/login"
    assert parse_qs(location.query)["next"] == [path]


def test_anonymous_redirect_keeps_the_query_string(client):
    response = client.get("/meetings/new?category=game&time=15:00")
    location = urlparse(response.headers["Location"])
    assert parse_qs(location.query)["next"] == ["/meetings/new?category=game&time=15:00"]


@pytest.mark.parametrize(
    "path",
    [
        "/meetings/new",
        "/meetings/1/join",
        "/meetings/1/leave",
        "/meetings/1/edit",
        "/meetings/1/status",
        "/meetings/1/comments",
        "/comments/1/delete",
        "/me/interests",
    ],
)
def test_anonymous_posts_redirect_to_login(client, data, path):
    host = data.user("host")
    meeting = data.meeting(host)
    data.comment(meeting, host)
    response = client.post(path, data={"action": "cancel", "body": "hi"})
    assert response.status_code == 302
    assert path_of(response) == "/login"
    assert data.meeting_field(meeting, "status") == "open"
    assert data.member_ids(meeting) == [host]


@pytest.mark.parametrize("path", ["/api/chat", "/api/chat/reset"])
def test_anonymous_api_is_401_json(client, path):
    response = client.post(path, json={"message": "보드게임"})
    assert response.status_code == 401
    assert response.is_json
    assert response.get_json()["error"]


def test_session_of_deleted_user_is_anonymous(app, client, data):
    user_id = data.user("ghost")
    login_as(client, user_id)
    with app.app_context():
        db.session.delete(db.session.get(User, user_id))
        db.session.commit()
    response = client.get("/")
    assert response.status_code == 302 and path_of(response) == "/login"
    assert session_user(client) is None


# ---------------------------------------------------------------------------
# 로그아웃
# ---------------------------------------------------------------------------


def test_logout_is_post_only(client, data):
    user_id = data.user("me")
    login_as(client, user_id)
    assert client.get("/logout").status_code == 405
    assert session_user(client) == user_id  # GET으로는 로그아웃되지 않는다

    response = client.post("/logout")
    assert response.status_code == 302 and path_of(response) == "/login"
    assert session_user(client) is None
    assert path_of(client.get("/")) == "/login"


def test_login_replaces_previous_session_contents(client, data):
    first = data.user("first")
    second = data.user("second")
    login_as(client, first)
    with client.session_transaction() as sess:
        sess["chat_slots"] = {"categories": ["game"]}
    client.post("/logout")
    login(client, "second@ssu.ac.kr", PASSWORD)
    with client.session_transaction() as sess:
        assert sess["user_id"] == second
        assert "chat_slots" not in sess


def test_session_cookie_flags(client, data):
    data.user("me")
    response = login(client, "me@ssu.ac.kr", PASSWORD)
    cookie = response.headers["Set-Cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie
