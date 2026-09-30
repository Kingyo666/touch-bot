# -*- coding: utf-8 -*-
"""Hibachi 刷量 bot（A/B 双模式）

MODE=A（taker 秒开秒平）: 市价开 -> 市价平(reduce-only) -> 间隔 -> 循环。
    出量最快、每轮成本最高（taker 费×2+点差×2），适合短窗冲量。
MODE=B（touch + Lighter 对冲 DN）: 挂 post-only 贴盘口等成交 -> Lighter 市价对冲
    -> 挂 post-only 平出 -> Lighter 平回。每轮成本低（maker 0 费），出量慢。

风控（内建，不可关闭）:
  - DRY_RUN 默认 true：只打印计划不下单；实盘需 .env 同设 DRY_RUN=false + LIVE_TRADING_ACK=true
  - KILL_USD：累计亏损（余额差分口径）超过即撤单+平仓+停机
  - VOL_STOP：累计成交量（双边合计）达标即停
  - 停止（Ctrl-C / 崩溃 / 触线）= finally 撤全部挂单 + 两腿 reduce-only 平到零

成交判定铁律: 挂单从 open_orders 消失 ≠ 未成交——必须查 totalQuantity 与
availableQuantity 的差值（部分成交可能藏在已撤销的订单里），本文件 touch_fill
按此实现；对冲提交成功 ≠ 成交，靠轮次间仓位对账兜底。

跑法: python touch_bot.py            （读同目录 .env）
"""
import os
import sys
import time
from decimal import Decimal, ROUND_DOWN

from hibachi_client import HibachiVenue, load_env
from lighter_client import LighterLeg

