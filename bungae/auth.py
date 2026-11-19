import re
from datetime import timedelta
from functools import wraps
from urllib.parse import urlparse

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from . import db, emailcode, mailer
from .constants import CATEGORY_KEYS
from .models import LoginThrottle, User
from .timeutil import now_utc

bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[a-z0-9._%+-]{1,64}@soongsil\.ac\.kr$")
NICKNAME_RE = re.compile(r"^[0-9A-Za-z가-힣_]{2,12}$")
NICKNAME_RULE = "닉네임은 한글·영문·숫자·밑줄(_) 2~12자로 정해 주세요."
MAX_LOGIN_FAILS = 5
LOCK_MINUTES = 5
CODE_RE = re.compile(r"^\d{6}$")
# 인증하지 않은 계정이 닉네임을 붙잡아 두는 시간. 이보다 오래된 미인증 계정의 닉네임은 다른 사람이 가져갈 수 있다.
UNVERIFIED_HOLD = timedelta(hours=1)
CODE_ERRORS = {
    "wrong": "코드가 맞지 않아요. 메일에 적힌 6자리 숫자를 다시 확인해 주세요.",
    "locked": "코드를 여러 번 틀렸어요. 아래에서 코드를 새로 받아 주세요.",
    "expired": "코드의 유효 시간(10분)이 지났어요. 아래에서 코드를 새로 받아 주세요.",
    "none": "받은 코드가 없거나 이미 쓴 코드예요. 아래에서 코드를 새로 받아 주세요.",
}

# 없는 계정으로 로그인할 때도 해시 계산 시간을 똑같이 써서 계정 존재 여부가 응답 시간으로 새지 않게 한다.
_DUMMY_HASH = generate_password_hash("not-a-real-password")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            if request.path.startswith("/api/"):
                return {"error": "로그인이 필요해요."}, 401
            # POST 전용 주소로는 로그인 뒤에 돌아갈 수 없으므로(405) 그때는 홈으로 보낸다.
            target = request.full_path.rstrip("?") if request.method == "GET" else None
            return redirect(url_for("auth.login", next=target))
        return view(*args, **kwargs)

    return wrapped


def start_session(user):
    """로그인 상태를 만든다. 기존 세션 값은 버려 세션 고정 공격을 막는다."""
    session.clear()
    session["user_id"] = user.id
    session["pw"] = user.password_hash[-8:]
    session.permanent = True


def _safe_next(target):
    """로그인 뒤 돌아갈 주소는 사이트 내부 경로만 허용한다."""
    if not target or len(target) > 200:
        return None
    # 줄바꿈 같은 제어 문자는 응답 헤더에 넣을 수 없다.
    if any(ord(char) < 32 or ord(char) == 127 for char in target):
        return None
    parsed = urlparse(target)
    # 역슬래시는 일부 브라우저가 슬래시로 읽어 "/\evil.com"이 바깥 주소가 될 수 있다.
    if parsed.scheme or parsed.netloc or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return None
    return target


def nickname_taken(nickname, except_user_id=None):
    """대소문자만 다른 닉네임("Minsu"와 "minsu")도 같은 것으로 본다. 화면에서 구별하기 어려워 사칭에 쓰일 수 있다."""
    query = User.query.filter(func.lower(User.nickname) == nickname.lower())
    if except_user_id is not None:
        query = query.filter(User.id != except_user_id)
    return query.first() is not None


def _clean_interests(values):
    return ",".join(key for key in CATEGORY_KEYS if key in values)


def verification_on():
    return bool(current_app.config.get("EMAIL_VERIFICATION"))


def password_error(password, confirm):
    """(오류가 난 칸 이름, 문구) 또는 None."""
    if not 8 <= len(password) <= 64:
        return "password", "비밀번호는 8자 이상 64자 이하로 정해 주세요."
    if not password.strip():
        return "password", "공백만으로 된 비밀번호는 쓸 수 없어요."
    if password != confirm:
        return "password_confirm", "비밀번호가 서로 달라요."
    return None


