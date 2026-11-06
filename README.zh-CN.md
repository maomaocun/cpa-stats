# cpa-stats

[English](README.md) · **简体中文**

面向 **CLIProxyAPI**(CPA)的按请求延迟 / TTFT / TPS / 成本监控。

CPA 把每条请求的用量记录放在一个**会被消费的短命队列**上
(`GET /v8/management/observability/usage/queue`,旧版是
`/v0/management/usage-queue`,读一次就没了)。`cpa-stats collect`
把队列落进本地 SQLite;其余子命令只读数据库,所以任意多个查看端
可以同时运行。

```
CPA (127.0.0.1:PORT)
   │  每 2 秒拉一次,每次最多 500 条;凭证/quota 快照每 60 秒
   ▼
cpa-stats collect  →  SQLite (WAL)  →  watch / tui / tail / summary / quota / export
```

## 安装

```bash
# 一行命令:平台有独立二进制就用它,否则回落到 pipx/pip
curl -fsSL https://raw.githubusercontent.com/maomaocun/cpa-stats/master/install.sh | sh

# 或者从 checkout / tag 安装
pipx install .          # 或: pip install --user .
cpa-stats doctor        # 体检:CPA 端点、key、collector、数据库
```

需要 Python ≥ 3.10 和 `rich`。

打 tag(`git tag vX.Y.Z && git push --tags`)会触发 release 流程:
3.10–3.12 跑测试,每个平台出一个 PyInstaller 单文件二进制
(`cpa-stats-linux-amd64`、`cpa-stats-darwin-arm64`),然后发布
GitHub Release(sdist + wheel + 二进制)。`install.sh` 优先用二进制
(目标机不需要 Python),没有对应平台的构建时回落 `pipx` /
`pip --user`。也可以直接按 tag 安装
(`pipx install git+https://github.com/maomaocun/cpa-stats@v2.3.0`),
或从 release 页下载 wheel / 二进制。`cpa-stats upgrade` 对比最新
release 和当前版本,`--apply` 按安装方式自动升级(二进制 → 重新下载;
editable → `git pull --ff-only` + `pip install -e`;pipx → `pipx
upgrade`;pip → `pip install --user -U git+…@tag`)。collector 每天
重新检查一次最新 release;落后时 `watch` / `tui` / `doctor` 会显示
`↑ vX.Y.Z` 提示。

`install.sh` 访问 GitHub 可以走代理:`CPA_STATS_PROXY` 环境变量优先,
其次 `https_proxy` / `HTTPS_PROXY`;都没设但本机 127.0.0.1:7890 有
代理在监听时自动使用(clash/mihomo 常见端口)。包代码本身不做任何
代理处理——urllib 默认认标准 env。

## 快速上手

```bash
cpa-stats collect &                       # 启动 collector
cpa-stats collect --url http://a:8317,http://b:8317   # 多个 CPA 实例并入一个 db
cpa-stats                                 # 实时 dashboard(默认 1h 窗口)
cpa-stats tui                             # 交互式 TUI(键盘/鼠标)
cpa-stats -w 15m -m swe-2                 # 过滤窗口 + 模型
cpa-stats tail                            # 每请求一行,实时(-v:完整路由细节)
cpa-stats show <id>                       # 单条请求的全部字段(--raw 输出 JSON)
cpa-stats quota                           # 凭证 quota / 冷却 + 燃尽预测
cpa-stats errors                          # 按 模型/状态码/fail code/endpoint 的失败拆解
cpa-stats top --since 7d                  # 最重请求(按 tokens / output / cost / latency)
cpa-stats trend --since 7d --bucket 6h    # 逐桶指标 sparkline(req/fail/TTFT/TPS/tokens)
cpa-stats report --since 24h              # 紧凑摘要(--webhook 可发到 Lark/通用 webhook)
cpa-stats sql 'SELECT model, COUNT(*) FROM requests GROUP BY 1'   # 只读 SQL
cpa-stats serve --port 8399               # 只读 web dashboard(自动刷新)+ /api/rows
cpa-stats summary --since 7d --by model   # 聚合报告
cpa-stats export --since 7d -o out.csv    # CSV / JSONL 导出
cpa-stats prune --older-than 30d          # 删除旧记录
```

查看类命令都支持 `-m`(模型)、`-s`(session)和 `-a`(账号)过滤。
`summary`、`errors`、`top`、`quota`、`show`、`trend`、`report`
还支持 `--json` 方便脚本化。

