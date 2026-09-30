# -*- coding: utf-8 -*-
"""Hibachi 场所适配层（基于 hibachi_xyz SDK 的同步封装）。

要点（都是实测踩过的坑，别删）:
  - 撤单/超时后必须查成交记录增量：部分成交可能藏在已撤销的订单上，
    只看 open_orders 会漏（touch_fill 内已实现）
  - side 判定做防御式归一（BID/BUY/1/LONG -> 买）
  - taker 市价单带 max_fees 保护顶（taker 费+buffer），防极端滑点
  - 挂单用 post-only（maker），想吃单就走 market

.env 键（Hibachi 侧）:
  HIBACHI_ACCOUNT_ID   数字账户 id（网页端账户页可见）
  HIBACHI_API_KEY      API key 名
  HIBACHI_PRIVATE_KEY  0x 开头私钥
  HIBACHI_SYMBOL       默认市场，如 BTC/USDT-P
"""
import os
import threading
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional, Tuple

from hibachi_xyz import HibachiApiClient, OrderFlags, Side


def load_env(path: str) -> Dict[str, str]:
    e: Dict[str, str] = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                e[k.strip()] = v.strip().strip('"').strip("'")
    return e


def _side_buy(side_val) -> bool:
    """side 值防御式判买（兼容 Side enum / 'BUY' / 'BID' / 'Long' / 1）。"""
    if side_val is None:
        return False
    v = getattr(side_val, "value", None) or str(side_val)
    return str(v).upper() in ("BUY", "BID", "1", "LONG")


