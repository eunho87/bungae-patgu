"""recommend.py: 조건에 맞는 모임 고르기. 기준 시각 2026-11-12 05:00 UTC (14:00 KST)."""

from datetime import timedelta

import pytest

from bungae import db
from bungae.meetings import validate_meeting_form
from bungae.models import User
from bungae.recommend import Conditions, create_prefill, describe, for_you, open_meetings, recommend, similar_to, time_fit

from .conftest import NOW_UTC, kst


def hours(n):
    return NOW_UTC + timedelta(hours=n)


def minutes(n):
    return NOW_UTC + timedelta(minutes=n)


@pytest.fixture
def host(data):
    return data.user("host")


@pytest.fixture
def me(data):
    return data.user("me")


def run(app, cond, user_id=None, limit=5):
    """(추천 id 목록, 대안 id 목록, {id: Recommendation})"""
    with app.app_context():
        user = db.session.get(User, user_id) if user_id else None
        strict, loose = recommend(cond, user, limit=limit)
        by_id = {rec.meeting.id: rec for rec in strict + loose}
        return [rec.meeting.id for rec in strict], [rec.meeting.id for rec in loose], by_id


# ---------------------------------------------------------------------------
# 시간 구간
# ---------------------------------------------------------------------------


def test_strict_window_fit_near_and_miss(app, data, host, me):
    cond = Conditions(window_start=NOW_UTC, window_end=hours(2), must_end_in_window=True)
    inside = data.meeting(host, title="안에 끝남", start_at=minutes(30), duration_min=60)  # 15:30 끝
    exact = data.meeting(host, title="딱 맞게 끝남", start_at=hours(1), duration_min=60)  # 16:00 끝
    near = data.meeting(host, title="30분 늦게 끝남", start_at=hours(1), duration_min=90)  # 16:30 끝
    late = data.meeting(host, title="31분 늦게 끝남", start_at=minutes(61), duration_min=90)  # 16:31 끝
    later = data.meeting(host, title="한참 늦게 끝남", start_at=hours(1), duration_min=120)  # 17:00 끝
    after = data.meeting(host, title="구간 뒤에 시작", start_at=hours(2), duration_min=30)

    strict, loose, by_id = run(app, cond, me)
    assert set(strict) == {inside, exact, near}
    assert loose == []
    assert late not in strict and later not in strict and after not in strict
    assert by_id[inside].reasons[0] == "시간이 맞아요"
    assert by_id[exact].reasons[0] == "시간이 맞아요"
    assert by_id[near].reasons[0] == "30분 늦게 끝나요"
    # 딱 맞는 모임이 거의 맞는 모임보다 앞에 온다.
    assert strict.index(near) == 2


def test_time_fit_labels(app, data, host):
    cond = Conditions(window_start=NOW_UTC, window_end=hours(2), must_end_in_window=True)
    ids = {
        "fit": data.meeting(host, start_at=minutes(30), duration_min=60),
        "near": data.meeting(host, start_at=minutes(100), duration_min=30),  # 16:10 끝
        "miss": data.meeting(host, start_at=hours(1), duration_min=120),
    }
    with app.app_context():
        from bungae.models import Meeting

        for expected, meeting_id in ids.items():
            fit, _ = time_fit(db.session.get(Meeting, meeting_id), cond)
            assert fit == expected
        fit, reason = time_fit(db.session.get(Meeting, ids["fit"]), Conditions(categories=["game"]))
        assert (fit, reason) == ("none", None)


def test_window_start_excludes_earlier_meetings(app, data, host, me):
    cond = Conditions(window_start=hours(1), window_end=hours(3), must_end_in_window=True)
    early = data.meeting(host, start_at=minutes(30), duration_min=60)
    ok = data.meeting(host, start_at=hours(1), duration_min=60)
    strict, _, _ = run(app, cond, me)
    assert strict == [ok]
    assert early not in strict


def test_non_strict_window_matches_on_start_time(app, data, host, me):
    # 오늘 17:30~21:00 KST, 그 안에 시작하기만 하면 된다.
    cond = Conditions(window_start=kst(2026, 11, 12, 17, 30), window_end=kst(2026, 11, 12, 21, 0))
    before = data.meeting(host, title="17시 시작", start_at=kst(2026, 11, 12, 17, 0), duration_min=120)
    first = data.meeting(host, title="17시 반 시작", start_at=kst(2026, 11, 12, 17, 30), duration_min=60)
    runs_over = data.meeting(host, title="20시 반 시작 2시간", start_at=kst(2026, 11, 12, 20, 30), duration_min=120)
    at_end = data.meeting(host, title="21시 시작", start_at=kst(2026, 11, 12, 21, 0), duration_min=30)
    strict, loose, _ = run(app, cond, me)
    assert set(strict) == {first, runs_over}
    assert before not in strict and at_end not in strict
    assert loose == []


