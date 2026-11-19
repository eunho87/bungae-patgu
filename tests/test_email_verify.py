"""이메일 인증과 비밀번호 재설정 (auth.py, emailcode.py, mailer.py)."""

import re
import smtplib
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, text
from werkzeug.security import check_password_hash

from bungae import create_app, db, emailcode, mailer, timeutil
from bungae.models import EmailCode, LoginThrottle, User

from .conftest import BASE_CONFIG, NOW_UTC, PASSWORD, login_as

EMAIL = "newbie@soongsil.ac.kr"
NEW_PASSWORD = "brand-new-pass-1"


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, body: sent.append({"to": to, "subject": subject, "body": body}))
    return sent


@pytest.fixture
def app(make_app):
    return make_app(EMAIL_VERIFICATION=True)


def code_in(mail):
    return re.search(r"\b(\d{6})\b", mail["body"]).group(1)


def signup(client, **over):
    form = {
        "email": EMAIL,
        "nickname": "새내기",
        "password": "password-123",
        "password_confirm": "password-123",
        "agree": "1",
    }
    form.update(over)
    return client.post("/signup", data=form)


def session_value(client, key):
    with client.session_transaction() as sess:
        return sess.get(key)


def user_by_email(app, email=EMAIL):
    with app.app_context():
        user = User.query.filter_by(email=email).first()
        return None if user is None else {"id": user.id, "verified": user.email_verified, "hash": user.password_hash}


def later(**delta):
    timeutil.set_fixed_now(NOW_UTC + timedelta(**delta))


# ---------------------------------------------------------------------------
# 가입 인증
# ---------------------------------------------------------------------------


def test_signup_sends_a_code_and_does_not_log_in(app, client, outbox):
    response = signup(client)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/verify")
    assert session_value(client, "user_id") is None
    assert user_by_email(app)["verified"] is False
    assert len(outbox) == 1
    assert outbox[0]["to"] == EMAIL
    assert re.fullmatch(r"\d{6}", code_in(outbox[0]))


def test_correct_code_finishes_signup(app, client, outbox):
    signup(client)
    response = client.post("/verify", data={"code": code_in(outbox[0])})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")
    user = user_by_email(app)
    assert user["verified"] is True
    assert session_value(client, "user_id") == user["id"]


def test_code_with_spaces_is_accepted(app, client, outbox):
    signup(client)
    code = code_in(outbox[0])
    response = client.post("/verify", data={"code": f" {code[:3]} {code[3:]} "})
    assert response.status_code == 302
    assert user_by_email(app)["verified"] is True


def test_signup_next_is_kept_through_verification(app, client, outbox):
    signup(client, next="/meetings/7")
    response = client.post("/verify", data={"code": code_in(outbox[0])})
    assert response.headers["Location"].endswith("/meetings/7")


def test_wrong_code_is_rejected(app, client, outbox):
    signup(client)
    wrong = "000000" if code_in(outbox[0]) != "000000" else "111111"
    response = client.post("/verify", data={"code": wrong})
    assert response.status_code == 400
    assert "코드가 맞지 않아요" in response.get_data(as_text=True)
    assert user_by_email(app)["verified"] is False
    assert session_value(client, "user_id") is None


def test_non_numeric_code_is_rejected_without_using_an_attempt(app, client, outbox):
    signup(client)
    assert client.post("/verify", data={"code": "abc"}).status_code == 400
    with app.app_context():
        assert EmailCode.query.one().attempts == 0


def test_five_wrong_codes_lock_the_code(app, client, outbox):
    signup(client)
    right = code_in(outbox[0])
    wrong = "000000" if right != "000000" else "111111"
    for _ in range(5):
        client.post("/verify", data={"code": wrong})
    response = client.post("/verify", data={"code": right})
    assert response.status_code == 400
    assert "여러 번 틀렸어요" in response.get_data(as_text=True)
    assert user_by_email(app)["verified"] is False


def test_code_expires_after_ten_minutes(app, client, outbox):
    signup(client)
    later(minutes=10, seconds=1)
    response = client.post("/verify", data={"code": code_in(outbox[0])})
    assert response.status_code == 400
    assert "유효 시간" in response.get_data(as_text=True)


