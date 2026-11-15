# -*- coding: utf-8 -*-
"""Telegram Bot 通道：主推送并联成员（心跳/日报/达标都推）。
渲染（markdown → Telegram HTML 子集）、回环死链过滤、整行截断、
HTML 被拒降级纯文本、装配门。"""
import logging as _lg
from unittest import mock

import core.notifier as N

LOG = _lg.getLogger("t")


def _mk(monkeypatch, replies):
    calls = []
    it = iter(replies)

    def _fake_post(url, **kw):
        calls.append((url, kw))
        code, body = next(it)
        return mock.Mock(status_code=code, text=str(body),
                         json=lambda: body)
    monkeypatch.setattr(N.httpx, "post", _fake_post)
    return N.TelegramNotifier("123:abc", "-1001", LOG,
                              proxy="socks5://127.0.0.1:1080"), calls


def test_send_ok_html(monkeypatch):
    tg, calls = _mk(monkeypatch, [(200, {"ok": True}), (200, {"ok": True})])
    desp = ("#### 🎯 达标 HKG→NRT\n\n> **￥1890** <含税>\n"
            "[打开携程](https://flights.ctrip.com/x?a=1&b=2)\n"
            "![走势](https://img.example/a.png)\n"
            "[控制台](http://127.0.0.1:8765/)")
    assert tg.send("达标", desp, launch="http://mac-air:8765/d/1") is True
    url, kw = calls[0]
    assert url == "https://api.telegram.org/bot123:abc/sendMessage"
    assert kw["proxy"] == "socks5://127.0.0.1:1080"
    assert kw["trust_env"] is False
    p = kw["json"]
    assert p["chat_id"] == "-1001" and p["parse_mode"] == "HTML"
    t = p["text"]
    assert t.startswith("<b>达标</b>\n<b>🎯 达标 HKG→NRT</b>")
    assert "<b>￥1890</b> &lt;含税&gt;" in t
    assert '<a href="https://flights.ctrip.com/x?a=1&amp;b=2">打开携程</a>' in t
    assert "img.example" not in t and "走势" not in t      # 图不进正文
    photo = calls[1]
    assert photo[0].endswith("/sendPhoto")
    assert photo[1]["json"]["photo"] == "https://img.example/a.png"
    assert photo[1]["json"]["caption"] == "走势"
    assert "127.0.0.1" not in t and "控制台" in t   # 回环死链剥壳留字
    assert t.endswith('<a href="http://mac-air:8765/d/1">打开详情</a>')
    assert tg._fail_streak == 0


def test_truncate_whole_lines():
    desp = "\n".join("第%d行 ￥%d" % (i, 1000 + i) for i in range(2000))
    t = N._tg_html("心跳", desp)
    assert len(t) <= 3900 and N._TG_NOTE in t
    body = t.split("\n")[1:-1]
    assert all(ln.startswith("第") and ln.split("￥")[1].isdigit()
               for ln in body)


def test_parse_error_falls_back_to_plain(monkeypatch):
    tg, calls = _mk(monkeypatch, [
        (400, {"ok": False, "description": "Bad Request: can't parse entities"}),
        (200, {"ok": True})])
    assert tg.send("达标", "**a** <b>") is True
    assert len(calls) == 2
    p2 = calls[1][1]["json"]
    assert "parse_mode" not in p2 and p2["text"] == "达标\na <b>"


def test_failure_counts_streak(monkeypatch):
    tg, _ = _mk(monkeypatch, [(401, {"ok": False})])
    assert tg.send("x", "y") is False
    assert tg._fail_streak == 1 and tg.last_err.startswith("HTTP 401")


def test_build_notifier_wiring():
    on = {"enabled": True, "bot_token": "1:a", "chat_id": -1001}
    n = N.build_notifier({"telegram": on}, LOG)
    assert isinstance(n, N.TelegramNotifier) and n.chat_id == "-1001"
    n = N.build_notifier({"dingtalk": {"enabled": True, "webhook": "https://x"},
                          "telegram": on}, LOG)
    assert isinstance(n, N.CompositeNotifier)
    assert [type(m).__name__ for m in n.members] == [
        "DingTalkNotifier", "TelegramNotifier"]
    assert N.build_notifier({"telegram": dict(on, enabled=False)}, LOG) is None
    assert N.build_notifier({"telegram": dict(on, chat_id="")}, LOG) is None
    assert N.build_notifier({"telegram": "bad"}, LOG) is None


def test_local_chart_goes_as_photo_bytes(monkeypatch, tmp_path):
    import report
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    png = tmp_path / "data" / "flights_table_hzh_HKGNRT.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    url = report.upload_chart(str(png), None, LOG, ih={"provider": "local"})
    assert url.startswith("local://charts/") and url.endswith(".png")
    tg, calls = _mk(monkeypatch, [(200, {"ok": True}), (200, {"ok": True})])
    assert tg.send("心跳", "#### 📋 明细总表\n\n![明细总表](%s)\n" % url) is True
    assert "local://" not in calls[0][1]["json"]["text"]
    sent = calls[1][1]
    assert calls[1][0].endswith("/sendPhoto") and sent["data"]["caption"] == "明细总表"
    assert sent["files"]["photo"][1] == png.read_bytes()


def test_photo_failure_does_not_fail_push(monkeypatch):
    tg, calls = _mk(monkeypatch, [(200, {"ok": True}), (400, {"ok": False})])
    assert tg.send("x", "![图](https://img.example/b.png)") is True
    assert tg._fail_streak == 0 and len(calls) == 2
