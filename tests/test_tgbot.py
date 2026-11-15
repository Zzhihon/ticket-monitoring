# -*- coding: utf-8 -*-
"""Telegram 群指令：/watch 参数解析、查价口径（单程排除往返行、往返
按 backDate 配对）、指令分发与只认配置群。"""
import json
import logging
import sqlite3
from datetime import date, timedelta

import pytest

import core.tgbot as T
from core.storage import PriceStorage

LOG = logging.getLogger("t")
D1 = (date.today() + timedelta(days=30)).isoformat()
D2 = (date.today() + timedelta(days=37)).isoformat()


def test_parse_watch_round_trip_with_budget():
    r = T.parse_watch(["hkg", "nrt", D1, D2, "￥3000"])
    assert r["from"] == "HKG" and r["to"] == "NRT"
    assert r["from_name"] == "中国香港" and r["to_name"] == "东京成田"
    assert r["dates"] == [D1] and r["ret_dates"] == [D2]
    assert r["alert_direct"] == 3000 and r["direct_only"] is True


@pytest.mark.parametrize("args,msg", [
    (["HKG", "NRT"], "格式"),
    (["HKG", "HKG", D1], "不同"),
    (["HKG", "NRT", "2020-01-01"], "早于今天"),
    (["HKG", "NRT", D2, D1], "返程"),
    (["HKG", "NRT", D1, "abc"], "看不懂"),
])
def test_parse_watch_rejects(args, msg):
    with pytest.raises(ValueError, match=msg):
        T.parse_watch(args)


def _db(tmp_path):
    path = str(tmp_path / "p.db")
    PriceStorage(path)   # 用真实建表逻辑，不手抄 schema
    rows = [
        ("ctrip", 1800.0, "CX500", None),
        ("tongcheng", 1500.0, "UO850", json.dumps([{"tripType": "RT", "backDate": D2}])),
        ("ctrip", 1300.0, "HX608", json.dumps([{"tripType": "RT", "backDate": "2099-01-01"}])),
        ("ctrip", 2900.0, "CX504", json.dumps([{"tripType": "RT", "backDate": D2}])),
    ]
    c = sqlite3.connect(path)
    c.executemany(
        "INSERT INTO flight_prices (platform, from_city, to_city, depart_date,"
        " price, flight_no, fetched_at, extra) VALUES (?,?,?,?,?,?,"
        " datetime('now','localtime'), ?)",
        [(p, "HKG", "NRT", D1, pr, f, ex) for p, pr, f, ex in rows])
    c.commit()
    c.close()
    return path


def test_route_lows_trip_semantics(tmp_path):
    db = _db(tmp_path)
    ow = T.route_lows(db, {"from": "HKG", "to": "NRT", "dates": [D1]})
    assert [tuple(x["row"])[:3] for x in ow] == [("ctrip", 1800.0, "CX500")]
    rt = T.route_lows(db, {"from": "HKG", "to": "NRT", "dates": [D1],
                           "ret_dates": [D2]})
    assert tuple(rt[0]["row"])[:3] == ("tongcheng", 1500.0, "UO850")
    none = T.route_lows(db, {"from": "HKG", "to": "HND", "dates": [D1]})
    assert none[0]["row"] is None


class _Bot(T.TelegramBot):
    def __init__(self, routes, db=""):
        super().__init__({"bot_token": "1:a", "chat_id": "-5"}, "http://x",
                         db, LOG, user="hzh")
        self.cfg_users = [{"name": "hzh", "routes": routes}]
        self.saved, self.out = [], []

    def _users(self):
        return json.loads(json.dumps(self.cfg_users))

    def _save(self, users):
        self.saved.append(users)
        self.cfg_users = users

    def reply(self, text):
        self.out.append(text)


def test_watch_unwatch_flow():
    b = _Bot([])
    b.handle("/watch@flying_tiket_monitor_bot HKG NRT %s %s 3000" % (D1, D2))
    assert len(b.saved) == 1 and "已加入第 1 条" in b.out[-1]
    b.handle("/watch HKG NRT %s %s" % (D1, D2))
    assert len(b.saved) == 1 and "已经在监控" in b.out[-1]
    b.handle("/routes")
    assert b.out[-1].startswith("1. 中国香港→东京成田 %s 返 %s" % (D1, D2))
    b.handle("/unwatch 9")
    assert len(b.saved) == 1 and "用法" in b.out[-1]
    b.handle("/unwatch 1")
    assert b.cfg_users[0]["routes"] == [] and "已删除" in b.out[-1]
    b.handle("随便聊聊")
    b.handle("/nope")
    assert len(b.out) == 5


def test_price_marks_budget_hit(tmp_path):
    b = _Bot([{"from": "HKG", "from_name": "香港", "to": "NRT",
               "to_name": "东京成田", "dates": [D1], "ret_dates": [D2],
               "alert_direct": 1600}], db=_db(tmp_path))
    b.handle("/price")
    assert "￥1500 tongcheng UO850" in b.out[-1] and "✅ 已到心理价" in b.out[-1]


def test_start_bot_needs_commands_flag(monkeypatch):
    started = []
    monkeypatch.setattr(T.TelegramBot, "start", lambda self: started.append(self))
    tg = {"enabled": True, "bot_token": "1:a", "chat_id": "-5"}
    cfg = {"users": [{"name": "hzh", "routes": [], "notifier": {"telegram": tg}}]}
    assert T.start_bot(cfg, LOG, 8765, "x.db") is None
    tg["commands"] = True
    assert T.start_bot(cfg, LOG, 8765, "x.db") is started[0]
    assert started[0].web == "http://127.0.0.1:8765"


class _Resp:
    def __init__(self, content, code=200):
        self.status_code, self.text = code, content
        self._c = content

    def json(self):
        return {"choices": [{"message": {"content": self._c}}]}