def test_max_duration(app, data, host, me):
    cond = Conditions(window_start=kst(2026, 11, 13), window_end=kst(2026, 11, 14), max_duration_min=120)
    short = data.meeting(host, start_at=kst(2026, 11, 13, 15), duration_min=90)
    exact = data.meeting(host, start_at=kst(2026, 11, 13, 16), duration_min=120)
    long_one = data.meeting(host, start_at=kst(2026, 11, 13, 17), duration_min=180)
    strict, _, by_id = run(app, cond, me)
    assert set(strict) == {short, exact}
    assert long_one not in strict
    assert by_id[short].reasons[0] == "시간이 맞아요"


# ---------------------------------------------------------------------------
# 종류·키워드
# ---------------------------------------------------------------------------


def test_category_filter(app, data, host, me):
    game = data.meeting(host, category="game", start_at=hours(1))
    meal = data.meeting(host, title="마라탕", category="meal", start_at=hours(3))
    strict, loose, by_id = run(app, Conditions(categories=["game"]), me)
    assert strict == [game]
    assert meal not in strict and loose == []
    assert "보드게임·게임 모임" in by_id[game].reasons
    strict, _, _ = run(app, Conditions(categories=["game", "meal"]), me)
    assert set(strict) == {game, meal}


def test_keyword_scoring_ranks_hits_first(app, data, host, me):
    # 같은 종류, 같은 시간대. 키워드가 들어간 모임이 더 늦게 시작해도 먼저 나온다.
    plain = data.meeting(host, title="운동해요", category="sports", start_at=hours(3))
    futsal = data.meeting(host, title="풋살 한 게임", category="sports", start_at=hours(4), tags="가볍게")
    cond = Conditions(categories=["sports"], keywords=["풋살", "가볍게"])
    strict, _, by_id = run(app, cond, me)
    assert strict == [futsal, plain]
    assert by_id[futsal].score == by_id[plain].score + 20
    assert "'풋살' 포함" in by_id[futsal].reasons


def test_keyword_search_covers_title_tags_description_place_case_insensitively(app, data, host, me):
    by_title = data.meeting(host, title="Python 코딩", start_at=hours(1))
    by_tag = data.meeting(host, title="모임1", tags="python", start_at=hours(3))
    by_desc = data.meeting(host, title="모임2", description="PYTHON 같이 해요", start_at=hours(5))
    by_place = data.meeting(host, title="모임3", place="Python 동아리방", start_at=hours(7))
    other = data.meeting(host, title="모임4", start_at=hours(9))
    strict, loose, _ = run(app, Conditions(keywords=["python"]), me)
    assert set(strict) == {by_title, by_tag, by_desc, by_place}
    assert other not in strict and loose == []


def test_keyword_only_without_hit_returns_nothing(app, data, host, me):
    data.meeting(host, title="보드게임", start_at=hours(1))
    strict, loose, _ = run(app, Conditions(keywords=["클라이밍"]), me)
    assert strict == [] and loose == []


def test_empty_condition_returns_all_open_soonest_first(app, data, host, me):
    later = data.meeting(host, start_at=hours(30))
    sooner = data.meeting(host, start_at=hours(20))
    strict, loose, _ = run(app, Conditions(), me)
    assert strict == [sooner, later] and loose == []


def test_limit(app, data, host, me):
    for n in range(7):
        data.meeting(host, start_at=hours(1 + 2 * n))
    strict, _, _ = run(app, Conditions(), me)
    assert len(strict) == 5
    strict, _, _ = run(app, Conditions(), me, limit=2)
    assert len(strict) == 2


def test_interest_bonus(app, data, host):
    fan = data.user("fan", interests="meal")
    game = data.meeting(host, category="game", start_at=hours(20))
    meal = data.meeting(host, category="meal", title="점심", start_at=hours(21))
    strict, _, by_id = run(app, Conditions(), fan)
    assert strict == [meal, game]
    assert "내 관심사" in by_id[meal].reasons


# ---------------------------------------------------------------------------
# 제외 규칙
# ---------------------------------------------------------------------------


def test_excludes_joined_and_overlapping(app, data, host, me):
    other_host = data.user("other")
    joined = data.meeting(host, title="내가 참여", start_at=hours(2), duration_min=60, members=[me])
    overlapping = data.meeting(other_host, title="겹침", start_at=minutes(150), duration_min=60)
    back_to_back = data.meeting(other_host, title="바로 뒤", start_at=hours(3), duration_min=60)
    free = data.meeting(other_host, title="안 겹침", start_at=hours(6), duration_min=60)

    strict, loose, _ = run(app, Conditions(), me)
    assert strict == [back_to_back, free]
    assert joined not in strict and overlapping not in strict and loose == []
    # 로그인하지 않은 관점(user=None)에서는 넷 다 열려 있다.
    strict, _, _ = run(app, Conditions())
    assert set(strict) == {joined, overlapping, back_to_back, free}


