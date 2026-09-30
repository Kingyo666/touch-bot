# -*- coding: utf-8 -*-
"""Lighter（lighter.xyz）对冲腿同步封装——基于官方 lighter SDK。

设计给 touch+DN 模式做对冲腿：主所成交多少，这里反向市价吃多少。
要点（实测踩坑，别删）:
  - 市价单实际是"限价+保护顶"（avg_execution_price 按 VWAP+滑点带计算），
    不是无限滑点的真市价——防极端滑点
  - create_market_order 失败不抛异常，装在返回三元组第 3 位（err）——
    必须检查，否则被拒了还当成功（单腿裸奔）
  - 精度（size/price decimals）启动时动态拉，别 hardcode
  - 只用主网 mainnet.zklighter.elliot.ai；Standard 账户档 0 费够用

.env 键（Lighter 侧）:
  LIGHTER_BASE_URL        https://mainnet.zklighter.elliot.ai
  LIGHTER_ACCOUNT_INDEX   账户 index（网页 API 页可见）
  LIGHTER_API_KEY_INDEX   api key 的 index（一般 0 或 1）
  LIGHTER_API_PRIVATE_KEY api 私钥（只填 API key 对应的，不是钱包主私钥）
  LIGHTER_MARKET_INDEX    市场 index（BTC=1，其他在网页 API 文档查）
"""
import asyncio
import threading
import time
from decimal import Decimal
from typing import Optional, Tuple


