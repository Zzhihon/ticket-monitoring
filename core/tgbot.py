# -*- coding: utf-8 -*-
"""Telegram 群指令：在群里查价、增删航线、立即扫描。

长轮询 getUpdates（守护线程，随监控常驻）；只认 notifier.telegram.chat_id
那一个群的消息，其他会话一律忽略。改配置不另开写入口：读写都走本机
控制台的 /api/config（同一把保存锁、同一套校验、保存即热重载）——
两个写入口会在「谁最后写」上打架（webui /api/config 处的既有纪律）。
查价只读 prices.db。

开关：notifier.telegram.commands: true（默认关，不开就和没有这个模块一样）。

自然语言（可选，notifier.telegram.llm，一个或一串模型，按顺序兜底）：@bot
说一句话，由 OpenAI 兼容接口（CodeBuddy hy4、DeepSeek 等）把它翻译成上面的指令，再交给同一套解析与校验——模型
只产出指令文本，不直接碰配置。会改东西的指令（/watch /unwatch /scan）
先回一条带「确认/取消」按钮的消息，点了确认才执行。
"""
import logging
import os
import re
import sqlite3
import json
import threading
import time
import uuid
from datetime import date

import httpx

from core.intl import city_name_of

_HELP = (
    "可用指令：\n"
    "/price — 各航线 24 小时内最低价\n"
    "/routes — 列出监控中的航线\n"
    "/watch HKG NRT 2026-12-20 [2026-12-27] [3000] — 加航线"
    "（第二个日期=返程，末尾数字=每人含税心理价；默认只盯直飞）\n"
    "/unwatch 2 — 删第 2 条航线\n"
    "/scan — 立即扫描一轮\n"
    "也可以直接 @我 说话，如「12 月 20 号香港飞东京，玩 5 天，3000 以内」")
_COMMANDS = [("price", "各航线最低价"), ("routes", "监控中的航线"),
             ("watch", "加航线：/watch HKG NRT 2026-12-20 [返程] [心理价]"),
             ("unwatch", "删航线：/unwatch 序号"), ("scan", "立即扫描一轮"),
             ("help", "指令说明")]
_MUTATING = {"watch", "unwatch", "scan"}
_READONLY = {"price", "routes", "help"}
_NL_PROMPT = """你是机票监控 bot 的指令翻译器。把用户的话翻译成下列指令，只输出 JSON 对象：
{"commands": ["/指令 参数", ...], "reply": "给用户的一句中文说明"}
可用指令：
/watch 出发三字码 到达三字码 去程YYYY-MM-DD [返程YYYY-MM-DD] [每人含税心理价数字]
/unwatch 序号
/price
/routes
/scan
规则：
- 今天是 {today}。相对日期按今天换算；日期说不清（如「12月下旬」没说哪天）就不要猜，commands 留空，在 reply 里问清楚。
- 东京有成田 NRT 和羽田 HND 两个机场：用户没指定就各出一条 /watch。大阪用 KIX。其他城市用主机场三字码。
- 「往返」「X 天」要换算出返程日期；只说单程就不带返程。
- 心理价是人民币每人含税总价；用户没说就不写这一项。
- 删除航线要用下面列表里的序号；对不上就不出指令，在 reply 里说明。
- 和机票监控无关的话：commands 留空，reply 简短说明你能做什么。
- reply 只说「将要做什么」，不要说已经完成：/watch /unwatch /scan 要用户点确认按钮后才执行。
当前监控中的航线：
{routes}"""
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_IATA = re.compile(r"^[A-Za-z]{3}$")


def _valid_date(s: str) -> bool:
    if not _DATE.match(s):
        return False
    try:
        return date.fromisoformat(s) >= date.today()
    except ValueError:
        return False


