"""시연용 예시 데이터(bungae/demo.py)와 SEED_DEMO 스위치."""

from datetime import timedelta

from sqlalchemy.exc import OperationalError
from werkzeug.security import check_password_hash

from bungae import db, demo, timeutil
from bungae.models import Comment, Meeting, Notification, Participation, User

from .conftest import NOW_UTC, login_as


def test_first_run_creates_accounts_and_meetings(app):
    with app.app_context():
        made = demo.auto_seed()
        assert made == len(demo.plan(timeutil.now_kst())) == 11
        assert User.query.count() == 5
        assert Meeting.query.count() == 11
        assert Comment.query.count() == 2
        for meeting in Meeting.query.all():
            # 연 사람은 항상 참여자에 들어 있고, 정원을 넘기지 않는다.
            assert meeting.has_participant(meeting.host)
            assert meeting.joined_count <= meeting.capacity
            assert meeting.start_at > NOW_UTC
            assert meeting.joinable


def test_second_run_adds_nothing(app):
    with app.app_context():
        demo.auto_seed()
        assert demo.auto_seed() == 0
        assert User.query.count() == 5
        assert Meeting.query.count() == 11


def test_refills_when_meetings_have_passed(app):
    with app.app_context():
        demo.auto_seed()
        timeutil.set_fixed_now(NOW_UTC + timedelta(days=4))
        assert demo.upcoming_count() == 0
        assert demo.auto_seed() == 11
        # 지난 모임은 지우지 않는다. 계정은 다시 만들지 않는다.
        assert Meeting.query.count() == 22
        assert User.query.count() == 5
        assert demo.upcoming_count() == 11


def test_keeps_enough_upcoming_without_refill(app):
    with app.app_context():
        demo.auto_seed()
        # 하루 뒤에는 '내일 점심' 이후 모임이 아직 남아 있어 새로 만들지 않는다.
        timeutil.set_fixed_now(NOW_UTC + timedelta(hours=20))
        remaining = demo.upcoming_count()
        assert remaining >= demo.MIN_UPCOMING
        assert demo.auto_seed() == 0


def test_cancelled_meetings_do_not_count(app):
    with app.app_context():
        demo.auto_seed()
        for meeting in Meeting.query.all():
            meeting.status = "cancelled"
        db.session.commit()
        assert demo.auto_seed() == 11


def test_auto_seed_accounts_cannot_log_in(app, client):
    with app.app_context():
        demo.auto_seed()
        user = User.query.filter_by(email="demo1@soongsil.ac.kr").one()
        for guess in ("", "demo", "password", "demo1@soongsil.ac.kr", "번개장인"):
            assert not check_password_hash(user.password_hash, guess)
    response = client.post("/login", data={"email": "demo1@soongsil.ac.kr", "password": "demo"})
    assert response.status_code == 400


def test_existing_nickname_gets_a_suffix(app, data):
    data.user(nickname="번개장인", email="real@soongsil.ac.kr")
    with app.app_context():
        assert demo.auto_seed() == 11
        assert User.query.filter_by(email="real@soongsil.ac.kr").one().nickname == "번개장인"
        assert User.query.filter_by(email="demo1@soongsil.ac.kr").one().nickname == "번개장인1"


def test_accounts_from_the_old_domain_are_reused(app):
    """가입 주소를 바꾸기 전에 만들어진 데모 계정과 모임이 있는 DB(배포 DB)에서 두 벌이 생기지 않아야 한다."""
    with app.app_context():
        demo.auto_seed()
        for user in User.query.all():
            user.email = user.email.replace("@soongsil.ac.kr", demo.LEGACY_DOMAIN)
        db.session.commit()
        ids = sorted(user.id for user in User.query.all())

        assert demo.upcoming_count() == 11
        assert demo.auto_seed() == 0
        assert Meeting.query.count() == 11

        timeutil.set_fixed_now(NOW_UTC + timedelta(days=4))
        assert demo.auto_seed() == 11
        assert sorted(user.id for user in User.query.all()) == ids
        assert all(user.email.endswith("@soongsil.ac.kr") for user in User.query.all())


def test_real_users_data_is_untouched(app, data):
    host = data.user()
    meeting_id = data.meeting(host)
    with app.app_context():
        demo.auto_seed()
        timeutil.set_fixed_now(NOW_UTC + timedelta(days=4))
        demo.auto_seed()
        assert db.session.get(Meeting, meeting_id).host_id == host
        assert Participation.query.filter_by(meeting_id=meeting_id).count() == 1


def test_ensure_users_with_password_allows_login(app):
    with app.app_context():
        users, created = demo.ensure_users("demo-password-1")
        db.session.commit()
        assert created == 5
        assert check_password_hash(users[0].password_hash, "demo-password-1")
        assert demo.ensure_users("other")[1] == 0


def test_clear_meetings_keeps_notifications(app, data):
    with app.app_context():
        users, _ = demo.ensure_users()
        created = demo.create_meetings(users)
        db.session.commit()
        target = created[0].id
    reader = data.user()
    with app.app_context():
        db.session.add(Notification(user_id=reader, meeting_id=target, message="시간이 바뀌었어요"))
        db.session.commit()
        users, _ = demo.ensure_users()
        assert demo.clear_meetings(users) == 11
        db.session.commit()
        assert Meeting.query.count() == 0
        assert Notification.query.one().meeting_id is None


def test_switch_off_by_default(app):
    with app.app_context():
        assert User.query.count() == 0


def test_switch_on_seeds_at_startup(make_app):
    app = make_app(SEED_DEMO=True)
    with app.app_context():
        assert Meeting.query.count() == 11
    client = app.test_client()
    with app.app_context():
        viewer = User(email="viewer@soongsil.ac.kr", nickname="viewer", password_hash="x")
        db.session.add(viewer)
        db.session.commit()
        viewer_id = viewer.id
    login_as(client, viewer_id)
    body = client.get("/").get_data(as_text=True)
    assert "보드게임" in body


def test_seed_failure_does_not_stop_the_app(make_app, monkeypatch):
    def broken():
        raise OperationalError("SELECT 1", {}, Exception("DB가 깨어나는 중"))

    monkeypatch.setattr(demo, "auto_seed", broken)
    app = make_app(SEED_DEMO=True)
    assert app.test_client().get("/login").status_code == 200