class LighterLeg:
    name = "lighter"

    def __init__(self, *, market_index: int, base_url: str, account_index: int,
                 api_key_index: int, api_private_key: str,
                 slippage: Decimal = Decimal("0.003")):
        self._mi = market_index
        self._base_url = base_url
        self._aidx = account_index
        self._kidx = api_key_index
        self._pk = api_private_key
        self._sd = None      # size decimals，启动动态拉
        self._pd = None      # price decimals
        self._slip = slippage
        self._loop = None
        self._thr = None
        self._signer = None
        self._api = None
        self._order_api = None
        self._account_api = None
        self._pos = Decimal(0)

    # ── async↔sync 桥 ──
    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _call(self, coro, timeout: float = 8):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def start(self):
        self._loop = asyncio.new_event_loop()
        self._thr = threading.Thread(target=self._run_loop, daemon=True, name="lighter-loop")
        self._thr.start()
        self._call(self._connect(), timeout=30)

    async def _connect(self):
        import lighter
        self._api = lighter.ApiClient(configuration=lighter.Configuration(host=self._base_url))
        self._order_api = lighter.OrderApi(self._api)
        self._account_api = lighter.AccountApi(self._api)
        self._signer = lighter.SignerClient(
            url=self._base_url, account_index=self._aidx,
            api_private_keys={self._kidx: self._pk})
        try:
            err = self._signer.check_client()
            if asyncio.iscoroutine(err):
                err = await err
            if err is not None:
                raise RuntimeError("lighter signer check 失败: %s" % err)
        except AttributeError:
            pass
        # 动态拉精度
        if self._sd is None or self._pd is None:
            d = await self._order_api.order_book_details(market_id=self._mi)
            row = d[0] if isinstance(d, list) else getattr(d, "order_book_details", d)
            row = row[0] if isinstance(row, list) else row
            g = (lambda o, k: o.get(k) if isinstance(o, dict) else getattr(o, k, None))
            self._sd = int(g(row, "size_decimals"))
            self._pd = int(g(row, "price_decimals"))

    def stop(self):
        try:
            if self._signer:
                self._call(self._signer.close(), timeout=10)
            if self._api:
                self._call(self._api.close(), timeout=10)
        except Exception:
            pass
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop())

    # ── scaling ──
    def _scale_amt(self, qty) -> int:
        return int((Decimal(str(qty)) * (10 ** self._sd)).to_integral_value())

    def _scale_px(self, px) -> int:
        return int((Decimal(str(px)) * (10 ** self._pd)).to_integral_value())

    # ── 行情 ──
    def best_bid_ask(self) -> Optional[Tuple[Decimal, Decimal]]:
        try:
            ob = self._call(self._order_api.order_book_orders(market_id=self._mi, limit=1))
            g = (lambda o, k: o.get(k) if isinstance(o, dict) else getattr(o, k, None))
            asks = g(ob, "asks")
            bids = g(ob, "bids")
            return Decimal(str(g(bids[0], "price"))), Decimal(str(g(asks[0], "price")))
        except Exception:
            return None

    def book_vwap(self, side, qty, depth=50) -> Optional[Decimal]:
        """按量吃深度的 VWAP——市价保护价的基准（别锚盘口顶档，大单会吃穿深度）。"""
        try:
            ob = self._call(self._order_api.order_book_orders(market_id=self._mi, limit=depth))
            g = (lambda o, k: o.get(k) if isinstance(o, dict) else getattr(o, k, None))
            asks = sorted(g(ob, "asks") or [], key=lambda r: Decimal(str(g(r, "price"))))
            bids = sorted(g(ob, "bids") or [], key=lambda r: -Decimal(str(g(r, "price"))))
            book = asks if side == "BUY" else bids
            rem, cost = Decimal(str(qty)), Decimal(0)
            for row in book:
                px = Decimal(str(g(row, "price")))
                amt = Decimal(str(g(row, "amount") or 0))
                take = min(rem, amt)
                cost += take * px
                rem -= take
                if rem <= 0:
                    return cost / Decimal(str(qty))
            return None
        except Exception:
            return None

    # ── 账户 ──
    def position(self) -> Decimal:
        """带符号净持仓（本地账本 + REST 对账）。"""
        try:
            acct = self._call(self._account_api.account(by="index", value=str(self._aidx)))
            src = (getattr(acct, "accounts", None) or [acct])[0]
            g = (lambda o, k: o.get(k) if isinstance(o, dict) else getattr(o, k, None))
            for p in (g(src, "positions") or []):
                if int(g(p, "market_index") or -1) == self._mi:
                    amt = Decimal(str(g(p, "base_amount") or 0)) / (10 ** self._sd)
                    self._pos = -amt if g(p, "is_ask") else amt   # SDK 侧空仓为正+is_ask
                    return self._pos
            self._pos = Decimal(0)
        except Exception:
            pass
        return self._pos

    def total_asset_value(self) -> Optional[Decimal]:
        try:
            acct = self._call(self._account_api.account(by="index", value=str(self._aidx)))
            src = (getattr(acct, "accounts", None) or [acct])[0]
            g = (lambda o, k: o.get(k) if isinstance(o, dict) else getattr(o, k, None))
            for f in ("total_asset_value", "collateral", "available_balance"):
                v = g(src, f)
                if v is not None:
                    return Decimal(str(v))
        except Exception:
            pass
        return None

    # ── 下单 ──
    def place_taker(self, side: str, qty) -> Optional[Decimal]:
        """市价（限价+保护顶）吃单，返回保护价或 None（被拒/异常）。"""
        bbo = self.best_bid_ask()
        if not bbo:
            print("    [Lighter] 无盘口，跳过")
            return None
        bid, ask = bbo
        vw = self.book_vwap(side, qty)
        base_px = vw if vw is not None else (ask if side == "BUY" else bid)
        avg_px = base_px * (Decimal(1) + self._slip) if side == "BUY" else base_px * (Decimal(1) - self._slip)
        coid = int(time.time() * 1000) % (10 ** 9)
        try:
            result = self._call(self._signer.create_market_order(
                market_index=self._mi, client_order_index=coid,
                base_amount=self._scale_amt(qty), avg_execution_price=self._scale_px(avg_px),
                is_ask=(side == "SELL"), reduce_only=False))
            # 失败装在三元组第 3 位——必须检查
            err = result[2] if isinstance(result, tuple) and len(result) == 3 else None
            if err is not None:
                print("    [Lighter taker 被拒] side=%s qty=%s err=%s" % (side, qty, err))
                return None
            sign = -1 if side == "SELL" else 1
            self._pos += sign * Decimal(str(qty))
            return avg_px
        except Exception as ex:
            print("    [Lighter taker 异常] %r" % ex)
            return None

    def cancel_all(self):
        try:
            result = self._call(self._signer.cancel_all_orders(
                time_in_force=self._signer.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
                timestamp_ms=int(time.time() * 1000),
                cancel_all_market_index=self._mi))
            err = result[2] if isinstance(result, tuple) and len(result) == 3 else None
            if err is not None:
                print("    [Lighter cancel_all 被拒] err=%s" % err)
        except Exception as ex:
            print("    [Lighter cancel_all 异常] %r" % ex)