def parse_watch(args: list) -> dict:
    """/watch 参数 → 航线 dict（config.yaml routes 同形）；不合法抛 ValueError
    （原话回给群里，告诉用户哪一项不对）。"""
    if len(args) < 3:
        raise ValueError("格式：/watch 出发 到达 去程日期 [返程日期] [心理价]")
    frm, to, dep = args[0].upper(), args[1].upper(), args[2]
    if not (_IATA.match(frm) and _IATA.match(to)) or frm == to:
        raise ValueError("出发/到达要填不同的三字码，如 HKG NRT")
    if not _valid_date(dep):
        raise ValueError("去程日期要写成 YYYY-MM-DD，且不能早于今天")
    ret, budget = "", 0.0
    for a in args[3:]:
        if _DATE.match(a):
            if not _valid_date(a) or a < dep:
                raise ValueError("返程日期不能早于去程日期")
            ret = a
        else:
            try:
                budget = float(a.lstrip("¥￥"))
            except ValueError:
                raise ValueError("看不懂「%s」：只接受日期或心理价数字" % a)
            if budget <= 0:
                raise ValueError("心理价要大于 0")
    route = {"from": frm, "from_name": city_name_of(frm) or frm,
             "to": to, "to_name": city_name_of(to) or to,
             "dates": [dep], "alert_direct": budget, "alert_transfer": 0,
             "direct_only": True}
    if ret:
        route["ret_dates"] = [ret]
    return route


def chat_once(llm: dict, messages: list) -> str:
    """调一次 OpenAI 兼容 chat/completions，返回 content 文本。
    llm 键：api_base / api_key / model / headers（额外请求头，值里的 {uuid}
    每次请求换新值）/ stream（网关只收流式时开，如 CodeBuddy 的 hy4）/
    timeout（秒，默认 30）/ json_mode（默认开 response_format=json_object）。"""
    url = "%s/chat/completions" % str(llm.get("api_base")
                                      or "https://api.deepseek.com").rstrip("/")
    rid = uuid.uuid4().hex
    headers = {str(k): str(v).replace("{uuid}", rid)
               for k, v in (llm.get("headers") or {}).items()}
    headers["Authorization"] = "Bearer %s" % llm.get("api_key", "")
    body = {"model": llm.get("model") or "deepseek-flash", "messages": messages}
    if llm.get("json_mode", True):
        body["response_format"] = {"type": "json_object"}
    timeout = float(llm.get("timeout") or 30)
    if not llm.get("stream"):
        r = httpx.post(url, headers=headers, json=body, timeout=timeout,
                       trust_env=False)
        if r.status_code != 200:
            raise RuntimeError("HTTP %s %s" % (r.status_code, r.text[:120]))
        return r.json()["choices"][0]["message"]["content"] or ""
    body["stream"] = True
    parts = []
    with httpx.stream("POST", url, headers=headers, json=body,
                      timeout=timeout, trust_env=False) as r:
        if r.status_code != 200:
            raise RuntimeError("HTTP %s %s" % (r.status_code,
                                               r.read().decode()[:120]))
        for line in r.iter_lines():
            data = line[5:].strip() if line.startswith("data:") else ""
            if not data:
                continue
            if data == "[DONE]":
                break
            for c in json.loads(data).get("choices") or []:
                parts.append((c.get("delta") or {}).get("content") or "")
    return "".join(parts)


def nl_to_commands(llms, text: str, routes: list, logger=None) -> dict:
    """一句话 → {"commands": [...], "reply": "..."}。llms 是一个或一串
    模型配置，按顺序试，前一个出错/超时/回的不是 JSON 就换下一个（免费
    模型在前、付费兜底在后）；全挂才抛。只认 / 开头且属于已知指令的
    条目，模型多给的东西丢掉。"""
    listing = "\n".join("%d. %s" % (i, route_label(r))
                         for i, r in enumerate(routes, 1)) or "（无）"
    prompt = (_NL_PROMPT.replace("{today}", date.today().isoformat())
              .replace("{routes}", listing))
    messages = [{"role": "system", "content": prompt},
                {"role": "user", "content": text}]
    errs = []
    for llm in (llms if isinstance(llms, list) else [llms]):
        name = llm.get("model") or "deepseek-flash"
        try:
            out = json.loads(chat_once(llm, messages) or "{}")
            if not isinstance(out, dict):
                raise ValueError("回的不是 JSON 对象")
        except Exception as e:
            errs.append("%s：%s" % (name, str(e)[:80]))
            if logger:
                logger.warning("[TG指令] 模型 %s 失败，换下一个: %s", name, e)
            continue
        cmds = [c.strip() for c in (out.get("commands") or [])
                if isinstance(c, str) and c.strip().startswith("/")
                and c.strip()[1:].split()[0].lower() in _MUTATING | _READONLY]
        return {"commands": cmds, "reply": str(out.get("reply") or "").strip(),
                "model": name}
    raise RuntimeError("模型都没回上：" + "；".join(errs))


