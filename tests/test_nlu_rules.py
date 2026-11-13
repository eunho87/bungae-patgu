"""규칙 기반 해석(nlu.parse_rules)과 슬롯 -> 검색 구간(nlu.slots_to_conditions).

기준 시각: 2026-11-12(목) 14:00 KST.
"""

from datetime import datetime, timedelta

import pytest

from bungae.nlu import merge_slots, parse_rules, slots_to_conditions, wants_browse
from bungae.timeutil import to_kst

from .conftest import NOW_KST

D12 = "2026-11-12"
D13 = "2026-11-13"


def slots_of(text, now=NOW_KST):
    return parse_rules(text, now)[0]


def cond_of(text, now=NOW_KST):
    return slots_to_conditions(slots_of(text, now), now)


def window(text, now=NOW_KST):
    """(시작, 끝)을 KST로."""
    cond = cond_of(text, now)
    start = to_kst(cond.window_start) if cond.window_start else None
    end = to_kst(cond.window_end) if cond.window_end else None
    return start, end


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute)


# ---------------------------------------------------------------------------
# 과제 설명에 적힌 기본 문장
# ---------------------------------------------------------------------------


def test_free_two_hours_now_boardgame():
    text = "지금 2시간 비는데 보드게임 할 사람 있어?"
    slots, found, reset = parse_rules(text, NOW_KST)
    assert found and not reset
    assert slots["duration_min"] == 120
    assert slots["categories"] == ["game"]
    cond = cond_of(text)
    assert window(text) == (at(12, 14), at(12, 16))
    assert cond.must_end_in_window is True
    assert cond.categories == ["game"]


def test_today_three_to_five_free():
    text = "오늘 3시부터 5시까지 비어"
    slots = slots_of(text)
    assert (slots["date"], slots["start"], slots["end"], slots["strict_end"]) == (D12, "15:00", "17:00", True)
    assert window(text) == (at(12, 15), at(12, 17))
    assert cond_of(text).must_end_in_window is True


def test_tomorrow_evening_meal():
    text = "내일 저녁에 밥 먹을 사람"
    slots = slots_of(text)
    assert slots["date"] == D13
    assert slots["categories"] == ["meal"]
    assert window(text) == (at(13, 17, 30), at(13, 21))
    assert cond_of(text).must_end_in_window is False


def test_until_six_cafe():
    text = "6시 전까지 카페"
    slots = slots_of(text)
    assert slots["start"] is None
    assert (slots["end"], slots["strict_end"]) == ("18:00", True)
    assert slots["categories"] == ["cafe"]
    assert window(text) == (at(12, 14), at(12, 18))
    assert cond_of(text).must_end_in_window is True


def test_in_one_hour_futsal():
    text = "1시간 뒤에 가볍게 풋살"
    slots = slots_of(text)
    assert slots["start"] == "15:00"
    assert slots["duration_min"] is None
    assert slots["categories"] == ["sports"]
    assert "풋살" in slots["keywords"]
    start, _ = window(text)
    assert start == at(12, 15)
    assert cond_of(text).must_end_in_window is False


def test_friday_nine_thirty_study_cafe_is_study_only():
    text = "금요일 9시 반에 스터디카페 카공"
    slots = slots_of(text)
    assert slots["date"] == D13
    assert slots["start"] == "09:30"
    assert slots["categories"] == ["study"]  # '스터디카페'의 카페는 cafe가 아니다
    assert window(text)[0] == at(13, 9, 30)


def test_duration_offset_vs_clock():
    offset = slots_of("3시간 뒤")
    clock = slots_of("3시")
    assert offset["start"] == "17:00" and offset["duration_min"] is None
    assert clock["start"] == "15:00" and clock["duration_min"] is None
    plain = slots_of("3시간 비어")
    assert plain["duration_min"] == 180 and plain["start"] is None


def test_gisul_yesul_is_not_drink():
    slots, found, _ = parse_rules("기술 예술 얘기", NOW_KST)
    assert "drink" not in slots["categories"]
    assert slots["categories"] == []
    assert not found


@pytest.mark.parametrize("text, expected", [("술 마실 사람", ["drink"]), ("오늘 술 한잔", ["drink"]), ("미술 전시 볼 사람", ["culture"])])
def test_drink_word_boundaries(text, expected):
    assert slots_of(text)["categories"] == expected


