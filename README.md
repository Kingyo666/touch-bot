# hibachi-touch-bot

[Hibachi](https://app.hibachi.xyz)（Arbitrum/Base 上的永续 DEX）刷量/交易 bot，两种模式：

| 模式 | 干什么 | 每轮成本结构 | 适合 |
|---|---|---|---|
| **A**（taker 秒开秒平） | 市价开→市价平→间隔循环 | taker 费×2 + 点差×2（最贵） | 短窗快速冲交易量 |
| **B**（touch + Lighter 对冲） | 挂 post-only 等成交→Lighter 反向对冲→挂单平出→对冲平回 | maker 0 费 + 对冲腿滑点（便宜） | 慢速攒量，磨损敏感 |

> ⚠️ **风险声明**：本软件仅供学习研究，不构成投资建议。合约交易有爆仓风险；刷量模式每轮支付点差与手续费是结构性成本（费率再低也省不掉）；Hibachi/Lighter 均有地区限制（美国/中国大陆等），请自行确认合规。使用产生的一切盈亏由使用者自行承担。

## 用 Codex 部署（推荐）

把本仓库下载/克隆到本地，打开 Codex，直接发送下面这段：

```
阅读本仓库的 AGENTS.md 并严格按它的工序部署运行这个交易 bot：
装 Python 虚拟环境和依赖（注意 hibachi_xyz SDK 必须用仓库内 tools/hibachi_patched/
的补丁版覆盖安装，不要直接用 PyPI 版跑）、引导我填写 .env 凭证、先 DRY_RUN 冒烟、
再用最小单量（$10）实盘跑一轮验收（验收标准=开仓→平仓后仓位归零、余额变化合理），
全部通过后转入常驻运行。过程中任何一步失败就停下来向我报告，不要自行放大资金量。
```

## 手动部署（不用 AI 也行）

```bash
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install hibachi_xyz==0.3.2
# 🔴 关键一步：用仓库内补丁覆盖 SDK（PyPI 版在部分环境有签名问题）
python -c "import hibachi_xyz, os, shutil; p=os.path.dirname(hibachi_xyz.__file__); shutil.rmtree(p); shutil.copytree('tools/hibachi_patched', p)"
cp .env.example .env    # 填入你的凭证
python touch_bot.py     # 默认 DRY_RUN，只打印不下单
```

实盘：`.env` 里同时设 `DRY_RUN=false` 和 `LIVE_TRADING_ACK=true`。停止=Ctrl-C（自动撤单+两腿平仓）。

## 参数（.env）

| 参数 | 默认 | 说明 |
|---|---|---|
| `MODE` | A | A=taker 秒开秒平；B=touch+对冲 |
| `NOTIONAL` | 10 | 每轮单边名义 $ |
| `INTERVAL` | 5 | A 模式轮间隔秒 |
| `VOL_STOP` | 0 | 双边累计量目标 $，到量自动停（0=不限） |
| `KILL_USD` | 5 | 累计亏损 $ 停机线 |
| `OFFSET_TICKS` | 0 | B：挂单离盘口 tick 数（0=贴盘口最激进，越大越便宜越难成交） |
| `REQUOTE_BP` | 1.0 | B：盘口偏离多少 bp 撤单重挂 |
| `ORDER_TIMEOUT` | 120 | B：挂单超时秒 |

**调参从大到小的顺序反过来：从小到大**。先用最小单量跑通完整一轮（开→平→仓位归零），统计你自己的每轮实际成本，之后所有量级决策=实测每轮成本×目标轮数，不用感觉。任何时候 `KILL_USD` 和 `VOL_STOP` 双保险必须在。

## 费率与成本（公开信息）

- Hibachi 创世纪：**maker 0 费 / taker 4.5bp**（以官方费率页为准）
- Lighter Standard 档：maker/taker 均 0 费（滑点仍在）
- A 模式每轮付约 `2×taker费 + 2×点差`；B 模式主要付对冲腿滑点和两所价差漂移

## 常见问题

**Q: 为什么 B 模式挂单一直不成交？**
挂的是 post-only 限价单，要等对手方来吃。想快就把 `OFFSET_TICKS` 设 0（贴盘口）；想省就挂深一点慢慢等。盘口是移动的，bot 会按 `REQUOTE_BP` 跟着重挂。

**Q: 对冲腿下单失败会怎样？**
B 模式对冲失败时 bot 会立即市价平掉主所仓位（不裸奔敞口）再进入下一轮。Lighter 腿连续失败=凭证/网络问题，先停手检查。

**Q: 每轮亏损是 bug 吗？**
刷量的点差+费是结构性成本，不是 bug。先用小单量实测你的每轮成本，再决定要不要放大。

**Q: 支持哪些币？**
`HIBACHI_SYMBOL` 改市场名（如 ETH/USDT-P），B 模式还要同步改 `LIGHTER_MARKET_INDEX`（Lighter 文档查）。

## License

MIT