### `cpa-stats tui`

交互式 dashboard(textual),数据和 `watch` 一致但可导航:

| 按键 / 鼠标 | 作用 |
|-|-|
| `1`–`7` | 切换窗口:15 m / 1 h / 6 h / 24 h / 7 d / 30 d / 全部 |
| `↑` `↓` / 滚轮 / 点击 | 选中当前表格的行 |
| `←` `→` | 切换右侧 tab(recent · sessions · trend · heatmap · quota · totals) |
| `tab` | 在面板间循环焦点 |
| `enter` | 请求/凭证详情弹窗;在 session 或 model 行上:按它过滤(再按取消) |
| `f` | 开关模型过滤输入框 |
| `s` | 切换 models 表排序键(req → in → out → tps → lat) |
| `e` | 把当前窗口的行导出为 CSV 到当前目录 |
| `p` | 暂停 / 恢复自动刷新 |
| `r` | 强制刷新 |
| `q` / `esc` | 退出 / 关闭弹窗 |

右侧六个 tab:**recent**(每条请求,可选中 → 详情)、**sessions**
(按 token 排序的 top sessions)、**trend**(窗口内逐桶指标
sparkline)、**heatmap**(7 天 × 24 小时请求量网格)、**quota**
(每凭证 quota 信号,可选中 → 完整冷却/信号详情)、**totals**
(全量汇总 + 数据质量)。statusbar 显示所有凭证中最低的剩余 quota。

## Dashboard

```
cpa-stats   window 1h                                    ● collector live  2000 stored · 08:08
╭─ all time · requests ─╮ ╭─ all time · input ──╮ ╭─ all time · cache ──╮ ╭─ all time · est. cost
│        2000           │ │       66.74M        │ │       hit 96%       │ │        $64.77
╭─ requests · 1h ───────╮ ╭─ TTFT p50 ──────────╮ ╭─ TPS p50 decode/e2e ╮ ╭─ latency p50 ───────
│        1219           │ │        784ms        │ │   103 / 64 tok/s    │ │        1.86s
activity ▁▁▁▄▃▂▂▁▃▁▁▁▅█▁▁▁▁▁▁▁▁▁▁  1219 req · peak 308/2min bucket
accounts · quota                                        ← 每凭证 daily/weekly %、重置时间、冷却
per model · per session · recent requests
```

- **all-time 行** —— 整个数据库的累计(requests、tokens、cache、
  output、估算成本),SQL 计算而不是加载行。
- **窗口卡片** —— 成功率,TTFT / TPS / 延迟分位数,token 拆分
  (new / cache-read / cache-write),cache 命中率。
- **activity** —— 窗口内请求速率 sparkline。
- **top sessions** —— 当前消耗 token 最多的 session。
- **est. cost** —— 传了 `--price 'IN,CACHED,OUT'`(每 1M token 的
  USD,如 `--price '6,0.6,18'`)时显示。

## 指标口径

| 指标 | 定义 | 备注 |
|-|-|-|
| TTFT | 到上游第一个字节的时间 | tool-call 请求 TTFT ≈ 总延迟(见 burst) |
| latency | 请求总耗时 | |
| TPS | out ÷ (latency − TTFT) | 上界:out 含首字节前生成的隐藏 reasoning |
| e2e | out ÷ latency | 最可靠的速度指标 |
| burst | out ≥ 16 且 (latency − TTFT) < 300 ms | Devin 一次性下发 tool call |
| in / new / c.read / c.write | in = new + c.read + c.write | 少数记录(Claude cache-creation)把 c.write 放在 in 外;`token_parts()` 两种情况都处理 |
| hit | c.read ÷ in | Devin swe-2 典型 ~90% |
| clean | 上游 `token_breakdown.quality` 为 `complete` 的行占比 | 低了说明上游记账不一致 |
| cost | (new·P_in + c.read·P_cached + out·P_out) / 1e6 | 仅 `--price` 时显示 |
| reasoning | 上游上报时才有 | Devin 不上报(计入 `out`) |

## Quota

