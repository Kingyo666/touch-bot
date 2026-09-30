# AGENTS.md — 部署与运行工序（给 AI 编程助手）

你（Codex/Claude 等）的任务：帮用户把本仓库的 Hibachi 刷量 bot 部署跑起来。
按下面的工序走，**任何一步失败就停下来向用户报告**，不要跳步、不要自行放大资金量。

## 前置确认

1. Python 3.10+ 可用（`python3 --version`）
2. 用户已在 app.hibachi.xyz 有账户，并能在网页端创建 API key（账户 id / api key 名 / 私钥三件套）
3. 若用户要用 MODE=B（touch+对冲）：用户已在 app.lighter.xyz 创建 API key（account index / key index / api 私钥）

## Step 1：环境

```bash
cd <仓库目录>
python3 -m venv .venv && source .venv/bin/activate   # Windows 用 .venv\Scripts\activate
pip install -r requirements.txt
pip install hibachi_xyz==0.3.2
```

## Step 2：SDK 补丁覆盖（🔴 关键，不能跳）

PyPI 的 hibachi_xyz 在部分环境有签名/兼容问题，仓库内 `tools/hibachi_patched/` 是修好的版本：

```bash
python -c "import hibachi_xyz, os, shutil; p=os.path.dirname(hibachi_xyz.__file__); shutil.rmtree(p); shutil.copytree('tools/hibachi_patched', p)"
python -c "from hibachi_xyz import HibachiApiClient; print('sdk ok')"
```

打印 `sdk ok` 才算过。若 Python 版本 < 3.11 且 import 报 typing 相关错误，在覆盖后的包 `__init__.py` 里补 `import sys; if sys.version_info < (3,11): import typing; typing.override = lambda f: f`（shim 方式，加在文件最前）。

## Step 3：凭证

1. `cp .env.example .env`
2. 引导用户填（不要让用户把私钥贴进对话以外的任何地方；.env 已被 .gitignore 排除，永不提交）：
   - `HIBACHI_ACCOUNT_ID`（网页账户页的数字 id）
   - `HIBACHI_API_KEY` / `HIBACHI_PRIVATE_KEY`（API 页创建后下载的三件套）
   - MODE=B 再填 `LIGHTER_*` 四项
3. 首次参数保持默认（MODE=A、NOTIONAL=10、KILL_USD=5、DRY_RUN=true）——先跑通再加量

## Step 4：DRY_RUN 冒烟

```bash
python touch_bot.py
```

预期：打印 `bot up: MODE=... dry_run=True`，然后每轮打印 `[A] round#... (dry)` 的计划（不下单）。看到两三轮正常打印即可 Ctrl-C（会走收尾流程）。

## Step 5：实盘最小验收

1. `.env` 设 `DRY_RUN=false`、`LIVE_TRADING_ACK=true`（两行都改才是实盘）
2. `python touch_bot.py` 跑一轮（MODE=A 约几秒；MODE=B 要等挂单成交，可能 1-3 分钟）
3. **验收标准**：
   - 日志出现 `round#1`，wear 是个小的负数或接近 0
   - 跑完后 Ctrl-C，`DONE` 行后面人工到网页核对：Hibachi 仓位归零、（B 模式）Lighter 仓位归零
   - 仓位不归零=立刻报告用户，不要继续
4. 记录这一轮的实际磨损，告诉用户"你的每轮成本≈$X"

## Step 6：常驻运行与调参指引

- 常驻：`nohup python touch_bot.py > runs/bot.log 2>&1 &`（先 mkdir runs）
- 停止：`kill <pid>` 或 Ctrl-C——bot 的 finally 会撤单+平仓；强杀后若仓位非零，重跑一次 bot 再停即可对账归零
- **帮用户调参的纪律**（向用户解释时用）：
  1. 从小到大：先最小单量跑通全链路，统计每轮实际成本
  2. 所有量级决策=实测每轮成本 × 目标轮数，不基于感觉
  3. 任何时候 `KILL_USD`（亏到多少停）和 `VOL_STOP`（刷到多少量停）双保险必须在
  4. A 模式贵（taker 费换速度）适合短窗；B 模式便宜但慢（`OFFSET_TICKS` 调激进/保守）
  5. 用户问"为什么亏"：先查每轮成本结构（费率+点差+滑点是结构性成本），再查是否对冲腿失败

## 红线（AI 助手同样要守）

- 私钥只进 `.env`，不写进代码/日志/对话记录以外的文件，不提交 git
- 不替用户把 NOTIONAL/KILL_USD 调大——加量决策必须用户自己拍
- 对冲腿连续失败、仓位对不齐、余额异常跳变：停手报告，不自动"修复"
- 任何你不确定的行为差异（API 报错/字段形状变化）：先跑 `python -c` 只读探针确认，再动