def _clean_code(raw):
    return "".join((raw or "").split())[:12]


def _release_unverified(email, nickname):
    """인증을 마치지 않은 계정이 이메일·닉네임을 붙잡고 있으면 풀어 준다.

    남의 학교 메일 주소로 가입만 해 두고 인증하지 않으면 진짜 주인이 가입하지 못하게 된다. 인증하지 않은 계정은
    로그인할 수 없어 남긴 기록도 없으므로, 같은 이메일로 다시 가입하면 앞의 것을 지운다. 닉네임은 방금 가입하고
    인증 메일을 기다리는 사람의 것일 수 있어 1시간이 지난 뒤에만 풀어 준다.
    """
    stale = User.query.filter(User.email_verified.is_(False)).filter(
        (User.email == email)
        | ((func.lower(User.nickname) == nickname.lower()) & (User.created_at < now_utc() - UNVERIFIED_HOLD))
    )
    for user in stale.all():
        emailcode.forget(user.email)
        db.session.delete(user)
    db.session.flush()


def _begin_verification(user, next_url):
    """미인증 계정으로 코드 입력 화면에 들어가는 상태를 만든다. 로그인 상태는 만들지 않는다."""
    session.clear()
    session["verify_uid"] = user.id
    if next_url:
        session["verify_next"] = next_url


