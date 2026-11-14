"""CSRF 보호와 보안 헤더 (__init__.py)."""

import pytest
from itsdangerous import URLSafeTimedSerializer

from bungae.models import Comment

from .conftest import Data, login_as

CSP_PARTS = [
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
]


def assert_security_headers(response):
    csp = response.headers.get("Content-Security-Policy", "")
    for part in CSP_PARTS:
        assert part in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert response.headers.get("X-Frame-Options") == "DENY"
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("Referrer-Policy") == "same-origin"


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


@pytest.fixture
def csrf_app(make_app):
    return make_app(WTF_CSRF_ENABLED=True)


@pytest.fixture
def csrf(csrf_app):
    """(로그인된 클라이언트, Data, 호스트 id, 모임 id, 로그인 사용자 id) - CSRF 검사가 켜진 앱."""
    data = Data(csrf_app)
    host = data.user("host")
    me = data.user("me")
    meeting = data.meeting(host)
    client = csrf_app.test_client()
    login_as(client, me)
    return client, data, host, meeting, me


def issue_token(app, client):
    """Flask-WTF가 만드는 것과 같은 방식으로 세션에 묶인 토큰을 만든다."""
    with client.session_transaction() as sess:
        sess["csrf_token"] = "0123456789abcdef0123456789abcdef01234567"
    serializer = URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="wtf-csrf-token")
    return serializer.dumps("0123456789abcdef0123456789abcdef01234567")


@pytest.mark.needs_template
def test_join_without_token_is_400(csrf):
    client, data, host, meeting, me = csrf
    response = client.post(f"/meetings/{meeting}/join")
    assert response.status_code == 400
    assert data.member_ids(meeting) == [host]
    assert_security_headers(response)


@pytest.mark.needs_template
def test_api_chat_without_token_is_400(csrf):
    client, _, _, _, _ = csrf
    response = client.post("/api/chat", json={"message": "보드게임 할 사람"})
    assert response.status_code == 400
    assert not (response.is_json and "meetings" in response.get_json())


@pytest.mark.needs_template
@pytest.mark.parametrize(
    "path, payload",
    [
        ("/meetings/new", {"title": "가로챈 모임"}),
        ("/meetings/1/leave", {}),
        ("/meetings/1/edit", {"title": "가로챈 제목"}),
        ("/meetings/1/status", {"action": "cancel"}),
        ("/meetings/1/comments", {"body": "스팸"}),
        ("/me/interests", {"interests": "game"}),
        ("/logout", {}),
        ("/api/chat/reset", {}),
        ("/login", {"email": "me@ssu.ac.kr", "password": "x"}),
        ("/signup", {"email": "x@ssu.ac.kr"}),
    ],
)
def test_every_post_requires_a_token(csrf, path, payload):
    client, data, host, meeting, me = csrf
    response = client.post(path, data=payload)
    assert response.status_code == 400
    assert data.meeting_field(meeting, "status") == "open"
    assert data.count(Comment) == 0
    with client.session_transaction() as sess:
        assert sess.get("user_id") == me  # 로그아웃되지도 않았다


@pytest.mark.needs_template
@pytest.mark.parametrize("token", ["", "forged", "0123456789abcdef0123456789abcdef01234567"])
def test_wrong_token_is_400(csrf, token):
    client, data, host, meeting, me = csrf
    assert client.post(f"/meetings/{meeting}/join", data={"csrf_token": token}).status_code == 400
    assert client.post("/api/chat", json={"message": "보드게임"}, headers={"X-CSRFToken": token}).status_code == 400
    assert data.member_ids(meeting) == [host]


@pytest.mark.needs_template
def test_token_from_another_session_is_rejected(csrf_app, csrf):
    client, data, host, meeting, me = csrf
    other = csrf_app.test_client()
    login_as(other, host)
    stolen = issue_token(csrf_app, other)
    assert client.post(f"/meetings/{meeting}/join", data={"csrf_token": stolen}).status_code == 400
    assert data.member_ids(meeting) == [host]


def test_valid_form_token_is_accepted(csrf_app, csrf):
    client, data, host, meeting, me = csrf
    token = issue_token(csrf_app, client)
    assert client.post(f"/meetings/{meeting}/join", data={"csrf_token": token}).status_code == 302
    assert data.member_ids(meeting) == [host, me]


def test_valid_header_token_is_accepted_by_the_api(csrf_app, csrf):
    client, _, _, _, _ = csrf
    token = issue_token(csrf_app, client)
    response = client.post("/api/chat", json={"message": "보드게임 할 사람"}, headers={"X-CSRFToken": token})
    assert response.status_code == 200
    assert response.get_json()["mode"] == "rule"
    assert client.post("/api/chat/reset", headers={"X-CSRFToken": token}).get_json() == {"ok": True}


@pytest.mark.needs_template
def test_pages_embed_a_token_that_works(csrf_app, csrf):
    """화면에 실린 토큰(meta 태그)으로 실제 요청이 통과하는지."""
    import re

    client, data, host, meeting, me = csrf
    html = client.get(f"/meetings/{meeting}").get_data(as_text=True)
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    assert client.post(f"/meetings/{meeting}/join", data={"csrf_token": match.group(1)}).status_code == 302
    assert data.member_ids(meeting) == [host, me]


# ---------------------------------------------------------------------------
# 보안 헤더
# ---------------------------------------------------------------------------


def test_headers_on_redirect_and_json(client, data):
    assert_security_headers(client.get("/"))  # 로그인으로 보내는 302
    assert_security_headers(client.post("/api/chat", json={"message": "x"}))  # 401 JSON
    login_as(client, data.user("me"))
    assert_security_headers(client.post("/api/chat", json={"message": "보드게임"}))
    assert_security_headers(client.post("/api/chat", json={}))  # 400 JSON
    assert_security_headers(client.get("/logout"))  # 405


@pytest.mark.needs_template
def test_headers_on_html_pages(client, data):
    assert_security_headers(client.get("/login"))
    assert_security_headers(client.get("/signup"))
    login_as(client, data.user("me"))
    for path in ("/", "/chat", "/me", "/meetings/new"):
        response = client.get(path)
        assert response.status_code == 200
        assert_security_headers(response)


@pytest.mark.needs_template
def test_headers_on_error_pages(client, data):
    login_as(client, data.user("me"))
    response = client.get("/meetings/999")
    assert response.status_code == 404
    assert_security_headers(response)
    assert_security_headers(client.get("/no-such-page"))


@pytest.mark.needs_template
def test_pages_have_no_inline_script_or_style(client, data):
    """CSP가 script-src 'self' / style-src 'self' 이므로 인라인 스크립트·스타일은 브라우저에서 막힌다."""
    import re

    host = data.user("host")
    meeting = data.meeting(host, contact_link="https://open.kakao.com/o/abc")
    pages = [client.get("/login"), client.get("/signup")]
    login_as(client, host)
    pages += [client.get(path) for path in ("/", "/chat", "/me", "/meetings/new", f"/meetings/{meeting}", f"/meetings/{meeting}/edit")]
    for response in pages:
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        for tag in re.findall(r"<script\b[^>]*>", html):
            assert "src=" in tag, tag
        assert not re.search(r"<style\b", html)
        assert not re.search(r"\son[a-z]+\s*=\s*[\"']", html), "인라인 이벤트 핸들러"
        assert not re.search(r"\sstyle\s*=\s*[\"']", html), "인라인 style 속성"