def route_label(r: dict) -> str:
    ret = (r.get("ret_dates") or [r.get("ret_date")] or [""])[0] or ""
    lab = "%s→%s %s" % (r.get("from_name") or r.get("from"),
                        r.get("to_name") or r.get("to"),
                        ",".join(r.get("dates") or []))
    if ret:
        lab += " 返 %s" % ret
    if r.get("alert_direct"):
        lab += " · 心理价 ￥%.0f" % float(r["alert_direct"])
    return lab


def route_lows(db_path: str, r: dict, hours: int = 24) -> list:
    """一条航线各日期 24h 内的最低价（每个出发日×返程日一行）。
    往返只认 extra 里 tripType=RT 且 backDate 对上的行，单程排除往返行
    （storage.latest_prices 同一口径）。"""
    rets = [x for x in (r.get("ret_dates") or [r.get("ret_date")]) if x]
    rt_sql = ("json_valid(extra) AND json_type(extra)='array' AND "
              "EXISTS (SELECT 1 FROM json_each(extra) je WHERE "
              "json_extract(je.value,'$.tripType')='RT' AND "
              "json_extract(je.value,'$.backDate')=?)")
    ow_sql = ("NOT (json_valid(extra) AND json_type(extra)='array' AND "
              "EXISTS (SELECT 1 FROM json_each(extra) je WHERE "
              "json_extract(je.value,'$.tripType')='RT'))")
    out = []
    try:
        c = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=5)
    except sqlite3.Error:
        return out
    try:
        for dep in r.get("dates") or []:
            for ret in (rets or [""]):
                trip = (rt_sql if ret else ow_sql)
                args = [r.get("from"), r.get("to"), dep,
                        "-%d hours" % hours] + ([ret] if ret else [])
                row = c.execute(
                    "SELECT platform, price, flight_no, fetched_at "
                    "FROM flight_prices WHERE from_city=? AND to_city=? "
                    "AND depart_date=? AND price>0 "
                    "AND fetched_at >= datetime('now','localtime',?) "
                    "AND " + trip + " ORDER BY price ASC LIMIT 1",
                    args).fetchone()
                out.append({"dep": dep, "ret": ret, "row": row})
    except sqlite3.Error:
        pass
    finally:
        c.close()
    return out