def test_resend_waits_a_minute_and_replaces_the_old_code(app, client, outbox):
    signup(client)
    first = code_in(outbox[0])
    client.post("/verify/resend")
    assert len(outbox) == 1  # 1분이 지나지 않아 보내지 않았다
    later(seconds=61)
    client.post("/verify/resend")
    assert len(outbox) == 2
    second = code_in(outbox[1])
    if first != second:
        assert client.post("/verify", data={"code": first}).status_code == 400
    assert client.post("/verify", data={"code": second}).status_code == 302


def test_at_most_five_codes_per_hour(app, client, outbox):
    signup(client)
    for minute in range(1, 10):
        later(minutes=minute * 2)
        client.post("/verify/resend")
    assert len(outbox) == 5
    later(hours=1, minutes=1)
    client.post("/verify/resend")
    assert len(outbox) == 6


def test_code_is_not_stored_in_plain_text(app, client, outbox):
    signup(client)
    code = code_in(outbox[0])
    with app.app_context():
        row = EmailCode.query.one()
        assert code not in row.code_hash
        assert len(row.code_hash) == 64


def test_verify_page_without_pending_signup_goes_to_login(client):
    response = client.get("/verify")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_mail_failure_keeps_the_account_and_allows_resend(app, client, monkeypatch, outbox):
    def broken(to, subject, body):
        raise mailer.MailError("smtp down")

    monkeypatch.setattr(mailer, "send", broken)
    response = signup(client, nickname="메일실패")
    assert response.headers["Location"].endswith("/verify")
    page = client.get("/verify").get_data(as_text=True)
    assert "메일을 보내지 못했어요" in page
    with app.app_context():
        assert EmailCode.query.count() == 0  # 실패한 발송은 재시도 제한에 세지 않는다

    sent = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, body: sent.append(body))
    client.post("/verify/resend")
    assert len(sent) == 1


# ---------------------------------------------------------------------------
# 로그인과 미인증 계정
# ---------------------------------------------------------------------------


def test_unverified_login_goes_to_verification(app, client, outbox):
    signup(client)
    client.post("/logout")
    later(minutes=2)
    response = client.post("/login", data={"email": EMAIL, "password": "password-123"})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/verify")
    assert session_value(client, "user_id") is None
    assert len(outbox) == 2  # 로그인하면서 코드를 새로 보냈다
    assert client.post("/verify", data={"code": code_in(outbox[1])}).status_code == 302
    assert session_value(client, "user_id") == user_by_email(app)["id"]


def test_unverified_login_with_wrong_password_reveals_nothing(app, client, outbox):
    signup(client)
    client.post("/logout")
    response = client.post("/login", data={"email": EMAIL, "password": "wrong-password"})
    assert response.status_code == 400
    assert "이메일 또는 비밀번호가 맞지 않아요" in response.get_data(as_text=True)
    assert len(outbox) == 1


def test_existing_users_log_in_as_before(app, client, data, outbox):
    data.user(email="old@soongsil.ac.kr")
    response = client.post("/login", data={"email": "old@soongsil.ac.kr", "password": PASSWORD})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")
    assert outbox == []


def test_signing_up_again_replaces_an_unverified_account(app, client, outbox):
    signup(client, nickname="남의주소")
    client.post("/logout")
    response = signup(app.test_client(), nickname="진짜주인")
    assert response.headers["Location"].endswith("/verify")
    with app.app_context():
        users = User.query.filter_by(email=EMAIL).all()
        assert [u.nickname for u in users] == ["진짜주인"]


def test_unverified_nickname_is_held_for_an_hour(app, outbox):
    signup(app.test_client(), email="first@soongsil.ac.kr", nickname="번개러")
    response = signup(app.test_client(), email="second@soongsil.ac.kr", nickname="번개러")
    assert response.status_code == 400
    assert "이미 쓰이고 있는 닉네임" in response.get_data(as_text=True)

    later(hours=1, minutes=1)
    response = signup(app.test_client(), email="second@soongsil.ac.kr", nickname="번개러")
    assert response.status_code == 302
    assert user_by_email(app, "first@soongsil.ac.kr") is None