collector 还会每 `--quota-interval` 秒(默认 60;`0` 关闭)快照
`GET /v8/management/credentials`(回落 `/v0/management/auth-files`)。
最新快照存在 `meta.credentials_snapshot`,每次轮询同时追加到
`quota_history` 表——查看端只读本地 db,不额外打管理 API。
`cpa-stats quota`、`watch` dashboard 和 TUI **quota** tab 展示每凭证:
plan、各窗口剩余 quota %、重置倒计时、ok/fail 请求数、状态
(`active` / `error` / `blocked` / `limited`)、冷却、按模型的
quota、剩余的 provider 信号。Devin 读 `daily_quota_*`/`weekly_quota_*`;
Codex 读 `x-codex-primary-*`/`secondary-*`(窗口真实长度取
`window-minutes`,还有 credits 和 `code-review`、
`additional-<name>-*` 这类具名额外限额);Claude 读
`anthropic-ratelimit-unified-5h-*`/`7d-*`(utilization → 剩余 %,
分窗口状态)。`x-codex-limit-reached`、`x-codex-allowed` 和
`*-status: rejected` 会把凭证标记为 **limited**;`Retry-After`
显示为重试倒计时。从历史数据 `quota` 算出燃尽预测
(`−N%/day → 0 in Xd`)和趋势 sparkline——包括 Codex
`additional-*` 窗口的具名限额趋势——账号将耗尽时在被 block 之前就能
看到。`pace ≤N%/day` 行对比可持续消耗速率和当前速率("over pace"
意味着重置前就会耗尽)。凭证被 block 或窗口失败率超 5% 时 `watch`
会显示红色 ⚠ 行。传 `--alert-webhook` 后 collector 会在 quota 状态
跳变时 POST 文本告警(窗口低于 `--alert-below`、blocked/limited 翻转、
恢复、`--alert-eta-hours` 内预测耗尽)——Lark 机器人 webhook 可用。
请求级失败飙升也会告警:滚动 `--alert-fail-window`(默认 10m)内失败
占比达到 `--alert-fail-pct`(25%)且样本 ≥ `--alert-fail-min`(10)
时触发一次,恢复时再发一次。`cpa-stats report --webhook` 可按需推送
摘要(配合 cron 做日报)。

## Collector

usage 队列是消费式的——只允许一个读者,所以 `collect` 必须是单例。

```bash
cpa-stats collect                       # 前台运行
cpa-stats collect --retention-days 30   # 自动清理,每小时检查一次
cpa-stats collect --quota-interval 30   # 每 30 s 快照凭证/quota
cpa-stats collect --alert-webhook https://open.feishu.cn/...   # Lark/通用 webhook 告警
#   触发:quota < --alert-below(20%)、blocked/limited 翻转、恢复、
#   --alert-eta-hours 内预测耗尽、失败率飙升
#   (--alert-fail-pct / --alert-fail-window / --alert-fail-min)
cpa-stats install-service               # 写 + 启用 systemd --user unit
```

如果记录已被消费但写库失败,会先追加到 db 旁的 `spool.jsonl`,
下次启动时重放——已消费的数据绝不丢。

配置自动从 `~/.config/cliproxy/config.yaml`(端口)和旁边的
`management.key` 发现。可用 `CPA_URL`、`CPA_MGMT_KEY`、`CPA_DIR`、
`CPA_STATS_DB`、`CPA_STATS_PRICE`(默认 `--price`)、
`CPA_STATS_ALERT_WEBHOOK`(默认 `--alert-webhook` / `report --webhook`)
或对应 flag 覆盖。

`cpa-stats serve` 暴露:`/`(HTML dashboard,自动刷新)、
`/api/rows`、`/api/summary`、`/api/quota`、`/api/errors`(JSON)、
`/metrics`(Prometheus 格式——存储总量、分窗口 p50/p95
TTFT/latency/TPS、每凭证 quota %、collector 心跳)和 `/healthz`。

## 数据库

`~/.local/share/cpa-stats/requests.db`,权限 600,WAL 模式。schema 有
版本号(`meta.schema_version`),`open_db` 自动跑挂起的 migration。
最新凭证/quota 快照在 `meta.credentials_snapshot`。敏感字段
(`api_key`、`response_headers`、`access_token_sha256`)入库前丢弃。

## 开发

```bash
git clone <this repo> && cd cpa-stats
pip install --user -e '.[test]'
pytest                    # 143 tests
python -m cpa_stats ...   # 免安装直接跑
```

目录:

```
src/cpa_stats/
  model.py      纯指标计算 + 记录解析(无 I/O)
  storage.py    schema、migration、查询
  collector.py  usage-queue 守护进程
  views.py      rich 渲染
  cli.py        argparse + 入口
  format.py     数字 / 时间 / sparkline 格式化
tests/          单元 + 端到端(模拟 usage-queue HTTP 服务)
```