def test_excludes_meetings_i_host_and_ones_overlapping_them(app, data, host, me):
    mine = data.meeting(me, title="내가 연 모임", start_at=hours(2), duration_min=120)
    clash = data.meeting(host, title="겹침", start_at=hours(3), duration_min=60)
    strict, _, _ = run(app, Conditions(), me)
    assert mine not in strict and clash not in strict


def test_cancelled_participation_does_not_block(app, data, host, me):
    other_host = data.user("other")
    data.meeting(host, title="취소된 모임", start_at=hours(2), duration_min=60, members=[me], status="cancelled")
    same_time = data.meeting(other_host, title="같은 시간", start_at=hours(2), duration_min=60)
    strict, _, _ = run(app, Conditions(), me)
    assert strict == [same_time]


def test_excludes_full_closed_cancelled_started(app, data, host, me):
    filler = data.user("filler")
    full = data.meeting(host, title="정원 참", capacity=2, start_at=hours(1), members=[filler])
    closed = data.meeting(host, title="마감", start_at=hours(3), status="closed")
    cancelled = data.meeting(host, title="취소", start_at=hours(5), status="cancelled")
    started = data.meeting(host, title="시작함", start_at=minutes(-10), duration_min=120)
    starting_now = data.meeting(host, title="지금 시작", start_at=NOW_UTC)
    ok = data.meeting(host, title="열림", start_at=hours(7))
    for cond in (Conditions(), Conditions(categories=["game"]), Conditions(window_start=NOW_UTC, window_end=hours(24))):
        strict, loose, _ = run(app, cond, me)
        assert strict == [ok]
        assert not {full, closed, cancelled, started, starting_now} & set(strict + loose)
    with app.app_context():
        assert [m.id for m in open_meetings()] == [ok]


# ---------------------------------------------------------------------------
# 대안
# ---------------------------------------------------------------------------


def test_alternatives_when_nothing_matches_strictly(app, data, host, me):
    cond = Conditions(window_start=NOW_UTC, window_end=hours(2), must_end_in_window=True, categories=["game"])
    same_kind = data.meeting(host, title="내일 보드게임", category="game", start_at=hours(24))
    same_time = data.meeting(host, title="지금 밥", category="meal", start_at=minutes(30), duration_min=60)
    unrelated = data.meeting(host, title="내일 밥", category="meal", start_at=hours(26))

    strict, loose, by_id = run(app, cond, me)
    assert strict == []
    assert set(loose) == {same_kind, same_time}
    assert unrelated not in loose
    assert by_id[same_kind].reasons == ["시간은 다르지만 같은 종류예요"]
    assert by_id[same_time].reasons == ["그 시간에 열리는 다른 모임이에요"]


def test_no_alternatives_when_a_strict_match_exists(app, data, host, me):
    cond = Conditions(window_start=NOW_UTC, window_end=hours(2), must_end_in_window=True, categories=["game"])
    match = data.meeting(host, category="game", start_at=minutes(30), duration_min=60)
    data.meeting(host, title="내일 보드게임", category="game", start_at=hours(24))
    strict, loose, _ = run(app, cond, me)
    assert strict == [match] and loose == []


def test_alternatives_capped_at_three(app, data, host, me):
    cond = Conditions(window_start=NOW_UTC, window_end=hours(1), must_end_in_window=True, categories=["game"])
    for n in range(5):
        data.meeting(host, category="game", start_at=hours(24 + 2 * n))
    strict, loose, _ = run(app, cond, me)
    assert strict == [] and len(loose) == 3


def test_nothing_at_all(app, me):
    assert run(app, Conditions(categories=["game"]), me)[:2] == ([], [])


# ---------------------------------------------------------------------------
# 홈·상세가 쓰는 래퍼
# ---------------------------------------------------------------------------


def test_for_you_and_similar_to(app, data, host):
    fan = data.user("fan", interests="game,study")
    plain = data.user("plain")
    game1 = data.meeting(host, category="game", start_at=hours(20))
    game2 = data.meeting(host, title="마작", category="game", start_at=hours(22))
    study = data.meeting(host, title="카공", category="study", start_at=hours(24))
    meal = data.meeting(host, title="밥", category="meal", start_at=hours(26))
    with app.app_context():
        from bungae.models import Meeting

        got = [rec.meeting.id for rec in for_you(db.session.get(User, fan))]
        assert got == [game1, game2, study]
        assert for_you(db.session.get(User, plain)) == []
        similar = similar_to(db.session.get(Meeting, game1), db.session.get(User, plain))
        assert [rec.meeting.id for rec in similar] == [game2]
        assert meal not in got