def test_hello_finds_nothing():
    slots, found, reset = parse_rules("안녕", NOW_KST)
    assert not found and not reset
    assert not any(slots[key] for key in ("date", "start", "end", "duration_min", "categories", "keywords"))
    assert not wants_browse("안녕")


@pytest.mark.parametrize("text", ["", "   ", "ㅋㅋㅋㅋ", "hello", "25시", "5분", "혼술", "하네 시작", "모든 시간 가능"])
def test_nothing_found(text):
    slots, found, _ = parse_rules(text, NOW_KST)
    assert not found
    assert cond_of(text).is_empty


# ---------------------------------------------------------------------------
# "오전 10시 공부"를 오후 2시에 물었을 때
# ---------------------------------------------------------------------------


def test_past_morning_clock_parses_as_ten_am():
    slots = slots_of("오전 10시 공부")
    assert slots["start"] == "10:00"
    assert slots["date"] is None
    assert slots["categories"] == ["study"]


@pytest.mark.parametrize(
    "text, expected_start",
    [
        ("오전 10시 공부", at(13, 10)),
        ("새벽 1시", at(13, 1)),
        ("12시에 점심 먹을 사람", at(13, 12)),
    ],
)
def test_explicit_clock_already_past_today_rolls_to_tomorrow(text, expected_start):
    """날짜 없이 말한 시각이 오늘 이미 지났으면 내일 그 시각이어야 한다.

    slots_to_conditions는 '저녁에'처럼 구간으로 말한 경우에만 내일로 넘긴다("아침에 공부"는
    내일 07:00~10:00이 된다). 시각 하나만 말한 경우에는 구간 끝을 자정으로 잡아서 넘기지 않고,
    시작을 '지금'으로 끌어올려 '오늘 14:00 이후 아무 때나'가 된다. 오전 10시를 물었는데
    오늘 저녁 모임이 추천된다.
    """
    start, _ = window(text)
    assert start == expected_start


def test_day_part_already_past_today_rolls_to_tomorrow():
    assert window("아침에 공부") == (at(13, 7), at(13, 10))
    assert window("점심 먹을 사람") == (at(13, 11, 30), at(13, 13, 30))
    assert window("저녁 먹을 사람") == (at(12, 17, 30), at(12, 21))


# ---------------------------------------------------------------------------
# 시각 표현
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, start",
    [
        ("오후 3시 반", "15:30"),
        ("세 시 20분에 산책", "15:20"),
        ("3시30분", "15:30"),
        ("15:30 카페", "15:30"),
        ("15시 30분", "15:30"),
        ("저녁 7시쯤 마라탕", "19:00"),
        ("9시", "21:00"),  # 오늘 오전 9시는 지났으므로 밤 9시
        ("8시 반", "20:30"),
        ("열두 시", "12:00"),
        ("3시 이후에 할 만한 거", "15:00"),
        ("3시 간식 먹자", "15:00"),
        ("지금 카페 갈 사람", "14:00"),
    ],
)
def test_single_clock_is_start(text, start):
    slots = slots_of(text)
    assert slots["start"] == start
    assert slots["end"] is None


@pytest.mark.parametrize(
    "text, end",
    [
        ("6시 반까지 도서관에서 조용히 공부", "18:30"),
        ("5시 전에 끝나는 거", "17:00"),
        ("5시 이전", "17:00"),
        ("6시쯤까지", "18:00"),
        ("밤 12시까지", "23:59"),
    ],
)
def test_until_clock_is_strict_end(text, end):
    slots = slots_of(text)
    assert (slots["start"], slots["end"], slots["strict_end"]) == (None, end, True)


def test_tomorrow_morning_clock_stays_morning():
    slots = slots_of("내일 아침 8시 러닝")
    assert (slots["date"], slots["start"]) == (D13, "08:00")
    assert slots["categories"] == ["sports"]
    assert slots_of("내일 10시부터 1시까지 비어")["start"] == "10:00"
    assert window("내일 10시부터 1시까지 비어") == (at(13, 10), at(13, 13))


