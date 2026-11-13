"""테스트로 발견된 결함을 고치면서 함께 바꾼 동작을 고정해 두는 테스트."""

from datetime import datetime

from bungae import timeutil
from bungae.nlu import parse_rules, slots_to_conditions
from bungae.recommend import create_prefill, describe
from bungae.timeutil import from_kst, to_kst

from .conftest import NOW_KST


def window(text, now=NOW_KST):
    slots, _, _ = parse_rules(text, now)
    cond = slots_to_conditions(slots, now)
    start = to_kst(cond.window_start) if cond.window_start else None
    end = to_kst(cond.window_end) if cond.window_end else None
    return cond, start, end


def test_evening_range_without_meridiem_means_tonight():
    now = datetime(2026, 11, 12, 20, 0)
    timeutil.set_fixed_now(from_kst(now))
    cond, start, end = window("9시부터 11시까지 비어", now)
    assert (start, end) == (datetime(2026, 11, 12, 21, 0), datetime(2026, 11, 12, 23, 0))
    assert cond.must_end_in_window


def test_short_range_form_reads_both_ends():
    cond, start, end = window("3~5시 비어")
    assert (start.hour, end.hour) == (15, 17)
    assert cond.must_end_in_window


def test_colon_time_follows_the_same_afternoon_rule_as_spoken_time():
    _, start, _ = window("3:30에 카페")
    assert (start.hour, start.minute) == (15, 30)
    _, start, _ = window("내일 09:30 공부")
    assert (start.hour, start.minute) == (9, 30)


def test_month_day_date_is_used():
    slots, _, _ = parse_rules("11월 13일 3시 보드게임", NOW_KST)
    assert slots["date"] == "2026-11-13"
    assert slots["start"] == "15:00"


def test_until_time_that_already_passed_today_is_expired_not_inverted():
    cond, start, end = window("오늘 1시까지 밥")
    assert cond.expired
    assert start <= end
    assert describe(cond)[0] == "오늘 13:00까지"
    assert "time" not in create_prefill(cond)


def test_until_time_without_date_rolls_to_tomorrow():
    cond, start, end = window("12시까지 카페")
    assert not cond.expired
    assert start == NOW_KST
    assert end == datetime(2026, 11, 13, 12, 0)
    assert describe(cond)[0] == "오늘 14:00~내일 12:00"


def test_chat_reports_expired_window(as_user, data):
    user = data.user()
    client = as_user(user)
    response = client.post("/api/chat", json={"message": "오늘 오전에 공부"})
    assert response.status_code == 200
    body = response.get_json()
    assert body["kind"] == "expired"
    assert "지났" in body["reply"]


def test_prefill_duration_fits_the_remaining_free_time():
    cond, _, _ = window("지금 2시간 비는데 보드게임")
    params = create_prefill(cond)
    assert params["time"] == "14:10"
    assert params["duration"] == 90


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {"choices": [{"message": {"content": '{"intent": "search", "categories": ["game"]}'}}]}

    def json(self):
        return self._body


def capture_post(monkeypatch, response=None):
    from bungae import llm

    sent = {}

    def fake_post(url, **kwargs):
        sent.update(kwargs["json"])
        return response or FakeResponse()

    monkeypatch.setattr(llm.requests, "post", fake_post)
    return sent


def test_llm_sends_a_fallback_list_when_several_models_are_named(monkeypatch):
    from bungae import llm

    sent = capture_post(monkeypatch)
    llm.ask("보드게임", NOW_KST, None, "k", "a/one:free, b/two:free")
    assert sent["models"] == ["a/one:free", "b/two:free"]
    assert "model" not in sent


def test_llm_leaves_room_for_reasoning_models(monkeypatch):
    from bungae import llm

    sent = capture_post(monkeypatch)
    llm.ask("보드게임", NOW_KST, None, "k", "a/one:free")
    assert sent["model"] == "a/one:free"
    assert sent["max_tokens"] >= 2000


def test_llm_error_carries_the_upstream_reason(monkeypatch):
    import pytest

    from bungae import llm

    body = {"error": {"message": "Provider returned error", "metadata": {"raw": "model is temporarily rate-limited upstream"}}}
    capture_post(monkeypatch, FakeResponse(429, body))
    with pytest.raises(llm.LLMError) as caught:
        llm.ask("보드게임", NOW_KST, None, "k", "a/one:free")
    assert "429" in str(caught.value) and "rate-limited upstream" in str(caught.value)


def test_model_chain_puts_the_chosen_model_first_and_caps_at_three(make_app):
    from bungae.chatbot import _model_chain

    app = make_app(OPENROUTER_MODEL="my/pick:free", OPENROUTER_FALLBACK_MODELS="x/a:free, my/pick:free, x/b:free, x/c:free")
    with app.app_context():
        assert _model_chain() == "my/pick:free,x/a:free,x/b:free"
    app = make_app(OPENROUTER_MODEL="my/pick:free", OPENROUTER_FALLBACK_MODELS="none")
    with app.app_context():
        assert _model_chain() == "my/pick:free"


def test_favicon_request_is_not_a_404(client):
    assert client.get("/favicon.ico").status_code == 204


def test_generic_words_are_dropped_from_model_keywords():
    from bungae.nlu import sanitize_slots

    raw = {"categories": ["study"], "keywords": ["점심", "조용히", "공부", "#운동", "마라탕", "보드게임"]}
    assert sanitize_slots(raw, NOW_KST.date())["keywords"] == ["조용히", "마라탕"]


def test_blocked_meeting_is_hidden_from_strangers_but_not_from_its_people(data, as_user):
    host, member, stranger = data.user(), data.user(), data.user()
    data.meeting(host, title="신고로멈춘모임", members=[member], status="blocked")
    assert "신고로멈춘모임" not in as_user(stranger).get("/").get_data(as_text=True)
    assert "신고로멈춘모임" in as_user(member).get("/").get_data(as_text=True)
    assert "신고로멈춘모임" in as_user(host).get("/").get_data(as_text=True)


def test_schema_and_row_lock_compile_for_postgresql(app):
    """배포 DB는 PostgreSQL이다. 테이블 정의와 행 잠금 문장이 그 문법으로 만들어지는지 확인한다(연결은 하지 않는다)."""
    from sqlalchemy import select
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    from bungae import db
    from bungae.models import Meeting

    dialect = postgresql.dialect()
    with app.app_context():
        tables = db.metadata.sorted_tables
        ddl = "\n".join(str(CreateTable(table).compile(dialect=dialect)) for table in tables)
        lock = str(select(Meeting).where(Meeting.id == 1).with_for_update(of=Meeting).compile(dialect=dialect))
    assert len(tables) == 8
    assert "ON DELETE SET NULL" in ddl  # 알림이 가리키는 모임이 지워질 때
    assert lock.rstrip().endswith("FOR UPDATE OF meetings")