# ---------------------------------------------------------------------------
# describe / create_prefill
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cond, expected",
    [
        (Conditions(), []),
        (
            Conditions(window_start=NOW_UTC, window_end=NOW_UTC + timedelta(hours=2), must_end_in_window=True, categories=["game"]),
            ["오늘 14:00~16:00", "보드게임·게임"],
        ),
        (
            Conditions(window_start=kst(2026, 11, 13, 17, 30), window_end=kst(2026, 11, 13, 21), categories=["meal"]),
            ["내일 17:30~21:00", "식사"],
        ),
        (Conditions(window_start=kst(2026, 11, 13), window_end=kst(2026, 11, 14)), ["내일"]),
        (Conditions(window_start=kst(2026, 11, 12, 15), window_end=kst(2026, 11, 13)), ["오늘 15:00 이후"]),
        (Conditions(window_start=kst(2026, 11, 14, 9, 30), window_end=kst(2026, 11, 15)), ["모레 09:30 이후"]),
        (Conditions(window_start=kst(2026, 11, 16, 15), window_end=kst(2026, 11, 17)), ["11/16(월) 15:00 이후"]),
        (
            Conditions(window_start=kst(2026, 11, 13), window_end=kst(2026, 11, 14), max_duration_min=90),
            ["내일", "1시간 30분 이내"],
        ),
        (Conditions(categories=["sports"], keywords=["풋살", "가볍게"]), ["운동", "#풋살", "#가볍게"]),
        (Conditions(window_start=kst(2026, 11, 12, 15)), ["오늘 15:00 이후"]),
        (Conditions(window_end=kst(2026, 11, 12, 18), must_end_in_window=True), ["오늘 18:00까지"]),
    ],
)
def test_describe(cond, expected):
    assert describe(cond) == expected


def test_create_prefill_empty():
    assert create_prefill(Conditions()) == {}


def test_create_prefill_now_window():
    cond = Conditions(window_start=NOW_UTC, window_end=NOW_UTC + timedelta(hours=2), must_end_in_window=True, categories=["game", "meal"])
    params = create_prefill(cond)
    assert params["category"] == "game"
    assert (params["date"], params["time"]) == ("2026-11-12", "14:10")  # '지금'은 10분 뒤로 올려 준다
    assert params["duration"] in (60, 90, 120)


def test_create_prefill_rounds_up_to_ten_minutes():
    cond = Conditions(window_start=kst(2026, 11, 12, 15, 23), window_end=kst(2026, 11, 13))
    params = create_prefill(cond)
    assert (params["date"], params["time"]) == ("2026-11-12", "15:30")
    assert "duration" not in params and "category" not in params


def test_create_prefill_future_window_and_durations():
    cond = Conditions(window_start=kst(2026, 11, 13, 17, 30), window_end=kst(2026, 11, 13, 21), categories=["meal"])
    assert create_prefill(cond) == {"category": "meal", "date": "2026-11-13", "time": "17:30"}
    cond = Conditions(window_start=kst(2026, 11, 13), window_end=kst(2026, 11, 14), max_duration_min=100)
    assert create_prefill(cond)["duration"] == 90
    cond = Conditions(window_start=kst(2026, 11, 13, 15), window_end=kst(2026, 11, 13, 15, 20), must_end_in_window=True)
    assert create_prefill(cond)["duration"] == 30  # 가장 짧은 선택지


def test_create_prefill_passes_the_create_form():
    cond = Conditions(window_start=NOW_UTC, window_end=NOW_UTC + timedelta(hours=2), must_end_in_window=True, categories=["game"])
    params = {key: str(value) for key, value in create_prefill(cond).items()}
    form = {"title": "보드게임 한 판", "place": "학생회관", "capacity": "4", **params}
    _, errors = validate_meeting_form(form)
    assert errors == {}


def test_create_prefill_for_now_window_fits_inside_the_window():
    """'지금 2시간 비는데'로 만든 폼 값은 그 2시간 안에 끝나는 모임이어야 한다.

    시작을 14:10으로 올리면서 길이는 120분 그대로 두면 16:10에 끝나 말한 빈 시간을 넘긴다.
    """
    cond = Conditions(window_start=NOW_UTC, window_end=NOW_UTC + timedelta(hours=2), must_end_in_window=True)
    params = create_prefill(cond)
    start = kst(*map(int, params["date"].split("-")), *map(int, params["time"].split(":")))
    assert start + timedelta(minutes=params["duration"]) <= cond.window_end