def test_start_plus_duration_is_strict_window():
    assert window("3시부터 2시간 비어") == (at(12, 15), at(12, 17))
    assert cond_of("3시부터 2시간 비어").must_end_in_window is True
    assert window("3시 30분부터 1시간") == (at(12, 15, 30), at(12, 16, 30))


def test_clock_minutes_are_not_read_as_duration():
    slots = slots_of("3시 30분에 카페")
    assert slots["start"] == "15:30"
    assert slots["duration_min"] is None


@pytest.mark.parametrize(
    "text, minutes",
    [
        ("공강 1시간 30분", 90),
        ("두시간반 비어", 150),
        ("한 시간 비어", 60),
        ("100분 비어", 100),
        ("1.5시간 비어", 90),
        ("30분만 시간 돼", 30),
    ],
)
def test_durations(text, minutes):
    slots = slots_of(text)
    assert slots["duration_min"] == minutes
    assert slots["start"] is None and slots["end"] is None
    start, end = window(text)
    assert start == NOW_KST and end == NOW_KST + timedelta(minutes=minutes)


@pytest.mark.parametrize(
    "text, start",
    [
        ("30분 뒤", "14:30"),
        ("한시간뒤", "15:00"),
        ("두 시간 후에 농구", "16:00"),
        ("1시간 반 뒤에 농구", "15:30"),
        ("1.5시간 뒤", "15:30"),
        ("2시간 있다가 밥", "16:00"),
    ],
)
def test_offsets(text, start):
    slots = slots_of(text)
    assert slots["start"] == start
    assert slots["duration_min"] is None


def test_offset_then_duration():
    text = "2시간 뒤에 1시간 정도 탁구"
    assert window(text) == (at(12, 16), at(12, 17))
    assert cond_of(text).must_end_in_window is True


def test_offset_crossing_midnight_moves_the_date():
    slots = slots_of("11시간 뒤")
    assert (slots["date"], slots["start"]) == (D13, "01:00")


def test_two_clocks_without_free_word_is_not_strict():
    slots = slots_of("3시~5시")
    assert (slots["start"], slots["end"], slots["strict_end"]) == ("15:00", "17:00", False)


def test_window_with_max_duration():
    cond = cond_of("4시부터 6시 사이에 1시간짜리")
    assert window("4시부터 6시 사이에 1시간짜리") == (at(12, 16), at(12, 18))
    assert cond.max_duration_min == 60
    cond = cond_of("내일 2시간짜리 보드게임")
    assert window("내일 2시간짜리 보드게임") == (at(13, 0), at(14, 0))
    assert cond.max_duration_min == 120 and cond.must_end_in_window is False


# ---------------------------------------------------------------------------
# 기준 시각을 바꿔 본 경우
# ---------------------------------------------------------------------------


def test_morning_clock_rule_depends_on_the_current_time():
    nine_am = datetime(2026, 11, 12, 9, 0)
    assert slots_of("10시", nine_am)["start"] == "10:00"  # 아직 안 지났으면 오전
    assert slots_of("10시", datetime(2026, 11, 12, 10, 30))["start"] == "22:00"  # 지났으면 밤
    assert window("10시부터 1시까지 비어", nine_am) == (at(12, 10), at(12, 13))
    assert window("3시부터 5시까지", datetime(2026, 11, 12, 16, 0)) == (at(12, 16), at(12, 17))  # 이미 시작된 구간
    assert window("3시부터 5시까지", datetime(2026, 11, 12, 18, 0)) == (at(13, 15), at(13, 17))  # 다 지났으면 내일


def test_evening_part_late_at_night_means_tomorrow():
    late = datetime(2026, 11, 12, 21, 30)
    assert window("저녁에 밥", late) == (at(13, 17, 30), at(13, 21))
    assert window("저녁에 밥", datetime(2026, 11, 12, 20, 0)) == (at(12, 20), at(12, 21))


def test_just_after_midnight_kst():
    """UTC로는 아직 전날이어도 '오늘'은 한국 날짜다."""
    one_am = datetime(2026, 11, 13, 1, 0)
    assert slots_of("오늘 저녁에 밥", one_am)["date"] == "2026-11-13"
    assert slots_of("내일", one_am)["date"] == "2026-11-14"
    assert slots_of("금요일", one_am)["date"] == "2026-11-13"
    assert window("2시간 비어", datetime(2026, 11, 12, 23, 30)) == (at(12, 23, 30), at(13, 1, 30))
    assert slots_of("1시간 뒤", datetime(2026, 11, 12, 23, 30)) ["date"] == "2026-11-13"