E = load_env(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

MODE = (E.get("MODE") or "A").upper()
SYMBOL = E.get("SYMBOL") or "BTC/USDT-P"
NOTIONAL = Decimal(E.get("NOTIONAL") or "10")          # 每轮单边名义 $
INTERVAL = float(E.get("INTERVAL") or "5")             # A 模式轮间隔 s
VOL_STOP = Decimal(E.get("VOL_STOP") or "0")           # 双边累计量目标 $，0=不限
KILL_USD = Decimal(E.get("KILL_USD") or "5")           # 累亏停机线 $
OFFSET_TICKS = int(E.get("OFFSET_TICKS") or "0")       # B: 挂单价离盘口 tick 数（0=贴盘口）
REQUOTE_BP = float(E.get("REQUOTE_BP") or "1.0")       # B: 偏离盘口多少 bp 撤单重挂
ORDER_TIMEOUT = float(E.get("ORDER_TIMEOUT") or "120") # B: 挂单超时 s，超时撤单
DRY_RUN = (E.get("DRY_RUN") or "true").lower() != "false"
LIVE_ACK = (E.get("LIVE_TRADING_ACK") or "").lower() == "true"

GO_LIVE = (not DRY_RUN) and LIVE_ACK


def log(msg):
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def main():
    hv = HibachiVenue(E)
    leg = None
    if MODE == "B":
        leg = LighterLeg(
            market_index=int(E["LIGHTER_MARKET_INDEX"]),
            base_url=E["LIGHTER_BASE_URL"],
            account_index=int(E["LIGHTER_ACCOUNT_INDEX"]),
            api_key_index=int(E["LIGHTER_API_KEY_INDEX"]),
            api_private_key=E["LIGHTER_API_PRIVATE_KEY"],
        )
        leg.start()

    state = {"vol": Decimal(0), "wear": Decimal(0), "n": 0}

    def flatten():
        """撤 Hibachi 全部挂单 + 两腿市价平到零（reduce-only）。"""
        try:
            hv.cancel_all()
        except Exception as e:
            log("cancel_all 异常: %r" % e)
        hv.flatten_all(reason="stop")
        if leg is not None:
            try:
                leg.cancel_all()
            except Exception:
                pass
            for _ in range(3):
                lp = leg.position()
                if lp == 0:
                    break
                leg.place_taker("BUY" if lp < 0 else "SELL", abs(lp))
                time.sleep(1)

    try:
        b0 = hv.balance()
        l0 = leg_balance(leg)
        log("bot up: MODE=%s symbol=%s notional=$%s dry_run=%s kill=$%s vol_stop=$%s | h0=%.4f l0=%s"
            % (MODE, SYMBOL, NOTIONAL, DRY_RUN, KILL_USD, VOL_STOP, b0, l0))
        while True:
            if VOL_STOP > 0 and state["vol"] >= VOL_STOP:
                log("VOL_STOP 到量 $%s -> 收工" % state["vol"])
                break
            if state["wear"] >= KILL_USD:
                log("KILL 触发 wear=$%s >= $%s -> 收工" % (state["wear"], KILL_USD))
                break
            try:
                if MODE == "A":
                    round_a(hv, state)
                else:
                    round_b(hv, leg, state)
            except Exception as e:
                log("round 异常（10s 后重试，连续异常请手动检查仓位）: %r" % e)
                time.sleep(10)
                continue
            time.sleep(INTERVAL if MODE == "A" else 2)
    except KeyboardInterrupt:
        log("Ctrl-C -> 收工")
    finally:
        flatten()
        b1, l1 = hv.balance(), leg_balance(leg)
        log("DONE: n=%d vol=$%s wear=$%s | h=%.4f l=%s" %
            (state["n"], state["vol"], state["wear"], b1, l1))
        if leg is not None:
            leg.stop()


def leg_balance(leg):
    if leg is None:
        return None
    try:
        return leg.total_asset_value()
    except Exception:
        return None


# ── MODE A：taker 秒开秒平 ──
def round_a(hv, state):
    b0 = hv.balance()
    qty = hv.qty_for_notional(SYMBOL, NOTIONAL)
    log("[A] 开 %s %s ..." % (SYMBOL, qty))
    if GO_LIVE:
        hv.market_order(SYMBOL, "BUY", qty)
        hv.market_order(SYMBOL, "SELL", qty, reduce_only=True)
    b1 = hv.balance()
    wear = b0 - b1 if GO_LIVE else Decimal(0)
    vol = NOTIONAL * 2
    state["n"] += 1
    state["vol"] += vol
    state["wear"] += wear
    log("[A] round#%d wear=$%s vol=+$%s cum_wear=$%s%s"
        % (state["n"], wear, vol, state["wear"], "" if GO_LIVE else " (dry)"))


# ── MODE B：touch 挂单 + Lighter 对冲 ──
def round_b(hv, leg, state):
    b0, l0 = hv.balance(), leg_balance(leg)
    qty = hv.qty_for_notional(SYMBOL, NOTIONAL)
    r = hv.touch_fill(SYMBOL, "BUY", qty, OFFSET_TICKS, REQUOTE_BP, ORDER_TIMEOUT, GO_LIVE)
    if r is None:
        log("[B] 进场超时未成交 -> 跳过本轮")
        return
    p_in, q_in = r
    if GO_LIVE:
        if leg.place_taker("SELL", q_in) is None:
            log("[B] !!! 对冲失败 -> 先平 Hibachi 仓再继续（敞口保护）")
            hv.market_order(SYMBOL, "SELL", q_in, reduce_only=True)
            return
    r = hv.touch_fill(SYMBOL, "SELL", qty, OFFSET_TICKS, REQUOTE_BP, ORDER_TIMEOUT, GO_LIVE)
    if r is None:
        log("[B] !!! 出场超时 -> 市价强平两腿")
        hv.market_order(SYMBOL, "SELL", q_in, reduce_only=True)
        if GO_LIVE:
            leg.place_taker("BUY", q_in)
        return
    p_out, q_out = r
    if GO_LIVE and leg.place_taker("BUY", q_out) is None:
        log("[B] !!! 平对冲腿失败 -> 检查 Lighter，下轮对账自愈")
    b1, l1 = hv.balance(), leg_balance(leg)
    wear = (b0 - b1) if GO_LIVE else Decimal(0)
    if l0 is not None and l1 is not None:
        wear += (l0 - l1)
    vol = NOTIONAL * 2          # 双边口径：开+平
    state["n"] += 1
    state["vol"] += vol
    state["wear"] += wear
    bp = (wear / vol * Decimal(10000)) if vol > 0 else Decimal(0)
    log("[B] round#%d in@%s out@%s wear=$%s(=%s bp/双边量) vol=+$%s cum=$%s%s"
        % (state["n"], p_in, p_out, wear, round(bp, 2), vol, state["wear"],
           "" if GO_LIVE else " (dry)"))


if __name__ == "__main__":
    main()
