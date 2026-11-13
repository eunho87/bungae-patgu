import hashlib
import hmac
import os
import secrets
from datetime import timedelta
from pathlib import Path

from flask import Flask, abort, flash, g, render_template, request, session
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFError, CSRFProtect
from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool
from werkzeug.middleware.proxy_fix import ProxyFix

db = SQLAlchemy()
csrf = CSRFProtect()

BASE_DIR = Path(__file__).resolve().parent.parent


def _database_url(on_vercel):
    """(접속 주소, 임시 저장소 여부)를 돌려준다."""
    url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if url:
        # 드라이버를 명시한다. 주소에 드라이버가 없으면 SQLAlchemy 2.1은 설치하지 않은 psycopg(v3)를 찾다가 죽는다.
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                url = "postgresql+psycopg2://" + url[len(prefix):]
                break
        return url, False
    if on_vercel:
        # Vercel 함수는 /tmp 말고는 쓸 수 없고 그마저 인스턴스마다 따로라 데이터가 유지되지 않는다.
        return "sqlite:////tmp/bungae.db", True
    instance = BASE_DIR / "instance"
    instance.mkdir(exist_ok=True)
    return "sqlite:///" + (instance / "bungae.db").as_posix(), False


def _secret_key(on_vercel):
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    if on_vercel:
        # SECRET_KEY를 따로 넣지 않았으면 DB 접속 주소에서 만든다. 접속 주소에는 DB 서비스가 만든 긴 무작위
        # 비밀번호가 들어 있어 추측할 수 없고, 모든 인스턴스가 같은 값을 얻으므로 로그인 쿠키가 서로 통한다.
        # (HMAC이라 이 키에서 접속 주소를 거꾸로 알아낼 수는 없다.)
        database_url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
        if database_url:
            return hmac.new(database_url.encode(), b"bungae-patgu session key", hashlib.sha256).hexdigest()
        # DB도 연결하지 않은 임시 실행: 인스턴스마다 다른 키라 로그인이 자주 풀린다(화면에 경고가 함께 뜬다).
        return secrets.token_hex(32)
    path = BASE_DIR / "instance" / "secret_key"
    path.parent.mkdir(exist_ok=True)
    if not path.exists():
        path.write_text(secrets.token_hex(32), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip()


def _ensure_tables(app, on_vercel):
    """테이블이 없으면 만든다.

    Vercel에서는 인스턴스가 새로 뜰 때마다 이 코드가 돈다. 매번 테이블 전체를 검사하면 느리고,
    DB가 잠에서 깨는 중이면 인스턴스가 통째로 죽으므로 가장 나중에 추가한 테이블 하나만 확인한다.
    """
    if not on_vercel:
        db.create_all()
        return
    try:
        if not inspect(db.engine).has_table("reports"):
            db.create_all()
    except SQLAlchemyError:
        app.logger.exception("테이블 확인 실패: DB 연결 설정을 확인하세요")


def _seed_demo(app):
    """예시 모임이 모자라면 채운다. 실패해도 서비스는 떠야 하므로 오류는 기록만 한다."""
    from .demo import auto_seed

    try:
        made = auto_seed()
        if made:
            app.logger.warning("예시 모임 %d개를 새로 만들었습니다(SEED_DEMO)", made)
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("예시 데이터 생성 실패")


def create_app(test_config=None):
    try:
        from dotenv import load_dotenv

        load_dotenv(BASE_DIR / ".env")
    except ImportError:
        pass

    # Vercel은 public/ 아래 파일을 CDN으로 직접 내보낸다. 로컬에서는 Flask가 같은 경로로 서빙한다.
    app = Flask(
        __name__,
        static_folder=str(BASE_DIR / "public" / "assets"),
        static_url_path="/assets",
    )
    on_vercel = bool(os.environ.get("VERCEL"))
    if on_vercel:
        # Vercel은 프록시 뒤에서 앱을 실행한다. 원래 요청의 https와 도메인을 알아야 공유 링크가 올바르게 만들어진다.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=on_vercel,
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
        MAX_CONTENT_LENGTH=64 * 1024,
        # CSRF 토큰을 로그인 세션과 같은 수명으로 둔다. 기본값(1시간)이면 챗봇 화면을 오래 열어 둔 사용자의 요청이 거부된다.
        WTF_CSRF_TIME_LIMIT=None,
        OPENROUTER_API_KEY=os.environ.get("OPENROUTER_API_KEY", "").strip(),
        OPENROUTER_MODEL=os.environ.get("OPENROUTER_MODEL", "").strip() or "google/gemma-4-26b-a4b-it:free",
        # 기본 모델이 막혔을 때 차례로 시도할 무료 모델(쉼표로 구분). 비우려면 "none"을 넣는다.
        OPENROUTER_FALLBACK_MODELS=os.environ.get("OPENROUTER_FALLBACK_MODELS", "").strip()
        or "recursive/flint-v1.0:free,baidu/ernie-5.5-flash-preview:free",
        CHAT_AI_DAILY_LIMIT=int(os.environ.get("CHAT_AI_DAILY_LIMIT") or 15),
        EPHEMERAL_DB=False,
        SEED_DEMO=False,
    )
    if test_config:
        app.config.update(test_config)
    else:
        # 시연용 예시 모임을 서버가 스스로 채우게 하는 스위치. 비밀 값이 아니라 켜고 끄는 값이다.
        app.config["SEED_DEMO"] = os.environ.get("SEED_DEMO", "").strip() == "1"
        app.config["SECRET_KEY"] = _secret_key(on_vercel)
        uri, ephemeral = _database_url(on_vercel)
        app.config["SQLALCHEMY_DATABASE_URI"] = uri
        app.config["EPHEMERAL_DB"] = ephemeral

    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgresql"):
        # 서버리스 함수는 인스턴스가 수시로 생겼다 사라지므로 연결을 쥐고 있지 않는다.
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"poolclass": NullPool}

    db.init_app(app)

    from . import models  # noqa: F401

    # 로그인한 사용자를 먼저 알아내야, 뒤이은 검사(CSRF 등)에서 거절되어 오류 화면이 뜰 때도
    # 하단 메뉴와 닉네임이 그대로 보인다. before_request는 등록한 순서대로 실행된다.
    @app.before_request
    def load_user():
        g.user = None
        user_id = session.get("user_id")
        if user_id is not None:
            user = db.session.get(models.User, user_id)
            # 로그인 때 세션에 비밀번호 해시의 끝자리를 넣어 둔다. 비밀번호를 바꾸면 값이 달라져
            # 다른 기기에 남아 있던 로그인(탈취된 쿠키 포함)이 함께 풀린다.
            stamp = session.get("pw")
            if user is None:
                session.clear()
            elif stamp is not None and stamp != user.password_hash[-8:]:
                session.clear()
                flash("비밀번호가 바뀌어 로그아웃됐어요. 다시 로그인해 주세요.", "info")
            else:
                g.user = user

    csrf.init_app(app)

    from .account import bp as account_bp
    from .auth import bp as auth_bp
    from .chatbot import bp as chat_bp
    from .discover import bp as discover_bp
    from .meetings import bp as meetings_bp
    from .views import register_template_helpers

    app.register_blueprint(auth_bp)
    app.register_blueprint(account_bp)
    app.register_blueprint(discover_bp)
    app.register_blueprint(meetings_bp)
    app.register_blueprint(chat_bp)
    register_template_helpers(app)

    with app.app_context():
        _ensure_tables(app, on_vercel)
        if app.config["SEED_DEMO"]:
            _seed_demo(app)

    @app.before_request
    def reject_nul_bytes():
        # PostgreSQL은 문자열 안의 NUL 문자를 저장하지 못해 500을 낸다. 정상 입력에는 나올 일이 없으므로 거절한다.
        for value in list(request.args.values()) + list(request.form.values()):
            if "\x00" in value:
                abort(400)

    @app.after_request
    def security_headers(response):
        if g.get("user") is not None and response.mimetype == "text/html":
            # 로그인한 사람의 화면은 브라우저가 보관하지 않게 한다. 공용 PC에서 로그아웃한 뒤 '뒤로 가기'로
            # 이전 화면이 다시 보이는 일을 막는다.
            response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "base-uri 'self'; form-action 'self'; frame-ancestors 'none'",
        )
        return response

    @app.errorhandler(CSRFError)
    def csrf_error(error):
        return render_template("error.html", code=400, message="요청이 만료됐어요. 페이지를 새로 고친 뒤 다시 시도해 주세요."), 400

    @app.get("/favicon.ico")
    def favicon():
        # 아이콘은 페이지 안에 넣어 두었다. 브라우저가 습관적으로 찾는 이 주소에는 빈 응답을 준다.
        return "", 204

    @app.errorhandler(400)
    def bad_request(error):
        return render_template("error.html", code=400, message="요청을 처리할 수 없어요. 입력을 확인해 주세요."), 400

    @app.errorhandler(405)
    def method_not_allowed(error):
        return render_template("error.html", code=405, message="이 주소는 그렇게 열 수 없어요. 화면의 버튼을 이용해 주세요."), 405

    @app.errorhandler(500)
    def server_error(error):
        db.session.rollback()
        return render_template("error.html", code=500, message="서버에 문제가 생겼어요. 잠시 뒤 다시 시도해 주세요."), 500

    @app.errorhandler(403)
    def forbidden(error):
        return render_template("error.html", code=403, message="이 작업을 할 권한이 없어요."), 403

    @app.errorhandler(404)
    def not_found(error):
        return render_template("error.html", code=404, message="찾는 페이지가 없어요."), 404

    @app.errorhandler(413)
    def too_large(error):
        return render_template("error.html", code=413, message="입력 내용이 너무 길어요."), 413

    return app