# ---------------------------------------------------------------------------
# 날짜 표현
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("오늘", D12),
        ("내일", D13),
        ("모레", "2026-11-14"),
        ("내일모레 영화", "2026-11-14"),
        ("목요일", D12),
        ("금요일", D13),
        ("토요일", "2026-11-14"),
        ("일요일 저녁", "2026-11-15"),
        ("월요일 3시", "2026-11-16"),
        ("수요일", "2026-11-18"),
        ("다음 주 목요일", "2026-11-19"),
        ("다음주 금요일", "2026-11-20"),
    ],
)
def test_dates(text, expected):
    assert slots_of(text)["date"] == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("다음 주 월요일 보드게임", "2026-11-16"),
        ("다음 주 화요일", "2026-11-17"),
        ("다음 주 수요일", "2026-11-18"),
    ],
)
def test_next_week_weekday_earlier_than_today(text, expected):
    """목요일에 말한 '다음 주 월요일'은 나흘 뒤(11/16)다.

    nlu.py는 '가장 가까운 그 요일'(이미 다음 주)에 7일을 더해 11/23을 돌려준다.
    오늘보다 앞선 요일(월·화·수)에서만 한 주가 더 밀린다.
    """
    assert slots_of(text)["date"] == expected


# ---------------------------------------------------------------------------
# 종류·키워드
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, categories, keywords",
    [
        ("PC방 갈 사람", ["game"], ["pc방"]),
        ("오늘 밤에 치맥", ["drink"], ["치맥"]),
        ("영화 보고 밥 먹자", ["meal", "culture"], ["영화"]),
        ("독서실에서 과제", ["study"], ["독서실"]),
        ("독서 모임", ["culture"], ["독서"]),
        ("카페에서 수다", ["cafe"], []),
        ("24시간 카페", ["cafe"], []),
        ("주말에 영화", ["culture"], ["영화"]),
    ],
)
def test_categories_and_keywords(text, categories, keywords):
    slots = slots_of(text)
    assert slots["categories"] == categories
    assert slots["keywords"] == keywords


def test_mood_and_place_keywords():
    slots = slots_of("6시 반까지 도서관에서 조용히 공부")
    assert slots["categories"] == ["study"]
    assert set(slots["keywords"]) == {"조용", "도서관"}


def test_keywords_capped_at_five():
    slots = slots_of("축구 풋살 농구 배드민턴 러닝 조깅 산책 탁구")
    assert len(slots["keywords"]) == 5


@pytest.mark.parametrize("text", ["아무거나", "상관없어", "뭐든 좋아", "다른 거 없어?"])
def test_reset_topic(text):
    slots, found, reset = parse_rules(text, NOW_KST)
    assert found and reset
    assert slots["categories"] == [] and slots["keywords"] == []


@pytest.mark.parametrize("text", ["추천해줘", "심심해", "뭐 할 만한 거 없어?", "지금 열린 번개 있어?"])
def test_wants_browse(text):
    assert wants_browse(text)


# ---------------------------------------------------------------------------
# 이어지는 대화
# ---------------------------------------------------------------------------


def test_merge_followup_keeps_category():
    prev = slots_of("지금 2시간 비는데 보드게임 할 사람 있어?")
    new, found, reset = parse_rules("그럼 내일은?", NOW_KST)
    assert found and not reset
    merged = merge_slots(prev, new, reset)
    assert merged["date"] == D13
    assert merged["categories"] == ["game"]
    assert merged["duration_min"] is None  # 시간 조건은 새로 말한 것으로 바뀐다
    assert window_of_slots(merged) == (at(13, 0), at(14, 0))


def test_merge_anything_resets_category_but_keeps_time():
    prev = slots_of("내일 저녁에 밥 먹을 사람")
    new, found, reset = parse_rules("아무거나", NOW_KST)
    assert found and reset
    merged = merge_slots(prev, new, reset)
    assert merged["categories"] == [] and merged["keywords"] == []
    assert (merged["date"], merged["start"], merged["end"]) == (D13, "17:30", "21:00")


