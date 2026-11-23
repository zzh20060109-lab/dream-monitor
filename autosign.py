"""定时报名调度器（服务端常驻线程）。

抢名额的关键不是「到点开始」，而是「到点之后的第一秒内挤进去」。
所以这里拆成三个阶段：

1) 冷阶段（离开火还早）：只预取报名表单，不做任何提交。
   表单模板是静态字段表，提前拉好能省掉一次 HTTP 往返。
2) 预热（开火前 20 秒）：再刷一次表单 data，顺带把 TCP/TLS 连接热起来，
   避免第一次提交多花一次握手的时间。
3) 开火（提前 FIRE_LEAD 秒 ~ 开始后 HOT_UNTIL 秒）：走 client 的快模式
   （跳过 1.2 s 节流、超时 3 s），每 FAST_GAP 秒提交一次。
   「还没到报名时间」不算失败，立刻再撞——靠反复撞把时钟偏差吃掉。

旧版的问题：每次重试都要先拉表单、再提交，两个请求各吃 1.2 s 节流，
再加 2 s 的重试间隔，报名开始后要 3~5 秒才能报上。现在开火期只有提交一个请求、
间隔 0.18 s，通常开始后 0.2 秒内就能命中。
"""

from __future__ import annotations

import threading
import time
from typing import Callable

WARM = 90.0           # 提前多少秒开始盯这条规则
PRELOAD_AT = 20.0     # 距开火多少秒做「预热刷新」
FIRE_LEAD = 2.0       # 提前多少秒开始尝试（吸收本地与服务器时钟偏差）
HOT_UNTIL = 6.0       # 开始后多少秒保持最高频
GIVE_UP_AFTER = 300.  # 开始后多久仍失败就放弃（秒）

FAST_GAP = 0.15       # 开火窗口内的尝试间隔（秒）
WARM_GAP = 0.8        # 高频窗口之后的节奏
IDLE_NAP = 1.0        # 没有近期待办时的轮询间隔

# 这些错误码/文案表示「还没到点」，要继续重试
RETRY_HINTS = ("不在报名时间", "未开始", "2000166", "报名尚未开始", "还没开始")
# 这些表示「已经不需要再报了」
DONE_HINTS = ("已报名", "已经报过", "2000011", "名额已满", "名额已经满了", "报名已结束", "已结束")
# 这些是提交本身出错（签名/参数/登录态），重试也没用，直接判失败
FATAL_HINTS = ("11001", "11002", "参数错误", "签名", "10005", "10004", "10007", "失效")


class Scheduler:
    def __init__(self, users, hub_of: Callable[[str], object | None],
                 log: Callable[[str], None] | None = None):
        self.users = users
        self.hub_of = hub_of
        self._log = log or (lambda m: None)
        self._stop = threading.Event()
        self._th: threading.Thread | None = None
        self._preloaded: dict[tuple[str, str], float] = {}
        self._last_mark: dict[tuple[str, str], float] = {}
        self.last_tick = 0.0
        self.running = False

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        if self._th and self._th.is_alive():
            return
        self._stop.clear()
        self._th = threading.Thread(target=self._loop, name="autosign", daemon=True)
        self._th.start()
        self.running = True
        self._log("定时报名调度器已启动")

    def stop(self) -> None:
        self._stop.set()
        self.running = False

    def _loop(self) -> None:
        while not self._stop.is_set():
            nap = IDLE_NAP
            try:
                _fired, nap = self.tick()
            except Exception as e:  # noqa: BLE001
                self._log(f"调度器异常：{type(e).__name__}: {e}")
                nap = 2.0
            self._stop.wait(max(0.05, nap))

    # ------------------------------------------------------------------ 预热
    def _preload(self, hub, uid: str, aid: str) -> None:
        try:
            hub.signup_data(aid, ttl=90.0)
            self._preloaded[(uid, aid)] = time.time()
        except Exception as e:  # noqa: BLE001
            self._log(f"预取表单失败：{str(e)[:80]}")

    # ------------------------------------------------------------------ 主逻辑
    def tick(self, now: float | None = None) -> tuple[list[dict], float]:
        """跑一轮。返回 (本轮成功列表, 建议下次唤醒间隔)。"""
        now = time.time() if now is None else now
        self.last_tick = now
        due = self.users.auto_due(now, WARM)
        fired: list[dict] = []
        nap = IDLE_NAP

        for row in due:
            uid, aid = row["uid"], row["aid"]
            start = float(row["join_start"])
            key = (uid, aid)

            if now > start + GIVE_UP_AFTER:
                self.users.auto_mark(uid, aid, "failed", "超过 5 分钟仍未成功，已放弃")
                self._log(f"定时报名放弃：{str(row['name'])[:20]}")
                self._preloaded.pop(key, None)
                continue

            hub = self.hub_of(uid)
            if hub is None:
                # 用户没在本站登录过（或服务重启后 token 还没落到内存），等下一轮
                nap = min(nap, 2.0)
                continue

            left = start - now

            # ---- 阶段 1/2：还没到开火时间，只预取表单
            if left > FIRE_LEAD:
                if left <= PRELOAD_AT:
                    # 预热：进 20 秒圈就刷一次（最多每 15 秒一次），顺便把连接热起来
                    last = self._preloaded.get(key, 0.0)
                    if now - last > 15.0:
                        self._preload(hub, uid, aid)
                elif key not in self._preloaded:
                    self._preload(hub, uid, aid)
                nap = min(nap, max(0.3, min(left - FIRE_LEAD, 10.0)))
                continue

            # ---- 阶段 3：开火
            if key not in self._preloaded:
                self._preload(hub, uid, aid)
            data = hub.signup_data(aid, ttl=90.0)   # 命中缓存，不额外发请求
            try:
                res = hub.signup(aid, data=data, fast=True, quiet=True)
                msg = (res or {}).get("msg") or "已报名"
            except Exception as e:  # noqa: BLE001
                text = str(e)
                low = text.lower()
                if any(h in text for h in DONE_HINTS) or "already" in low:
                    self.users.auto_mark(uid, aid, "failed", text[:120])
                    self._log(f"定时报名跳过：{str(row['name'])[:20]}｜{text[:60]}")
                    self._preloaded.pop(key, None)
                elif any(h in text for h in FATAL_HINTS):
                    self.users.auto_mark(uid, aid, "failed", text[:120])
                    self._log(f"定时报名失败（不可重试）：{str(row['name'])[:20]}｜{text[:70]}")
                    self._preloaded.pop(key, None)
                else:
                    # 「不在报名时间」这类：立刻再来，不退化成慢节奏。
                    # 状态每 3 秒才写一次库，避免 SQLite 写入拖慢开火节奏。
                    gap = FAST_GAP if time.time() < start + HOT_UNTIL else WARM_GAP
                    nap = min(nap, gap)
                    mark_now = time.time()
                    if mark_now - self._last_mark.get(key, 0.0) > 3.0:
                        self.users.auto_mark(uid, aid, "retry", text[:120])
                        self._last_mark[key] = mark_now
                continue

            # 成功
            took = time.time() - start
            note = msg if took <= 0 else f"{msg}｜开始后 {took:.2f} 秒命中"
            self.users.auto_mark(uid, aid, "success", note[:120])
            self._log(f"定时报名成功：{str(row['name'])[:20]}｜{note}")
            self._preloaded.pop(key, None)
            fired.append({"uid": uid, "aid": aid, "ok": True, "msg": note})

        return fired, nap
