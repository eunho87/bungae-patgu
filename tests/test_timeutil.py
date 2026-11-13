"""timeutil: UTC <-> KST 변환과 화면용 시간 문구. 기준 시각 2026-11-12 05:00 UTC (목 14:00 KST)."""

from datetime import datetime, timedelta, timezone

import pytest

from bungae import timeutil
from bungae.timeutil import day_label, fmt_duration, fmt_relative, fmt_when, from_kst, now_kst, now_utc, to_kst

from .conftest import NOW_KST, NOW_UTC, kst


def test_fixed_now_and_kst_offset():
    assert now_utc() == NOW_UTC
    assert now_kst() == NOW_KST == datetime(2026, 11, 12, 14, 0)
    assert NOW_KST.weekday() == 3  # 목요일
    assert to_kst(datetime(2026, 11, 12, 15, 0)) == datetime(2026, 11, 13, 0, 0)
    assert from_kst(to_kst(NOW_UTC)) == NOW_UTC


def test_real_clock_is_naive_utc_when_not_fixed():
    timeutil.set_fixed_now(None)
    value = now_utc()
    assert value.tzinfo is None
    assert abs(value - datetime.now(timezone.utc).replace(tzinfo=None)) < timedelta(seconds=5)


@pytest.mark.parametrize(
    "when, expected",
    [
        (datetime(2026, 11, 12, 23, 59), "오늘"),
        (datetime(2026, 11, 13, 0, 0), "내일"),
        (datetime(2026, 11, 14, 9, 0), "모레"),
        (datetime(2026, 11, 15, 9, 0), "11/15(일)"),
        (datetime(2026, 11, 16, 9, 0), "11/16(월)"),
        (datetime(2026, 11, 11, 9, 0), "어제"),
        (datetime(2026, 11, 10, 9, 0), "11/10(화)"),
    ],
)
def test_day_label(when, expected):
    assert day_label(when) == expected


def test_fmt_when_uses_kst():
    assert fmt_when(kst(2026, 11, 12, 15, 0), 120) == "오늘 15:00~17:00"
    assert fmt_when(kst(2026, 11, 13, 9, 30)) == "내일 09:30"
    # UTC로는 11/12 16:00 이지만 한국 시간으로는 내일 새벽 1시다.
    assert fmt_when(datetime(2026, 11, 12, 16, 0), 60) == "내일 01:00~02:00"
    assert fmt_when(kst(2026, 11, 12, 23, 0), 120) == "오늘 23:00~01:00"


@pytest.mark.parametrize(
    "delta, expected",
    [
        (timedelta(minutes=-1), ""),
        (timedelta(seconds=0), "곧 시작"),
        (timedelta(seconds=59), "곧 시작"),
        (timedelta(minutes=1), "1분 뒤"),
        (timedelta(minutes=59), "59분 뒤"),
        (timedelta(minutes=60), "1시간 뒤"),
        (timedelta(minutes=90), "1시간 30분 뒤"),
        (timedelta(minutes=179), "2시간 59분 뒤"),
        (timedelta(minutes=200), "3시간 뒤"),
        (timedelta(hours=23, minutes=59), "23시간 뒤"),
        # 하루 넘게 남으면 날짜 표시로 충분하므로 비운다.
        (timedelta(hours=24), ""),
        (timedelta(days=13, hours=23), ""),
    ],
)
def test_fmt_relative(delta, expected):
    assert fmt_relative(NOW_UTC + delta) == expected


@pytest.mark.parametrize("minutes, expected", [(30, "30분"), (60, "1시간"), (90, "1시간 30분"), (240, "4시간")])
def test_fmt_duration(minutes, expected):
    assert fmt_duration(minutes) == expected