def test_merge_new_category_keeps_time():
    prev = slots_of("오늘 3시부터 5시까지 비어")
    new, _, reset = parse_rules("카페는?", NOW_KST)
    merged = merge_slots(prev, new, reset)
    assert merged["categories"] == ["cafe"]
    assert (merged["date"], merged["start"], merged["end"], merged["strict_end"]) == (D12, "15:00", "17:00", True)


def test_merge_new_clock_keeps_previous_date_and_category():
    prev = slots_of("내일 3시 보드게임")
    new, _, reset = parse_rules("그럼 5시는?", NOW_KST)
    merged = merge_slots(prev, new, reset)
    assert (merged["date"], merged["start"]) == (D13, "17:00")
    assert merged["categories"] == ["game"]


def test_merge_without_previous_returns_new():
    new = slots_of("내일 보드게임")
    assert merge_slots(None, new) == new


def window_of_slots(slots):
    cond = slots_to_conditions(slots, NOW_KST)
    return to_kst(cond.window_start), to_kst(cond.window_end)


# ---------------------------------------------------------------------------
# 아래부터는 까다로운 표현. 실패하는 것은 파서가 틀린 조건을 만든 경우다.
# ---------------------------------------------------------------------------

ALL_SENTENCES = [
    "지금 2시간 비는데 보드게임 할 사람 있어?",
    "오늘 3시부터 5시까지 비어",
    "내일 저녁에 밥 먹을 사람",
    "6시 전까지 카페",
    "1시간 뒤에 가볍게 풋살",
    "금요일 9시 반에 스터디카페 카공",
    "오전 10시 공부",
    "오후에 카페",
    "아침에 공부",
    "9시부터 11시까지 비어",
    "오늘 밤에 치맥",
    "밤 12시까지",
    "1시까지 밥",
    "오늘 1시까지 밥",
    "12시까지 카페",
    "밤 1시까지 술",
    "오늘 오전에 공부",
    "오늘 점심에 밥",
    "10시부터 1시까지 비어",
    "밤 10시부터 12시까지",
]


@pytest.mark.parametrize("text", ALL_SENTENCES)
def test_window_is_never_inverted(text):
    """검색 구간의 시작은 끝보다 앞이어야 한다.

    이미 지난 시각을 '까지'로 말하거나("1시까지" = 13:00, 지금은 14:00), 오늘의 지난 시간대를
    말하면("오늘 오전에") 시작이 '지금'으로 끌어올려지면서 끝보다 뒤가 된다. 그 결과 어떤 모임도
    맞지 않고, 챗봇의 '이렇게 이해했어요'에는 "오늘 14:00~12:00" 같은 구간이 찍힌다.
    """
    start, end = window(text)
    assert start is not None and end is not None
    assert start < end, f"{start:%m-%d %H:%M} ~ {end:%m-%d %H:%M}"


@pytest.mark.parametrize(
    "text, hours",
    [
        ("10시부터 1시까지 비어", 3),
        ("10시부터 12시까지 공부", 2),
        ("11시부터 1시까지 비어", 2),
    ],
)
def test_range_spoken_in_the_afternoon_keeps_its_length(text, hours):
    """오후 2시에 "10시부터 1시까지"라고 하면 3시간짜리 구간이어야 한다(내일 10~13시든 오늘 밤이든).

    시작은 '오늘 이미 지난 10시 -> 밤 10시'로 바뀌는데 끝은 그대로 오후 1시(13:00)로 남아,
    오늘 22:00 ~ 내일 13:00의 15시간짜리 구간이 만들어진다.
    """
    start, end = window(text)
    assert end - start == timedelta(hours=hours), f"{start:%m-%d %H:%M} ~ {end:%m-%d %H:%M}"


def test_night_ten_to_twelve_ends_at_midnight():
    """'밤 10시부터 12시까지'의 12시는 자정이다. 지금은 다음 날 낮 12시로 읽어 14시간짜리 구간이 된다."""
    start, end = window("밤 10시부터 12시까지")
    assert start == at(12, 22)
    assert end - start <= timedelta(hours=2), f"{start:%m-%d %H:%M} ~ {end:%m-%d %H:%M}"