def _send_code(email, purpose):
    """코드를 보낸다. 보냈으면 True. 실패 사유는 화면 위 문구로 알린다."""
    wait = emailcode.wait_seconds(email, purpose)
    if wait:
        flash(f"코드는 잠시 뒤에 다시 받을 수 있어요. {wait}초 뒤에 시도해 주세요.", "error")
        return False
    try:
        emailcode.send(email, purpose)
    except mailer.MailError:
        flash("메일을 보내지 못했어요. 잠시 뒤 '코드 다시 받기'를 눌러 주세요.", "error")
        return False
    return True


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if g.user:
        return redirect(url_for("discover.home"))
    errors = {}
    form = {"email": "", "nickname": "", "interests": [], "agree": False}
    # 공유 링크로 들어온 사람이 가입을 마치면 보려던 모임으로 바로 돌아가게 한다.
    next_url = _safe_next(request.values.get("next"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        nickname = request.form.get("nickname", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("password_confirm", "")
        interests = request.form.getlist("interests")
        agree = request.form.get("agree") == "1"
        form = {"email": email, "nickname": nickname, "interests": interests, "agree": agree}

        if not agree:
            errors["agree"] = "이용 안내를 확인하고 동의해 주세요."
        if not EMAIL_RE.match(email):
            errors["email"] = "숭실대학교 이메일(@soongsil.ac.kr)만 가입할 수 있어요."
        if not NICKNAME_RE.match(nickname):
            errors["nickname"] = NICKNAME_RULE
        problem = password_error(password, confirm)
        if problem:
            errors[problem[0]] = problem[1]

        if not errors:
            if verification_on():
                _release_unverified(email, nickname)
            if User.query.filter_by(email=email).first():
                errors["email"] = "이미 가입된 이메일이에요."
            elif nickname_taken(nickname):
                errors["nickname"] = "이미 쓰이고 있는 닉네임이에요."

        if not errors:
            user = User(
                email=email,
                nickname=nickname,
                password_hash=generate_password_hash(password),
                interests=_clean_interests(interests),
                email_verified=not verification_on(),
            )
            db.session.add(user)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                errors["email"] = "이미 가입된 이메일이거나 닉네임이에요."
            else:
                if not user.email_verified:
                    _begin_verification(user, next_url)
                    if _send_code(email, "verify"):
                        flash(f"{email}(으)로 인증 코드를 보냈어요. 메일함을 확인해 주세요.", "success")
                    return redirect(url_for("auth.verify"))
                start_session(user)
                flash("가입을 환영해요! 지금 열려 있는 번개를 둘러보세요.", "success")
                return redirect(next_url or url_for("discover.home"))

    return (
        render_template("signup.html", errors=errors, form=form, next_url=next_url),
        400 if errors else 200,
    )


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("discover.home"))
    error = None
    email = ""
    next_url = _safe_next(request.values.get("next"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()[:120]
        password = request.form.get("password", "")
        now = now_utc()
        throttle = db.session.get(LoginThrottle, email) if email else None

        if throttle and throttle.locked_until and throttle.locked_until > now:
            error = f"로그인 시도가 너무 많아요. {LOCK_MINUTES}분 뒤에 다시 시도해 주세요."
        else:
            user = User.query.filter_by(email=email).first()
            ok = check_password_hash(user.password_hash if user else _DUMMY_HASH, password)
            if user and ok:
                if throttle:
                    db.session.delete(throttle)
                    db.session.commit()
                if verification_on() and not user.email_verified:
                    # 비밀번호까지 맞힌 사람에게만 알려 주므로, 남의 이메일로 가입 여부를 알아내는 데 쓸 수 없다.
                    _begin_verification(user, next_url)
                    flash("이메일 인증을 마쳐야 로그인할 수 있어요.", "info")
                    if emailcode.wait_seconds(user.email, "verify") == 0 and _send_code(user.email, "verify"):
                        flash(f"{user.email}(으)로 인증 코드를 새로 보냈어요.", "success")
                    return redirect(url_for("auth.verify"))
                start_session(user)
                return redirect(next_url or url_for("discover.home"))

            error = "이메일 또는 비밀번호가 맞지 않아요."
            if email:
                if throttle is None:
                    throttle = LoginThrottle(email=email, fail_count=0)
                    db.session.add(throttle)
                if throttle.locked_until and throttle.locked_until <= now:
                    throttle.fail_count = 0
                    throttle.locked_until = None
                throttle.fail_count += 1
                if throttle.fail_count >= MAX_LOGIN_FAILS:
                    throttle.locked_until = now + timedelta(minutes=LOCK_MINUTES)
                    throttle.fail_count = 0
                try:
                    db.session.commit()
                except IntegrityError:
                    # 같은 이메일로 실패 요청이 동시에 와서 기록 행을 둘이 만들려 한 경우
                    db.session.rollback()

    return render_template("login.html", error=error, email=email, next_url=next_url), (400 if error else 200)


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


def _pending_user():
    user_id = session.get("verify_uid")
    user = db.session.get(User, user_id) if isinstance(user_id, int) else None
    return user if user is not None and not user.email_verified else None


@bp.route("/verify", methods=["GET", "POST"])
def verify():
    """가입 인증 코드 입력."""
    if g.user:
        return redirect(url_for("discover.home"))
    user = _pending_user()
    if user is None:
        session.pop("verify_uid", None)
        return redirect(url_for("auth.login"))

    error = None
    if request.method == "POST":
        code = _clean_code(request.form.get("code"))
        if not CODE_RE.match(code):
            error = "메일로 받은 6자리 숫자를 입력해 주세요."
        else:
            result = emailcode.check(user.email, "verify", code)
            if result == "ok":
                user.email_verified = True
                db.session.commit()
                next_url = _safe_next(session.get("verify_next"))
                start_session(user)
                flash("인증이 끝났어요. 가입을 환영해요! 지금 열려 있는 번개를 둘러보세요.", "success")
                return redirect(next_url or url_for("discover.home"))
            db.session.commit()
            error = CODE_ERRORS[result]
    return render_template("verify.html", email=user.email, error=error), (400 if error else 200)


@bp.post("/verify/resend")
def verify_resend():
    user = _pending_user()
    if user is None:
        return redirect(url_for("auth.login"))
    if _send_code(user.email, "verify"):
        flash("인증 코드를 새로 보냈어요. 앞서 받은 코드는 더 이상 쓸 수 없어요.", "success")
    return redirect(url_for("auth.verify"))


@bp.route("/password/forgot", methods=["GET", "POST"])
def password_forgot():
    """비밀번호 재설정 1단계: 가입한 이메일로 코드 받기."""
    if not verification_on():
        abort(404)
    if g.user:
        return redirect(url_for("meetings.me"))
    error = None
    email = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()[:120]
        if not EMAIL_RE.match(email):
            error = "가입할 때 쓴 숭실대 이메일(@soongsil.ac.kr)을 입력해 주세요."
        else:
            session["reset_email"] = email
            if _send_reset_quietly(email):
                flash("가입된 주소라면 재설정 코드를 보냈어요. 메일함을 확인해 주세요.", "success")
            return redirect(url_for("auth.password_reset"))
    return render_template("password_forgot.html", error=error, email=email), (400 if error else 200)


@bp.route("/password/reset", methods=["GET", "POST"])
def password_reset():
    """비밀번호 재설정 2단계: 코드와 새 비밀번호 입력."""
    if not verification_on():
        abort(404)
    if g.user:
        return redirect(url_for("meetings.me"))
    email = session.get("reset_email")
    if not isinstance(email, str) or not EMAIL_RE.match(email):
        return redirect(url_for("auth.password_forgot"))

    errors = {}
    if request.method == "POST":
        code = _clean_code(request.form.get("code"))
        password = request.form.get("password", "")
        confirm = request.form.get("password_confirm", "")
        problem = password_error(password, confirm)
        if not CODE_RE.match(code):
            errors["code"] = "메일로 받은 6자리 숫자를 입력해 주세요."
        elif problem:
            errors[problem[0]] = problem[1]
        else:
            user = User.query.filter_by(email=email, email_verified=True).first()
            result = emailcode.check(email, "reset", code) if user else "none"
            if result == "ok":
                user.password_hash = generate_password_hash(password)
                LoginThrottle.query.filter_by(email=email).delete(synchronize_session=False)
                db.session.commit()
                session.pop("reset_email", None)
                # 비밀번호가 바뀌면 다른 기기에 남아 있던 로그인도 다음 요청에서 풀린다(세션의 비밀번호 표식이 달라진다).
                flash("비밀번호를 바꿨어요. 새 비밀번호로 로그인해 주세요.", "success")
                return redirect(url_for("auth.login"))
            db.session.commit()
            errors["code"] = CODE_ERRORS[result]
    return render_template("password_reset.html", email=email, errors=errors), (400 if errors else 200)


@bp.post("/password/resend")
def password_resend():
    if not verification_on():
        abort(404)
    email = session.get("reset_email")
    if not isinstance(email, str) or not EMAIL_RE.match(email):
        return redirect(url_for("auth.password_forgot"))
    if _send_reset_quietly(email):
        flash("가입된 주소라면 재설정 코드를 새로 보냈어요. 1분에 한 번까지 받을 수 있어요.", "success")
    return redirect(url_for("auth.password_reset"))


def _send_reset_quietly(email):
    """가입된 주소일 때만 재설정 코드를 보낸다.

    가입되지 않은 주소이거나 아직 다시 보낼 때가 안 됐어도 '보냈다'와 같은 화면을 보여 준다. 답이 다르면
    남의 이메일로 가입 여부를 알아내는 데 쓰인다. 메일 서버가 실패했을 때만 False(오류 문구는 이미 띄움).
    """
    user = User.query.filter_by(email=email, email_verified=True).first()
    if user is None or emailcode.wait_seconds(email, "reset"):
        return True
    try:
        emailcode.send(email, "reset")
    except mailer.MailError:
        flash("메일을 보내지 못했어요. 잠시 뒤 다시 시도해 주세요.", "error")
        return False
    return True