class HibachiVenue:
    MAX_FEES = 0.0005   # taker 费 + buffer（保护顶）

    def __init__(self, env: Dict[str, str]):
        self._account_id = int(env.get("HIBACHI_ACCOUNT_ID") or 0)
        self._symbol = env.get("HIBACHI_SYMBOL") or "BTC/USDT-P"
        self._hc = HibachiApiClient(
            api_key=env.get("HIBACHI_API_KEY"),
            account_id=self._account_id,
            private_key=env.get("HIBACHI_PRIVATE_KEY"),
        )
        self._tick_cache: Dict[str, float] = {}

    @property
    def hc(self) -> HibachiApiClient:
        return self._hc

    # ── 行情 ──
    def tick(self, symbol: str) -> float:
        if symbol not in self._tick_cache:
            self._tick_cache[symbol] = float(self._hc.get_tick_size(symbol))
        return self._tick_cache[symbol]

    def bbo(self, symbol: str) -> Tuple[Decimal, Decimal]:
        ob = self._hc.get_orderbook(symbol, depth=1, granularity=self.tick(symbol))
        bid = Decimal(str((getattr(ob, "bid", None) or [{}])[0].price if getattr(ob, "bid", None) else 0))
        ask = Decimal(str((getattr(ob, "ask", None) or [{}])[0].price if getattr(ob, "ask", None) else 0))
        return bid, ask

    # ── 账户 ──
    def balance(self) -> Decimal:
        return Decimal(str(self._hc.get_account_info().balance))

    def position(self, symbol: str) -> Decimal:
        """带符号净持仓（多 + / 空 -）。"""
        ai = self._hc.get_account_info()
        for p in (getattr(ai, "positions", None) or []):
            if getattr(p, "symbol", None) == symbol:
                q = abs(Decimal(str(getattr(p, "quantity", "0"))))
                if q == 0:
                    return Decimal(0)
                d = str(getattr(p, "direction", "") or "")
                return q if (d.lower().startswith("long") or d.upper() in ("LONG", "BUY")) else -q
        return Decimal(0)

    def _n_trades(self) -> int:
        resp = self._hc.get_account_trades()
        return len(getattr(resp, "trades", None) or [])

    def qty_for_notional(self, symbol: str, notional: Decimal) -> Decimal:
        bid, ask = self.bbo(symbol)
        mid = (bid + ask) / 2 if bid and ask else Decimal("1")
        return notional / mid

    # ── 下单 ──
    def market_order(self, symbol: str, side: str, qty: Decimal,
                     reduce_only: bool = False, live: bool = True):
        sd = Side.BID if side == "BUY" else Side.ASK
        flags = OrderFlags.ReduceOnly if reduce_only else None
        if not live:
            print("    [dry] market %s %s %s reduce_only=%s" % (symbol, side, qty, reduce_only))
            return None
        return self._hc.place_market_order(symbol, qty, sd, self.MAX_FEES,
                                           order_flags=flags)

    def place_postonly(self, symbol: str, side: str, price: Decimal, qty: Decimal,
                       live: bool = True):
        sd = Side.BID if side == "BUY" else Side.ASK
        if not live:
            print("    [dry] postonly %s %s @%s x%s" % (symbol, side, price, qty))
            return None
        return self._hc.place_limit_order(symbol, qty, price, sd, self.MAX_FEES,
                                          order_flags=OrderFlags.PostOnly)

    def cancel_order(self, order_id) -> Any:
        return self._hc.cancel_order(order_id=order_id)

    def cancel_all(self, symbol: Optional[str] = None) -> int:
        """撤全部挂单（可按 symbol 过滤），返回撤掉的单数。"""
        n = 0
        po = self._hc.get_pending_orders()
        for o in (getattr(po, "orders", None) or []):
            if symbol and getattr(o, "symbol", None) != symbol:
                continue
            try:
                self.cancel_order(o.orderId)
                n += 1
            except Exception:
                pass
        return n

    def flatten_all(self, reason: str = "flatten"):
        """把所有非零仓位市价平到零（reduce-only），收尾安全网。"""
        ai = self._hc.get_account_info()
        for p in (getattr(ai, "positions", None) or []):
            q = abs(Decimal(str(getattr(p, "quantity", "0"))))
            if q == 0:
                continue
            d = str(getattr(p, "direction", "") or "")
            side = "SELL" if (d.lower().startswith("long") or d.upper() in ("LONG", "BUY")) else "BUY"
            try:
                self.market_order(p.symbol, side, q, reduce_only=True)
                print("    [flatten] %s %s %s (%s)" % (p.symbol, side, q, reason))
            except Exception as e:
                print("    [flatten FAIL] %s %s -> 人工处置: %r" % (p.symbol, q, e))

    # ── touch 挂单（等成交 + 偏离重挂 + 超时撤，含撤单查量差铁律）──
    def touch_fill(self, symbol: str, side: str, qty: Decimal,
                   offset_ticks: int, requote_bp: float, timeout_s: float,
                   live: bool = True) -> Optional[Tuple[Decimal, Decimal]]:
        """挂 post-only 贴盘口价（可加 offset_ticks 深度），等成交。

        - 盘口偏离超过 requote_bp -> 撤单重挂（盘口是移动的，挂价要跟着走）
        - 超过 timeout_s -> 撤单返回 None（本轮放弃）
        - 成交判定 = 成交记录数增加（撤单后也查——部分成交可能藏在已撤订单上）
        返回 (成交价, 成交量) 或 None。价格按 tick 对齐。
        """
        t0 = time.time()
        tk = Decimal(str(self.tick(symbol)))
        placed_px: Optional[Decimal] = None
        placed_oid = None
        n_rq = 0
        trades_before = self._n_trades()
        while time.time() - t0 < timeout_s:
            bid, ask = self.bbo(symbol)
            ref = bid if side == "BUY" else ask
            if placed_px is None:
                px = ref - tk * offset_ticks if side == "BUY" else ref + tk * offset_ticks
                px = px.quantize(tk, rounding=ROUND_DOWN if side == "BUY" else ROUND_DOWN)
                r = self.place_postonly(symbol, side, px, qty, live=live)
                if live and r is not None:
                    placed_oid = r[1] if isinstance(r, tuple) and len(r) > 1 else None
                placed_px = px
                n_rq += 1
            else:
                po = self._hc.get_pending_orders()
                mine = None
                for o in (getattr(po, "orders", None) or []):
                    if o.symbol == symbol and (_side_buy(o.side) == (side == "BUY")):
                        mine = o
                        break
                if mine is None:
                    # 单不在了：要么成交要么被吃后撤——查成交增量（铁律）
                    if self._n_trades() > trades_before:
                        return placed_px, qty
                    print("    [%s] 单消失无成交 -> 重挂" % side)
                    placed_px = None
                    time.sleep(1.0)
                    continue
                dev_bp = abs(Decimal(str(placed_px)) - ref) / ref * 10000 if ref else Decimal(0)
                if float(dev_bp) > requote_bp:
                    try:
                        self.cancel_order(mine.orderId)
                    except Exception:
                        pass
                    n_rq += 1
                    # 撤单后立刻查成交（部分成交藏在已撤订单上）
                    if self._n_trades() > trades_before:
                        return placed_px, qty
                    placed_px = None
            time.sleep(1.2)
        # 超时收尾：撤单 + 再查一次成交
        try:
            po = self._hc.get_pending_orders()
            for o in (getattr(po, "orders", None) or []):
                if o.symbol == symbol:
                    self.cancel_order(o.orderId)
        except Exception:
            pass
        if self._n_trades() > trades_before:
            return placed_px, qty
        print("    [%s] TIMEOUT %.0fs rq=%d" % (side, time.time() - t0, n_rq))
        return None
