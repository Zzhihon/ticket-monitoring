# -*- coding: utf-8 -*-
"""往返行的推送文字：价格后标「往返」，完整档带回程航班+日期；
超宽时回程先让位，「往返」在任何档都在（价格是往返总价，不能读成单程）。"""
from core.alerter import Alerter, _disp_dw

BASE = {"price": 3100.0, "name": "香港快运航空UO628", "code": "UO628",
        "depTime": "19:55", "arrTime": "00:45", "crossDayDesc": "+1天",
        "_platform": "tongcheng"}


def test_round_trip_line_shows_total_and_return():
    f = dict(BASE, tripType="RT", returnFlight="UO0857", returnDate="20270403")
    line = Alerter._fmt_flight_line(f, idx=1)
    assert line.startswith("1. ￥3100往返 ")
    assert "回UO0857 04/03" in line or _disp_dw(line) <= 40
    assert _disp_dw(line) <= 40


def test_round_trip_marker_survives_width_guard():
    f = dict(BASE, tripType="RT", returnFlight="UO0857", returnDate="20270403",
             name="香港快运航空有限公司超长航司名UO628", stopCity="大阪关西")
    line = Alerter._fmt_flight_line(f, idx=2)
    assert "￥3100往返" in line and _disp_dw(line) <= 40


def test_ctrip_round_trip_without_return_flight_uses_back_date():
    f = dict(BASE, tripType="RT", backDate="2027-04-03", _platform="ctrip",
             name="UO628", depTime="", arrTime="", crossDayDesc="")
    line = Alerter._fmt_flight_line(f, idx=1)
    assert "￥3100往返" in line and "回04/03" in line, line


def test_one_way_line_unchanged():
    line = Alerter._fmt_flight_line(dict(BASE), idx=1)
    assert "往返" not in line and line.startswith("1. ￥3100 ")