def test_offset_with_hours_and_minutes():
    """'1시간 30분 뒤'는 90분 뒤 시작이다. 지금은 '30분 뒤'만 잡고 '1시간'을 소요 시간으로 읽는다."""
    slots = slots_of("1시간 30분 뒤에 농구")
    assert slots["start"] == "15:30"
    assert slots["duration_min"] is None


def test_offset_with_ittaga():
    """'한 시간 이따가'는 '한 시간 있다가'와 같은 말이다(시작 15:00).

    OFFSET_RE는 '있다가'만 알아서 '이따가'는 소요 시간 60분(지금~15:00 안에 끝나는 모임)으로 읽는다.
    """
    assert slots_of("한 시간 있다가 카페")["start"] == "15:00"
    slots = slots_of("한 시간 이따가 카페")
    assert slots["start"] == "15:00"
    assert slots["duration_min"] is None


def test_colon_clock_resolves_like_spoken_clock():
    """'3:30'은 '3시 30분'과 같은 시각(15:30)이어야 한다. 지금은 03:30으로 읽어 '지금부터 아무 때나'가 된다."""
    assert slots_of("3시 30분에 카페")["start"] == "15:30"
    assert slots_of("3:30에 카페")["start"] == "15:30"


def test_tilde_range_with_one_si():
    """'3~5시 비어'는 15:00~17:00 빈 시간이다. 지금은 5시만 읽어 '17:00 이후 시작'으로 만든다."""
    slots = slots_of("3~5시 비어")
    assert (slots["start"], slots["end"], slots["strict_end"]) == ("15:00", "17:00", True)


def test_before_without_particle():
    """'5시 전 카페'는 '5시 전에'와 같다(끝 17:00). 지금은 '17:00 이후 시작'으로 반대로 읽는다."""
    assert slots_of("5시 전에 카페")["end"] == "17:00"
    slots = slots_of("5시 전 카페")
    assert (slots["start"], slots["end"], slots["strict_end"]) == (None, "17:00", True)


def test_conditional_ramyeon_is_not_a_meal():
    """'보드게임이라면'의 '라면'은 음식이 아니다."""
    slots = slots_of("보드게임이라면 좋겠어")
    assert slots["categories"] == ["game"]
    assert "라면" not in slots["keywords"]


def test_ojeon_sigan_is_not_an_exhibition():
    """'오전시간'(띄어쓰기 없이)의 '전시'는 문화 종류가 아니다."""
    slots = slots_of("오전시간에 공부")
    assert slots["categories"] == ["study"]
    assert "전시" not in slots["keywords"]


def test_negation_excludes_category():
    """'밥 말고 카페'는 식사를 빼 달라는 말이다(부정 표현 미지원: 둘 다 종류로 잡힌다)."""
    assert slots_of("밥 말고 카페")["categories"] == ["cafe"]


def test_numeric_date_is_not_silently_today():
    """'11월 13일 3시'는 날짜 표현을 지원하지 않아 무시되고, 오늘 15:00 이후로 검색된다."""
    start, _ = window("11월 13일 3시 보드게임")
    assert start is None or start.date() == datetime(2026, 11, 13).date()


@pytest.mark.parametrize("text", ["99999999시간 뒤", "9" * 200 + "시간 뒤", "99999999999분 후", "9" * 250 + "시간 비어"])
def test_huge_numbers_do_not_raise(text):
    """사용자가 친 문장 때문에 예외가 나면 /api/chat 이 500이 된다."""
    slots, _, _ = parse_rules(text, NOW_KST)
    slots_to_conditions(slots, NOW_KST)


@pytest.mark.parametrize(
    "text",
    [
        "시" * 300,
        "3시" * 100,
        "오전 오후 저녁 밤 낮 새벽 " * 10,
        ":::::12:30:::::",
        "0시 0분 0시간 0분 뒤",
        "24시 30분",
        "열한 시 반 열두 시 반 한 시 반",
        "\x00\x01 3시\t\n5시까지",
        "🙂 3시 🙂 5시까지 🙂",
        "１２시",
    ],
)
def test_odd_input_does_not_raise(text):
    slots, _, _ = parse_rules(text, NOW_KST)
    slots_to_conditions(slots, NOW_KST)