class TelegramBot(threading.Thread):
    def __init__(self, tg: dict, web_base: str, db_path: str,
                 logger: logging.Logger, user: str = "",
                 offset_path: str = os.path.join("data", ".tg_offset")):
        super().__init__(name="tgbot", daemon=True)
        self.token = str(tg.get("bot_token") or "").strip()
        self.chat_id = str(tg.get("chat_id") or "").strip()
        self.api = "%s/bot%s" % ((str(tg.get("api_base") or "").strip()
                                  or "https://api.telegram.org").rstrip("/"),
                                 self.token)
        self.web = web_base.rstrip("/")
        self.db_path = db_path
        self.logger = logger
        self.user = user
        self.offset_path = offset_path
        llm = tg.get("llm")
        llm = llm if isinstance(llm, list) else [llm]
        self.llm = [x for x in llm if isinstance(x, dict) and x.get("api_key")] \
            or None
        self.pending = {}   # 确认按钮 id → 待执行指令（进程内，重启即作废）
        # 长轮询 50s：读超时要比它长；trust_env=False 直连律，代理只认显式 proxy
        self.http = httpx.Client(proxy=str(tg.get("proxy") or "").strip()
                                 or None, timeout=httpx.Timeout(65, connect=15),
                                 trust_env=False)

    # ---- Telegram ----
    def _call(self, method: str, **payload):
        r = self.http.post("%s/%s" % (self.api, method), json=payload)
        d = r.json()
        if not d.get("ok"):
            raise RuntimeError("%s: %s" % (method, d.get("description")))
        return d.get("result")

    def reply(self, text: str, buttons: list = None):
        try:
            extra = ({"reply_markup": {"inline_keyboard": [buttons]}}
                     if buttons else {})
            self._call("sendMessage", chat_id=self.chat_id, text=text[:4000],
                       disable_web_page_preview=True, **extra)
        except Exception as e:
            self.logger.warning("[TG指令] 回复失败: %s", e)

    def _offset(self) -> int:
        try:
            with open(self.offset_path, encoding="utf-8") as f:
                return int(f.read().strip() or 0)
        except (OSError, ValueError):
            return 0

    def _save_offset(self, n: int):
        # 落盘：重启后不再重放已处理的指令（/watch 重放=重复加航线）
        try:
            os.makedirs(os.path.dirname(self.offset_path) or ".", exist_ok=True)
            with open(self.offset_path, "w", encoding="utf-8") as f:
                f.write(str(n))
        except OSError:
            pass

    def run(self):
        try:
            self._call("setMyCommands",
                       commands=[{"command": c, "description": d}
                                 for c, d in _COMMANDS])
        except Exception as e:
            self.logger.warning("[TG指令] 注册指令菜单失败: %s", e)
        offset = self._offset()
        self.logger.info("[TG指令] 已开始监听群 %s", self.chat_id)
        while True:
            try:
                ups = self._call("getUpdates", offset=offset, timeout=50,
                                 allowed_updates=["message", "callback_query"])
            except Exception as e:
                self.logger.warning("[TG指令] 拉取失败，30 秒后重试: %s", e)
                time.sleep(30)
                continue
            for u in ups or []:
                offset = int(u["update_id"]) + 1
                self._save_offset(offset)
                cq = u.get("callback_query")
                msg = (cq or {}).get("message") or u.get("message") or {}
                if str((msg.get("chat") or {}).get("id")) != self.chat_id:
                    continue
                try:
                    if cq:
                        self.on_button(cq)
                    else:
                        self.handle(msg.get("text") or "")
                except Exception as e:
                    self.logger.warning("[TG指令] 处理失败: %s", e)
                    self.reply("处理失败：%s" % str(e)[:200])

    # ---- 指令 ----
    def handle(self, text: str):
        parts = text.strip().split()
        if not parts:
            return
        if not parts[0].startswith("/"):
            # 群隐私模式下只收得到 @bot 和回复 bot 的消息：都当自然语言
            if self.llm:
                self.ask(re.sub(r"@\w+", "", text).strip())
            return
        cmd, args = parts[0][1:].split("@")[0].lower(), parts[1:]
        fn = getattr(self, "cmd_" + cmd, None)
        if fn is None:
            if cmd == "start":
                self.reply(_HELP)
            return
        fn(args)

    def ask(self, text: str):
        if not text:
            return
        routes = self._me(self._users()).get("routes") or []
        out = nl_to_commands(self.llm, text, routes, logger=self.logger)
        cmds = out["commands"]
        if out["reply"]:
            self.reply(out["reply"])
        todo = []
        for c in cmds:
            name, args = c[1:].split()[0].lower(), c.split()[1:]
            if name in _READONLY:
                self.handle(c)
                continue
            if name == "watch":
                try:
                    parse_watch(args)   # 先校验：不合法的不给确认按钮
                except ValueError as e:
                    self.reply("没法加「%s」：%s" % (c, e))
                    continue
            todo.append(c)
        if todo:
            pid = uuid.uuid4().hex[:10]
            self.pending[pid] = todo
            self.reply("要执行：\n" + "\n".join(todo),
                       buttons=[{"text": "确认", "callback_data": "ok:" + pid},
                                {"text": "取消", "callback_data": "no:" + pid}])

    def on_button(self, cq: dict):
        act, _, pid = str(cq.get("data") or "").partition(":")
        todo = self.pending.pop(pid, None)
        try:
            self._call("answerCallbackQuery", callback_query_id=cq.get("id"),
                       text=("已取消" if act == "no" else
                             "已过期，请重新说一遍" if todo is None else "执行中"))
        except Exception:
            pass
        if act == "ok" and todo:
            for c in todo:
                self.handle(c)

    def cmd_help(self, args):
        self.reply(_HELP)

    def _users(self) -> list:
        r = httpx.get(self.web + "/api/config", timeout=15, trust_env=False)
        r.raise_for_status()
        return r.json().get("users") or []

    def _me(self, users: list) -> dict:
        for u in users:
            if not self.user or u.get("name") == self.user:
                return u
        raise RuntimeError("配置里没有用户 %s" % self.user)

    def _save(self, users: list):
        r = httpx.post(self.web + "/api/config", json={"users": users},
                       timeout=30, trust_env=False)
        d = r.json()
        if r.status_code != 200 or not d.get("ok"):
            raise RuntimeError(d.get("err") or "HTTP %s" % r.status_code)

    def cmd_routes(self, args):
        routes = self._me(self._users()).get("routes") or []
        if not routes:
            self.reply("还没有航线。用 /watch HKG NRT 2026-12-20 2026-12-27 3000 加一条")
            return
        self.reply("\n".join("%d. %s" % (i, route_label(r))
                             for i, r in enumerate(routes, 1)))

    def cmd_price(self, args):
        routes = self._me(self._users()).get("routes") or []
        if not routes:
            self.reply("还没有航线，先 /watch 加一条")
            return
        lines = []
        for i, r in enumerate(routes, 1):
            lines.append("%d. %s" % (i, route_label(r)))
            for x in route_lows(self.db_path, r):
                tag = "%s%s" % (x["dep"], " 返 " + x["ret"] if x["ret"] else "")
                row = x["row"]
                if not row:
                    lines.append("   %s：24 小时内还没有价格" % tag)
                    continue
                plat, price, fno, at = row
                hit = (" ✅ 已到心理价" if r.get("alert_direct")
                       and price <= float(r["alert_direct"]) else "")
                lines.append("   %s：￥%.0f %s %s（%s 采集）%s" % (
                    tag, price, plat, fno or "", str(at)[5:16], hit))
        lines.append("价格=渠道含税总价%s；以原平台下单页为准" %
                     ("（往返为往返总价）" if any(
                         r.get("ret_dates") for r in routes) else ""))
        self.reply("\n".join(lines))

    def cmd_watch(self, args):
        route = parse_watch(args)
        users = self._users()
        me = self._me(users)
        routes = me.setdefault("routes", [])
        key = lambda r: (r.get("from"), r.get("to"), tuple(r.get("dates") or []),
                         tuple(r.get("ret_dates") or []))
        if any(key(r) == key(route) for r in routes):
            self.reply("这条已经在监控了：%s" % route_label(route))
            return
        routes.append(route)
        self._save(users)
        self.reply("已加入第 %d 条：%s\n只盯直飞；下一轮扫描开始采价，/scan 可立即扫"
                   % (len(routes), route_label(route)))

    def cmd_unwatch(self, args):
        users = self._users()
        routes = self._me(users).get("routes") or []
        try:
            n = int(args[0])
            assert 1 <= n <= len(routes)
        except (IndexError, ValueError, AssertionError):
            self.reply("用法：/unwatch 序号（/routes 查序号，共 %d 条）" % len(routes))
            return
        gone = routes.pop(n - 1)
        self._save(users)
        self.reply("已删除：%s" % route_label(gone))

    def cmd_scan(self, args):
        # /api/run 同步跑完整轮才回执（分钟级）：另起线程等，不堵指令轮询
        def _run():
            try:
                r = httpx.post(self.web + "/api/run", json={}, timeout=3600,
                               trust_env=False)
                d = r.json()
                self.reply("本轮扫描完成" if d.get("ok")
                           else "扫描没跑：%s" % d.get("err"))
            except Exception as e:
                self.reply("扫描失败：%s" % str(e)[:200])
        threading.Thread(target=_run, name="tgbot-scan", daemon=True).start()
        self.reply("已开始扫描，跑完会在这里说一声；达标照常推送")


def start_bot(cfg: dict, logger: logging.Logger, web_port: int, db_path: str):
    """找第一个开了 telegram.commands 的用户起监听线程；没有就什么都不做。
    读 config 原始 users（build_users 会跳过还没航线的用户，而空航线
    正是最需要 /watch 的时候）。"""
    for u in cfg.get("users") or []:
        tg = ((u.get("notifier") or {}).get("telegram") or {})
        if (tg.get("enabled") and tg.get("commands") and tg.get("bot_token")
                and tg.get("chat_id")):
            bot = TelegramBot(tg, "http://127.0.0.1:%d" % web_port, db_path,
                              logger, user=u.get("name", ""))
            bot.start()
            return bot
    return None
