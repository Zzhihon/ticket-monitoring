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



def test_digest_line_intl_codes_and_round_trip_span(tmp_path, monkeypatch):
    """心跳摘要行：国际航线用三字码（中文名一行放不下会被截成「东京」，
    成田/羽田读成同一条）；往返日期写「去-回」（同月回程只写日），
    口径「往返总价」在正文头部说一次。"""
    import logging
    from core.models import Route
    from tests.test_alert_path import _fp, _ps
    monkeypatch.chdir(tmp_path)
    a = Alerter(logging.getLogger("t"), notifier=None, storage=None, digest=True,
                at_mobile="", storm_repeat=1, user="hzh")
    built = []
    for to, to_name in (("NRT", "东京成田"), ("HND", "东京羽田")):
        r = Route(from_code="HKG", from_name="中国香港", to_code=to, to_name=to_name,
                  dates=["2027-03-01"], ret_dates=["2027-03-08"],
                  alert_direct=1000, alert_transfer=0)
        p = _ps([_fp(1729, "香港快运UO628", "19:55", "00:45", cross="+1天",
                     plat="tongcheng", dep="2027-03-01")], "HKG", to, "2027-03-01")
        built.append((r, a._build_sections(r, p)))
    d = a._digest_payload(built, fresh=True, with_tables=False, with_charts=False)["desp"]
    assert "> 价格为每人含税往返总价" in d, d
    assert "> HKG→NRT 03/01-08 ｜ 直飞 ￥1729" in d, d
    assert "> HKG→HND 03/01-08 ｜ 直飞 ￥1729" in d, d