def test_verified_accounts_are_never_released(app, client, data, outbox):
    data.user(nickname="번개러", email=EMAIL)
    later(days=3)
    response = signup(client, nickname="다른이름")
    assert response.status_code == 400
    assert "이미 가입된 이메일" in response.get_data(as_text=True)


def test_account_deletion_removes_codes(app, as_user, data, outbox):
    user_id = data.user(email=EMAIL)
    with app.app_context():
        emailcode.send(EMAIL, "reset")
    client = as_user(user_id)
    client.post("/me/delete", data={"password": PASSWORD})
    with app.app_context():
        assert EmailCode.query.count() == 0


# ---------------------------------------------------------------------------
# 비밀번호 재설정
# ---------------------------------------------------------------------------


def test_password_reset_flow(app, client, data, outbox):
    user_id = data.user(email=EMAIL)
    other_device = app.test_client()
    other_device.post("/login", data={"email": EMAIL, "password": PASSWORD})
    with app.app_context():
        db.session.add(LoginThrottle(email=EMAIL, fail_count=3))
        db.session.commit()

    response = client.post("/password/forgot", data={"email": EMAIL})
    assert response.headers["Location"].endswith("/password/reset")
    assert len(outbox) == 1 and outbox[0]["to"] == EMAIL

    response = client.post(
        "/password/reset",
        data={"code": code_in(outbox[0]), "password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD},
    )
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
    with app.app_context():
        user = db.session.get(User, user_id)
        assert check_password_hash(user.password_hash, NEW_PASSWORD)
        assert LoginThrottle.query.count() == 0

    # 비밀번호가 바뀌면 다른 기기의 로그인이 풀린다.
    assert other_device.get("/").status_code == 302
    assert client.post("/login", data={"email": EMAIL, "password": NEW_PASSWORD}).status_code == 302


def test_forgot_does_not_reveal_whether_an_email_is_registered(app, client, data, outbox):
    data.user(email=EMAIL)
    known = client.post("/password/forgot", data={"email": EMAIL}, follow_redirects=True).get_data(as_text=True)
    unknown_client = app.test_client()
    unknown = unknown_client.post(
        "/password/forgot", data={"email": "nobody@soongsil.ac.kr"}, follow_redirects=True
    ).get_data(as_text=True)
    assert len(outbox) == 1
    message = "가입된 주소라면 재설정 코드를 보냈어요"
    assert message in known and message in unknown

    # 1분 안에 다시 요청해도(실제로는 보내지 않아도) 같은 문구다.
    again = client.post("/password/forgot", data={"email": EMAIL}, follow_redirects=True).get_data(as_text=True)
    assert message in again
    assert len(outbox) == 1


def test_reset_rejects_wrong_code_and_weak_password(app, client, data, outbox):
    data.user(email=EMAIL)
    client.post("/password/forgot", data={"email": EMAIL})
    right = code_in(outbox[0])
    wrong = "000000" if right != "000000" else "111111"
    response = client.post("/password/reset", data={"code": wrong, "password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD})
    assert response.status_code == 400
    assert "코드가 맞지 않아요" in response.get_data(as_text=True)
    response = client.post("/password/reset", data={"code": right, "password": "short", "password_confirm": "short"})
    assert response.status_code == 400
    assert "8자 이상" in response.get_data(as_text=True)
    with app.app_context():
        assert check_password_hash(User.query.one().password_hash, PASSWORD)


def test_reset_code_cannot_verify_a_signup_and_vice_versa(app, client, data, outbox):
    data.user(email="known@soongsil.ac.kr")
    signup(client)
    signup_code = code_in(outbox[0])
    with app.app_context():
        assert emailcode.check(EMAIL, "reset", signup_code) == "none"


def test_unverified_accounts_cannot_reset(app, client, outbox):
    signup(client)
    other = app.test_client()
    other.post("/password/forgot", data={"email": EMAIL})
    assert len(outbox) == 1  # 가입 인증 메일만 있다


def test_reset_page_without_email_goes_back(app, client):
    response = client.get("/password/reset")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/password/forgot")


def test_login_page_links_to_reset_only_when_enabled(app, make_app, client):
    assert "비밀번호를 잊었나요?" in client.get("/login").get_data(as_text=True)
    off = make_app()
    page = off.test_client().get("/login").get_data(as_text=True)
    assert "비밀번호를 잊었나요?" not in page
    assert off.test_client().get("/password/forgot").status_code == 404


def test_signup_without_verification_logs_in_immediately(make_app):
    off = make_app()
    client = off.test_client()
    response = signup(client)
    assert response.headers["Location"].endswith("/")
    assert session_value(client, "user_id") is not None
    assert user_by_email(off)["verified"] is True


# ---------------------------------------------------------------------------
# 설정과 DB
# ---------------------------------------------------------------------------


def test_switch_follows_mail_settings(monkeypatch):
    config = {key: value for key, value in BASE_CONFIG.items() if key not in ("EMAIL_VERIFICATION", "MAIL_USERNAME", "MAIL_PASSWORD")}
    cases = [
        ({"MAIL_USERNAME": "", "MAIL_PASSWORD": "", "EMAIL_VERIFICATION": ""}, False),
        ({"MAIL_USERNAME": "team@gmail.com", "MAIL_PASSWORD": "abcd efgh ijkl mnop", "EMAIL_VERIFICATION": ""}, True),
        ({"MAIL_USERNAME": "team@gmail.com", "MAIL_PASSWORD": "abcdefghijklmnop", "EMAIL_VERIFICATION": "0"}, False),
        ({"MAIL_USERNAME": "", "MAIL_PASSWORD": "", "EMAIL_VERIFICATION": "1"}, True),
    ]
    for env, expected in cases:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        app = create_app(config)
        assert app.config["EMAIL_VERIFICATION"] is expected
        if env["MAIL_PASSWORD"]:
            assert " " not in app.config["MAIL_PASSWORD"]
        with app.app_context():
            db.engine.dispose()


def test_existing_database_gets_the_new_column(tmp_path):
    path = tmp_path / "old.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR(120) UNIQUE NOT NULL, "
                "nickname VARCHAR(20) UNIQUE NOT NULL, password_hash VARCHAR(255) NOT NULL, "
                "interests VARCHAR(200) NOT NULL, created_at DATETIME NOT NULL)"
            )
        )
        connection.execute(
            text("INSERT INTO users VALUES (1, 'old@soongsil.ac.kr', 'old', 'x', '', '2026-11-01 00:00:00')")
        )
    engine.dispose()

    app = create_app({**BASE_CONFIG, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{path.as_posix()}"})
    with app.app_context():
        assert db.session.get(User, 1).email_verified is True
        assert db.inspect(db.engine).has_table("email_codes")
        db.session.remove()
        db.engine.dispose()


# ---------------------------------------------------------------------------
# 메일 발송
# ---------------------------------------------------------------------------


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout):
        self.calls = [("connect", host, port, timeout)]
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def starttls(self):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, message):
        self.calls.append(("send", message["To"], message["Subject"], message.get_content()))


def test_mailer_sends_through_smtp(make_app, monkeypatch):
    app = make_app(MAIL_USERNAME="team@gmail.com", MAIL_PASSWORD="abcdefghijklmnop")
    FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    with app.app_context():
        mailer.send("a@soongsil.ac.kr", "제목", "본문 123456")
    calls = FakeSMTP.instances[0].calls
    assert calls[0] == ("connect", "smtp.gmail.com", 587, 10)
    assert ("starttls",) in calls
    assert ("login", "team@gmail.com", "abcdefghijklmnop") in calls
    assert calls[-1][:3] == ("send", "a@soongsil.ac.kr", "제목")


def test_mailer_error_becomes_mail_error(make_app, monkeypatch):
    app = make_app(MAIL_USERNAME="team@gmail.com", MAIL_PASSWORD="wrong")

    class Refusing(FakeSMTP):
        def login(self, user, password):
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(smtplib, "SMTP", Refusing)
    with app.app_context(), pytest.raises(mailer.MailError):
        mailer.send("a@soongsil.ac.kr", "제목", "본문")


def test_mailer_without_settings_only_logs(make_app, caplog):
    app = make_app()
    with app.app_context():
        mailer.send("a@soongsil.ac.kr", "제목", "코드 654321")
    assert "654321" in caplog.text
