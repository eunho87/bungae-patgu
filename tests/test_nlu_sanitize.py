"""nlu.sanitize_slots: 모델 출력·세션 쿠키처럼 바깥에서 온 값을 걸러낸다."""

import pytest

from bungae.nlu import empty_slots, sanitize_slots

from .conftest import TODAY


def test_valid_slots_pass_through():
    raw = {
        "date": "2026-11-13",
        "start": "15:00",
        "end": "17:00",
        "duration_min": 120,
        "strict_end": True,
        "categories": ["game"],
        "keywords": ["풋살"],
    }
    assert sanitize_slots(raw, TODAY) == raw


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-11-12", "2026-11-12"),  # 오늘
        ("2026-11-26", "2026-11-26"),  # 정확히 14일 뒤
        ("2026-11-27", None),  # 15일 뒤
        ("2026-11-11", None),  # 어제
        ("1999-01-01", None),
        ("2026-02-30", None),
        ("2026-13-01", None),
        ("내일", None),
        ("2026/11/13", None),
        ("", None),
        (20261113, None),
        (None, None),
        (["2026-11-13"], None),
    ],
)
def test_date(value, expected):
    assert sanitize_slots({"date": value}, TODAY)["date"] == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        ("15:00", "15:00"),
        ("9:05", "09:05"),
        (" 09:05 ", "09:05"),
        ("00:00", "00:00"),
        ("23:59", "23:59"),
        ("24:00", "23:59"),
        ("24:01", None),
        ("25:00", None),
        ("12:60", None),
        ("15:00:00", None),
        ("3시", None),
        ("noon", None),
        ("", None),
        (1500, None),
        (15.0, None),
        (["15:00"], None),
        ({"h": 15}, None),
        (True, None),
    ],
)
@pytest.mark.parametrize("key", ["start", "end"])
def test_times(key, value, expected):
    assert sanitize_slots({key: value}, TODAY)[key] == expected


def test_time_aliases_from_model_output():
    slots = sanitize_slots({"start_time": "15:00", "end_time": "17:30"}, TODAY)
    assert (slots["start"], slots["end"]) == ("15:00", "17:30")


@pytest.mark.parametrize(
    "value, expected",
    [
        (120, 120),
        (10, 10),
        (720, 720),
        (90.7, 90),
        (9, None),
        (721, None),
        (0, None),
        (-60, None),
        (True, None),
        (False, None),
        ("120", None),
        (None, None),
        ([120], None),
        (float("nan"), None),
        (float("inf"), None),
        (10**30, None),
    ],
)
def test_duration(value, expected):
    assert sanitize_slots({"duration_min": value}, TODAY)["duration_min"] == expected


@pytest.mark.parametrize("value, expected", [(True, True), (False, False), ("true", False), (1, False), (None, False)])
def test_strict_end_only_literal_true(value, expected):
    assert sanitize_slots({"strict_end": value}, TODAY)["strict_end"] is expected


@pytest.mark.parametrize(
    "value, expected",
    [
        (["game", "hacking"], ["game"]),
        (["drink", "meal", "meal"], ["meal", "drink"]),  # 고정 순서, 중복 없음
        (["GAME"], []),
        ([1, None, ["game"], {"game": 1}], []),
        ("game", []),
        ({"game": True}, []),
        (None, []),
    ],
)
def test_categories(value, expected):
    assert sanitize_slots({"categories": value}, TODAY)["categories"] == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        (["풋살", "#가볍게", " 도서관 "], ["풋살", "가볍게", "도서관"]),
        (["풋살", "풋살"], ["풋살"]),
        (["가" * 20], ["가" * 20]),
        (["가" * 21], []),
        (["", "   ", "#"], []),
        ([1, None, ["x"], {"a": 1}, "ok"], ["ok"]),
        (["a", "b", "c", "d", "e", "f", "g"], ["a", "b", "c", "d", "e"]),
        ("풋살", []),
        ({"풋살": 1}, []),
        (None, []),
    ],
)
def test_keywords(value, expected):
    assert sanitize_slots({"keywords": value}, TODAY)["keywords"] == expected


@pytest.mark.parametrize(
    "garbage",
    [
        None,
        [],
        [{"date": "2026-11-13"}],
        "문자열",
        42,
        3.14,
        True,
        {},
        {"date": {"a": 1}, "start": ["x"], "end": {"y": 2}, "duration_min": "많이", "strict_end": "yes"},
        {"categories": [[["game"]], {"a": [1, 2]}], "keywords": [[["x"]], {"k": [None]}, 3.5, b"bytes"]},
        {"date": None, "start": None, "end": None, "duration_min": None, "categories": None, "keywords": None},
        {"start": 0, "start_time": 0, "end": [], "end_time": {}},
        {"unknown": {"deep": [{"deeper": [1, 2, {"x": None}]}]}, 1: 2, None: "x"},
        {"date": "9" * 5000, "start": ":" * 5000, "keywords": ["가" * 5000] * 50},
    ],
)
def test_never_raises_and_keeps_shape(garbage):
    slots = sanitize_slots(garbage, TODAY)
    assert set(slots) == set(empty_slots())
    assert slots["categories"] == [] and slots["strict_end"] is False
    assert slots["date"] is None and slots["start"] is None and slots["end"] is None
    assert slots["duration_min"] is None
    assert all(isinstance(word, str) and len(word) <= 20 for word in slots["keywords"])