def test_nl_to_commands_filters_unknown(monkeypatch):
    seen = {}

    def post(url, **kw):
        seen.update(url=url, **kw)
        return _Resp(json.dumps({"commands": [
            "/watch HKG NRT %s %s 3000" % (D1, D2), "/rm -rf", "hello",
            "/price"], "reply": "好的"}))
    monkeypatch.setattr(T.httpx, "post", post)
    out = T.nl_to_commands({"api_key": "k"}, "香港去东京", [])
    assert out["commands"] == ["/watch HKG NRT %s %s 3000" % (D1, D2), "/price"]
    assert seen["url"] == "https://api.deepseek.com/chat/completions"
    assert seen["json"]["model"] == "deepseek-flash"
    assert date.today().isoformat() in seen["json"]["messages"][0]["content"]


def test_nl_falls_back_in_order(monkeypatch):
    tried = []

    def chat(llm, messages):
        tried.append(llm["model"])
        if llm["model"] == "hy4":
            raise RuntimeError("timeout")
        if llm["model"] == "bad":
            return "不是 JSON"
        return json.dumps({"commands": ["/price"], "reply": ""})
    monkeypatch.setattr(T, "chat_once", chat)
    out = T.nl_to_commands([{"model": "hy4"}, {"model": "bad"},
                            {"model": "deepseek-flash"}], "多少钱", [])
    assert tried == ["hy4", "bad", "deepseek-flash"]
    assert out["commands"] == ["/price"] and out["model"] == "deepseek-flash"
    monkeypatch.setattr(T, "chat_once",
                        lambda llm, m: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError, match="模型都没回上"):
        T.nl_to_commands([{"model": "a"}, {"model": "b"}], "x", [])


def test_chat_once_stream_and_headers(monkeypatch):
    seen = {}

    class _S:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_lines(self):
            return iter(['data: {"choices":[{"delta":{"content":"{\\"a\\""}}]}', "",
                         'data: {"choices":[{"delta":{"content":":1}"}}]}',
                         "data: [DONE]", 'data: {"choices":[{"delta":{"content":"x"}}]}'])

    def stream(method, url, **kw):
        seen.update(method=method, url=url, **kw)
        return _S()
    monkeypatch.setattr(T.httpx, "stream", stream)
    txt = T.chat_once({"api_base": "https://gw/v2/", "api_key": "t", "model": "hy4",
                       "stream": True, "headers": {"X-Request-ID": "{uuid}",
                                                   "x-product": "SaaS"}}, [])
    assert txt == '{"a":1}'
    assert seen["url"] == "https://gw/v2/chat/completions"
    assert seen["json"]["stream"] is True
    h = seen["headers"]
    assert h["Authorization"] == "Bearer t" and h["x-product"] == "SaaS"
    assert len(h["X-Request-ID"]) == 32


def test_ask_confirms_mutations_and_runs_reads(monkeypatch):
    b = _Bot([])
    b.llm = [{"api_key": "k"}]
    calls = []
    b._call = lambda m, **kw: calls.append((m, kw))
    b.reply = lambda text, buttons=None: b.out.append((text, buttons))
    monkeypatch.setattr(T, "nl_to_commands", lambda *a, **k: {
        "commands": ["/watch HKG NRT %s %s 3000" % (D1, D2),
                     "/watch HKG HND 2020-01-01", "/routes"],
        "reply": "按 5 天往返算"})
    b.handle("@flying_tiket_monitor_bot 12 月香港飞东京玩 5 天")
    texts = [t for t, _ in b.out]
    assert texts[0] == "按 5 天往返算"
    assert any("没法加" in t and "HND" in t for t in texts)
    assert any("还没有航线" in t for t in texts)          # /routes 直接执行
    confirm = [x for x in b.out if x[1]][-1]
    assert b.saved == []                                  # 确认前不落配置
    pid = confirm[1][0]["callback_data"].split(":")[1]
    b.on_button({"id": "q", "data": "ok:" + pid})
    assert len(b.saved) == 1 and b.cfg_users[0]["routes"][0]["to"] == "NRT"
    b.on_button({"id": "q2", "data": "ok:" + pid})        # 重复点不再执行
    assert len(b.saved) == 1
    assert calls[-1][1]["text"].startswith("已过期")


def test_other_chats_are_logged_and_private_gets_hint(caplog):
    b = _Bot([])
    calls = []
    b._call = lambda m, **kw: calls.append((m, kw))
    with caplog.at_level(logging.INFO):
        b.ignored({"type": "private", "id": 42})
        b.ignored({"type": "supergroup", "id": -1009})
    assert "忽略来自 private 会话 42" in caplog.text
    assert "忽略来自 supergroup 会话 -1009" in caplog.text
    assert [kw["chat_id"] for m, kw in calls] == [42]   # 只回私聊


def test_ask_keeps_context_between_turns(monkeypatch):
    b = _Bot([])
    b.llm = [{"api_key": "k"}]
    b._call = lambda m, **kw: None
    b.reply = lambda text, buttons=None: None
    seen = []

    def fake(llms, text, routes, logger=None, history=None, prices=""):
        seen.append([h["content"] for h in history])
        return {"commands": [], "reply": "回答%d" % len(seen), "model": "m"}
    monkeypatch.setattr(T, "nl_to_commands", fake)
    b.handle("@bot 香港-东京 玩七天")
    b.handle("@bot 樱花季呢")
    assert seen[0] == []
    assert seen[1] == ["香港-东京 玩七天", "回答1"]
    for h in b.history[:2]:                         # 1 小时前的对话不再带
        h["ts"] -= 7200
    b.handle("@bot 第三句")
    assert seen[2] == ["樱花季呢", "回答2"]
