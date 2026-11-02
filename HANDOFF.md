# Handoff：Devin CLIProxy + Codex / opencode + cpa-stats

> 最后更新：2026-11-02 · 机器：`dev-alsh-dialogue-yuanshu-cpu`（阿里云上海） · 用户：`yuanshu`
>
> 本文汇总这次搭建的全部内容：CLIProxyAPI（下称 CPA）把 Devin 账号转成 OpenAI 兼容接口，Codex 和 opencode 通过它使用 SWE-2 / SWE-2 Lightning，`cpa-stats` 负责采集和展示每个请求的延迟、TPS、token 与缓存数据。文中不含任何密钥，密钥都在下面列出的文件里。

---

## 1. 总览

```
Codex CLI / opencode / 其他客户端
        │  OpenAI Responses / Chat Completions，Bearer = api.key
        ▼
CPA（127.0.0.1:27100，systemd 用户服务 cliproxy-yuanshu）
        │  proxy-url = http://127.0.0.1:7890（本机 mihomo，出口洛杉矶）
        ▼
Devin 上游（api.devin.ai / server.codeium.com）

CPA 管理接口 /v0/management/usage-queue（读一次就清空）
        │  每 2 秒取一次
        ▼
cpa-stats collect（systemd 用户服务 cpa-stats）→ SQLite → cpa-stats 各查看命令
```

| 组件 | 状态 | 关键位置 |
|-|-|-|
| CPA 8.0.51 + 自定义补丁 | 运行中，`Restart=always` | `~/cliproxy-yuanshu/` |
| Devin 账号 | 已登录，杨奕存（yangyicun187@gmail.com） | `~/cliproxy-yuanshu/auth/devin-*.json` |
| Codex 0.166 | 默认 `swe-2`、max 档 | `~/.codex/config.toml` |
| opencode 2.1.1 | 已加 `devin/swe-2`、`devin/swe-2-lightning` | `~/.config/opencode/opencode.json` |
| cpa-stats 2.0.0 | 采集服务运行中 | `~/.local/bin/cpa-stats`（pip 包，源码在本目录） |

参考文档（飞书）：
- 搭建教程：https://vrfi1sk8a0.feishu.cn/wiki/Q0SwwjHIliHrtqkEYMiczdGznPg
- Codex 接入补丁：wiki `WhBDwgI29ijCzBkFZ7Tc7P8hnqe`（Codex 接入 Devin CLIProxy）

---

## 2. CPA（CLIProxyAPI）

### 2.1 文件

| 路径 | 说明 |
|-|-|
| `~/cliproxy-yuanshu/bin/cli-proxy-api` | 当前程序，版本 `8.0.51+devincodex.fast`（自己编译） |
| `~/cliproxy-yuanshu/bin/cli-proxy-api.prev` | 上一版程序（`8.0.51+devincodex`，不含 Fast 补丁），回滚用 |
| `~/cliproxy-yuanshu/config.yaml` | 配置。**只能原地编辑**才会热加载（见 2.4） |
| `~/cliproxy-yuanshu/config.yaml.bak*` | 历次修改前的备份 |
| `~/cliproxy-yuanshu/api.key` | 客户端 API key（无末尾换行，opencode 用 `{file:}` 读它） |
| `~/cliproxy-yuanshu/management.key` | 管理密钥明文（配置里存的是 bcrypt 哈希） |
| `~/cliproxy-yuanshu/auth/devin-*.json` | Devin 凭据，**等同账号本身**，权限 600，勿外传 |
| `~/.config/systemd/user/cliproxy-yuanshu.service` | systemd 用户服务 |
| `~/cliproxy-devin-codex-8.0.51.patch` | 完整补丁（基于 v8.0.51，见 2.3） |
| `~/.local/go/` | Go 1.27.3，编译 CPA 用 |

### 2.2 当前配置要点（`config.yaml`）

- `server`：`127.0.0.1:27100`，只监听本机。
- `management.allow-remote: false`。
- `requests.proxy-url: "http://127.0.0.1:7890"`（为什么要代理见第 6 节）。
- `observability.usage`：`usage-statistics-enabled: true`，`redis-usage-queue-retention-seconds: 600`（默认是 60，改大是为了采集服务重启时不丢数据）。
- `routing.session-affinity: true`，`ttl 1h`：同一会话固定用同一个账号，保证缓存能命中。
- `oauth-model-alias.devin`（`fork: true`，`devin/…` 原名同时保留）：

| 别名（客户端用的名字） | 实际模型 |
|-|-|
| `swe-2` | `devin/swe-2` |
| `swe-2-lightning` | `devin/swe-2-lightning` |
| `gpt-6-astra` | `devin/gpt-6-astra` |
| `gpt-6.1-sol` | `devin/gpt-6-1-sol` |
| `gpt-6-luna` | `devin/gpt-6-luna`（Codex 子代理默认用它） |

### 2.3 补丁内容（`~/cliproxy-devin-codex-8.0.51.patch`）

在官方 v8.0.51 源码上：

1. **文档里的 Codex 补丁**（原样套上）：
   - 对所有工具描述改写 Devin 拒收的三句话，修复 Codex 的 403 `MCP configuration issue`。
   - 远程模型目录更新后重新登记模型，修复 `gpt-6.1-sol` 报 `unknown provider`。
   - 上报的客户端版本改成 `3000.11.3`。
2. **自己写的 Fast 补丁**（文档里的 Fast 补丁在 8.0.51 上套不上）：
   - 新文件 `internal/runtime/executor/helps/devin_service_tier.go`：`ApplyDevinServiceTier`。请求带 `service_tier: priority`（即 Codex 的 `/fast`）时切到快速档：SWE-2 加 `-lightning`，GPT 加 `-priority`，Claude Opus 加 `-fast`。
   - `devin_executor.go`：`devinRequestedServiceTier`。Responses → Interactions 转换会丢掉 `service_tier`，所以先从转换前的原始请求里读。
   - 单元测试：`helps/devin_service_tier_test.go`、`executor/devin_service_tier_test.go`。

### 2.4 常用操作

```bash
systemctl --user status cliproxy-yuanshu
systemctl --user restart cliproxy-yuanshu
journalctl --user -u cliproxy-yuanshu -f

K=$(cat ~/cliproxy-yuanshu/api.key)
curl -s --noproxy '*' http://127.0.0.1:27100/v1/models -H "Authorization: Bearer $K" | jq -r '.data[].id'
```

改配置的注意事项：
- **先备份**，再用编辑器原地保存，或 Python `open(p, "r+")` 加 `truncate`。用 `sed -i`、`mv`、先写临时文件再改名的方式替换文件，CPA 就感知不到之后的修改了。
- 看日志里有没有 `config successfully reloaded`，YAML 写坏时会出现 `failed to reload config`。
- **不要直接 `kill` CPA 去"测试"**。服务已改成 `Restart=always`，被 kill 后 3 秒内会自动拉起；以前是 `on-failure`，kill 后进程正常退出，服务就停住了（踩过一次坑）。

重新编译或升级（升级前先确认补丁能套上）：

```bash
export PATH=$HOME/.local/go/bin:$PATH GOTOOLCHAIN=local GOPROXY=https://goproxy.cn,https://proxy.golang.org,direct
D=$(mktemp -d) && cd $D
git clone -q --depth 1 --branch v8.0.51 https://github.com/router-for-me/CLIProxyAPI.git src && cd src
git apply ~/cliproxy-devin-codex-8.0.51.patch
go test ./internal/translator/common/ ./internal/runtime/executor/helps/ ./internal/runtime/executor/ -run 'Devin|Sanitize|ServiceTier'
CGO_ENABLED=1 go build -buildvcs=false -ldflags="-s -w -X 'main.Version=8.0.51+devincodex.fast'" -o $D/cli-proxy-api ./cmd/server/
cp -p ~/cliproxy-yuanshu/bin/cli-proxy-api ~/cliproxy-yuanshu/bin/cli-proxy-api.prev
install -m 755 $D/cli-proxy-api ~/cliproxy-yuanshu/bin/cli-proxy-api.new && mv -f ~/cliproxy-yuanshu/bin/cli-proxy-api.new ~/cliproxy-yuanshu/bin/cli-proxy-api
systemctl --user restart cliproxy-yuanshu
```

回滚：`mv -f ~/cliproxy-yuanshu/bin/cli-proxy-api.prev ~/cliproxy-yuanshu/bin/cli-proxy-api && systemctl --user restart cliproxy-yuanshu`

### 2.5 重新登录 Devin

本机 Devin CLI 的凭据在 `~/.local/share/devin/credentials.toml`（`windsurf_api_key`，形如 `devin-session-token$…`），可以直接喂给 CPA：

```bash
~/cliproxy-yuanshu/bin/cli-proxy-api -config ~/cliproxy-yuanshu/config.yaml -devin-login -no-browser
# 提示 "Paste the Devin authorization code or session token" 时粘贴上面的 token，或浏览器登录后拿到的授权码
```

不用重启，CPA 会自动加载 auth 目录里的新凭据。检查凭据是否有效：

```bash
T=$(jq -r .api_key ~/cliproxy-yuanshu/auth/devin-*.json)
curl -s -x http://127.0.0.1:7890 -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $T" https://api.devin.ai/v3/self; unset T
```

---

## 3. Codex（`~/.codex/config.toml`）

- 顶层：`model = "swe-2"`，`model_provider = "devin"`，`model_reasoning_effort = "max"`，`plan_mode_reasoning_effort = "max"`，`model_context_window = 262000`，`model_auto_compact_token_limit = 235000`，`service_tier = "default"`。
- `model_catalog_json = "/home/yuanshu/.codex/model-catalogs/devin.json"`：Codex 自带的模型列表，加上：
  - `swe-2`（显示名 SWE-2）：从 GPT-6-Astra 的元数据复制，因此有 GPT-6 系统提示和原生 apply_patch。档位只保留 medium / high / max，并带 Fast 档（`service_tiers: priority`），`/fast` 才能用。
  - **两个 SWE-2 条目的 `tool_mode` 必须是 `null`（普通 shell 工具）**。复制来的 `code_mode_only` 会让 Codex 只提供一个运行 JavaScript 的 `exec` 工具，SWE-2 经常漏写 `text()` 打印结果，拿到空输出后以为 shell 坏了，越查越偏然后中途停住（见第 8 节第 8 条）。改过之后备份是 `devin.json.bak-codemode-*`。
  - `swe-2-lightning`（显示名 SWE-2 Lightning）。
  - **Codex 升级后要重新生成这个文件**，因为它是生成时自带列表的快照。生成方法：`codex debug models --bundled`，再用 jq 追加上述条目（备份见 `devin.json.bak`）。
- `[model_providers.devin]`：`base_url = "http://127.0.0.1:27100/v1"`，`wire_api = "responses"`，`experimental_bearer_token` 就是 api.key 的内容。
- 原来的 `mafia` provider 保留着，临时切回：`codex -c model_provider='"mafia"' -m gpt-6-astra`。
- 原配置备份：`~/.codex/config.toml.bak-<时间戳>`。
- `~/.local/bin/codex` 是 shell 包装，设置了代理环境变量（`NO_PROXY` 包含 127.0.0.1），没有覆盖 `model_provider`。

用法：
- `/fast` 开关：开启后上游是 `swe-2-<档位>-lightning`，状态栏显示 `SWE-2 max Fast`。
- 也可以 `/model` 选 SWE-2 Lightning，或启动时加 `-m swe-2-lightning`。

---

## 4. opencode（`~/.config/opencode/opencode.json`）

新增 provider `devin`（其他 provider 没动，原文件备份为 `opencode.json.bak-<时间戳>`）：

- `package`：`@opencode/ai/providers/openai-compatible/responses`
- `settings.baseURL`：`http://127.0.0.1:27100/v1`
- `settings.apiKey`：`{file:~/cliproxy-yuanshu/api.key}`（key 不写进配置）
- models：`swe-2`、`swe-2-lightning`。context 262000，output 64000，reasoning effort 可选 medium / high / max，默认 `reasoningEffort: max`。

用法：`/models` 里选择，或 `opencode run -m devin/swe-2#high "..."`。默认模型仍是 `mafia/claude-opus-5-5`，没有改。

---

## 5. cpa-stats

### 5.1 文件

| 路径 | 说明 |
|-|-|
| `~/jfs-dialogue-mos-cnnw08/data/users/yuanshu/cpa-stats/` | 源码仓库（src layout，pip 安装） |
| `~/.local/bin/cpa-stats` | 已安装的入口（`pip install --user .`） |
| `~/.config/systemd/user/cpa-stats.service` | 采集服务，`Restart=always`，依赖 cliproxy-yuanshu |
| `~/.local/share/cpa-stats/requests.db` | SQLite（WAL），目录权限 700，文件 600 |
| `~/.local/share/cpa-stats/spool.jsonl` | 只在数据库写入失败时出现，下次启动会自动补写 |

### 5.2 设计

- CPA 的 usage-queue 读一次就清空，并且有保留时间，所以只能有**一个**读取者：`cpa-stats collect`（systemd 常驻，每 2 秒取一次，每批最多 500 条）。
- 其他命令只读 SQLite，可以开任意多个窗口。
- 数据去重键是 `execution_id`，写库用 `INSERT OR IGNORE`。
- 写库失败时，已取出的记录写入 spool，不会丢。
- 存储时去掉了 `api_key`、`response_headers` 等敏感字段。
- 默认保留 90 天，每小时清理一次（`collect --retention-days`）。
- 配置自动发现：从 `~/cliproxy-$USER/config.yaml` 读端口，从 `management.key` 读管理密钥。也可以用环境变量 `CPA_URL`、`CPA_MGMT_KEY`、`CPA_DIR`、`CPA_STATS_DB` 覆盖。

### 5.3 命令

```bash
cpa-stats                       # 实时面板（默认 1h 窗口）；-w 15m / -m swe-2 / -s <session前缀>
cpa-stats --price '6,0.6,18'    # 加 est. cost 卡（单价 USD/1M：输入,缓存读,输出）
cpa-stats tail                  # 每个请求一行
cpa-stats summary --since 7d --by model|effort|session|account|hour|day
cpa-stats export --since 7d --format csv|jsonl -o out.csv
cpa-stats prune --older-than 30d
cpa-stats install-service       # 生成 systemd 用户服务（已装过则不必再跑）
cpa-stats doctor                # 检查 CPA、管理密钥、采集服务心跳、数据库、schema 版本
systemctl --user status cpa-stats ; journalctl --user -u cpa-stats -f
```

### 5.4 指标定义

| 指标 | 定义 | 注意 |
|-|-|-|
| TTFT | CPA 在 HTTP 层收到 Devin **第一个字节**的时间 | 工具调用请求的 TTFT ≈ 总耗时（见 burst） |
| latency | 请求总耗时 | |
| TPS | out ÷（latency − TTFT） | 偏高，只能当上限：out 里含隐藏思考，而思考大多在第一个字节之前就生成了 |
| e2e | out ÷ latency | **衡量速度最可靠的指标** |
| burst | out ≥ 16 且（latency − TTFT）< 300ms | Devin 对工具调用是生成完才整块发出的；纯文字是真流式。Codex 的请求约 30% 是 burst |
| in / new / c.read / c.write | in = new + c.read + c.write | 少数记录（Claude、GPT 写缓存的请求）的 c.write 不含在上游 input 里，`token_parts()` 已兼容这种情况 |
| hit | c.read ÷ in | 实测 SWE-2 约 90%，Lightning 约 97% |
| reasoning | 上游没有上报 | Devin 的用量只有 prompt / completion / cached / cache_write 四项，思考算在 out 里。如果以后上游开始上报，会自动多出 `reason` 列 |
| cost | (new·P_in + c.read·P_cached + out·P_out)/1e6 | 只在传 `--price` 时显示；单价自己填 |
| clean | `token_breakdown.quality = 'complete'` 的占比 | 低说明上游记账不一致（已见过 `inconsistent`：input 报 0、cache_creation 报 13K 之类） |

### 5.5 版本记录

- 1.0：采集服务 + 面板 / tail / summary / export / prune / doctor
- 1.1：加 e2e 列和 burst 标记
- 1.2：拆分 new / c.read / c.write / hit，加 token 卡片，reasoning 列按需显示（1.1 的备份在 `/tmp/opencode/cpa-stats.bak-1.1`，1.2 的备份在 `/tmp/opencode/cpa-stats.bak-1.2`，临时目录，可能被清掉）
- 1.3：面板顶部新增 all-time 总量卡片行（累计请求、输入/输出 token、缓存读/写、命中率、首条记录时间），用单条聚合 SQL 计算，不把记录拉进内存
- 2.0：重构成 `cpa_stats` 包（src layout，`pipx/pip install .`）。新增：`--price` 成本估算卡、activity sparkline、top-sessions 表、token 数据质量标记（`token_quality` 列 + 迁移回填）、schema_version 迁移机制、`install-service` 子命令、47 个 pytest、`python -m cpa_stats` 入口、CI 工作流。旧单文件备份在 `/tmp/opencode/cpa-stats.bak-1.2`

---

## 6. 网络与稳定性（2026-11-02 实测）

本机直连出口是阿里云上海；走代理（mihomo，127.0.0.1:7890）的出口是洛杉矶 NTT。DNS 没有被污染。

| 目标 | 直连 | 走代理 |
|-|-|-|
| api.devin.ai / server.codeium.com | 30/30，但尾部延迟偏高（p90 1.8s） | 30/30，p90 约 0.9s |
| app.devin.ai | 有一轮 6/20 超时，另一轮 30/30 | 30/30 |
| raw.githubusercontent.com | **0/30**（28 次 TLS 被重置） | 30/30 |
| github.com | 13/30 | 30/30 |

端到端 SWE-2 推理（临时开一个直连的 CPA 实例做对比）：
- 普通请求：两边都是 20/20 成功，代理的 TTFT p50 1.10s，直连 1.37s。
- 长输出约 1800 token、4 路并发：两边都是 8/8 成功，e2e 代理 98 tok/s，直连 83 tok/s。

结论：推理直连也能用，但**建议一直走代理**。GitHub（模型目录更新、管理面板）直连基本不通，直连登录 Devin 也有人遇到过超时。代理挂了的话，CPA 的所有请求都会失败，排查时先看 `ss -ltnp | grep 7890`。

---

## 7. 已验证的功能（SWE-2 + Codex 0.166）

单轮 shell ✓ · apply_patch 新建和修改 ✓ · `resume --last` 多轮 ✓ · 并行工具调用 ✓ · 图片输入 ✓ · 子代理（gpt-6-luna）✓ · 联网搜索 ✓ · 切换到 gpt-6.1-sol ✓ · 上下文自动压缩（阈值 16K 时连续触发 4 次，压缩后仍记得关键信息）✓ · `/fast` 和 Lightning ✓ · opencode 的 swe-2 / swe-2#medium / swe-2-lightning ✓

推理档位映射（看 CPA 记录里的 `response_model`）：

| 请求的档位 | 上游模型 |
|-|-|
| medium / high / max | `swe-2-medium` / `-high` / `-max` |
| low | medium |
| xhigh | max |
| ultra / none / 不传 | high |

max 档的思考明显比 medium 长（同一道题，思考摘要长 2～9 倍，耗时最多 5 倍）。

速度参考（e2e）：
- SWE-2：60～120 tok/s
- Lightning：190～400 tok/s

---

## 8. 已知问题与注意事项

1. **`Linger=no`**：用户退出登录后，两个 systemd 用户服务都会停，开机也不会自启。要常驻请执行 `sudo loginctl enable-linger yuanshu`（需要管理员）。
2. **Devin 上游偶尔返回 502 "capacity issues"**，属于上游问题。CPA 有多个账号时会自动重试换号，现在只有 1 个账号。
3. **工具调用是整块返回的（burst）**，这是 Devin 的行为，Codex 里会表现为停顿几秒后一次性出结果。
4. **Fast / Lightning 是否额外扣额度不确定**。Devin CLI 的模型选择器显示 "FREE · no quota consumed"，但不确定是否也适用于 Fast Mode，请在 Devin 用量页确认。
5. 子代理第一次偶尔返回空结果，重发后正常，是 Codex 自身的行为。
6. Codex 有个开发中的功能开关 `runtime_metrics`，能在每轮结束后显示 TTFT 一类的指标，但它依赖 OpenAI 官方通过 WebSocket 回传的计时数据，接 CPA 时用不上，这也是做 cpa-stats 的原因。
7. 不要在 CPA 管理面板里保存设置，它会把整份 config.yaml 按程序的格式重写。
8. **Codex 里 SWE-2 "中途停住"（2026-11-02 排查）**：模型说一句"我先查 X"就结束这一轮，用户得再发一句"继续"。
   - 原因：Codex 模型列表里 `tool_mode: code_mode_only` 时，SWE-2 有 22%～29% 的工具调用因为没打印而返回空输出（`gpt-6-luna` 是 0%），它会怀疑环境坏了然后停下。不是 CPA 丢了工具调用：对照 CPA 请求日志，停住的那几次 Devin 返回 `status: completed`，输出 token 数和转发出的文字量对得上，没有被丢掉的工具调用。
   - 修复：把 `tool_mode` 改成 `null`。同一个模拟排查任务，停住的比例从 2/8（第一轮是 4/8）降到 0/12，空输出从 22%～29% 降到 0%（148 次 shell 调用）。apply_patch、并行工具、图片、resume、`/fast` 回归测试都通过。样本不大，偶尔仍可能出现模型自己提前收尾。
   - 改完需要重启正在运行的 Codex 才生效。
9. **每发一条消息，cpa-stats 里多出一个约 9K 输入、会话 ID 每次都不同的请求**：这是 Codex 自动生成会话标题的临时线程（请求 ID 前缀 `temporary-structured-`）。只要会话还没有名字（`threads.name` 为空），每条用户消息都会触发一次。它要求模型返回 JSON，而 Devin 协议没有 `response_format` 字段，模型只回纯文字，Codex 解析失败，会话一直没有名字，于是每次都重试。请求都是成功的，成本很小（大部分是缓存命中）。在 Codex 里用 `/rename` 给会话起名后，这个会话就不会再触发（根据源码判断，条件是 `thread_name().is_none()`，没有实测）。

10. **不要给 Devin 模型传 `temperature`**：带 `temperature: 0` 的请求上游返回 `invalid_argument`，CPA 随后把这个账号对该模型放进冷却期，之后所有正常请求都返回 `auth_unavailable`（这个原因是从现象推断的，没有单独验证）。清除冷却的方法见第 9 节的 503 一行：对 `/v0/management/auth-files` 里每个 `auth_index` 调 `POST /v0/management/reset-quota`。

### SWE-2 和 SWE-2 Lightning 是不是同一个模型（2026-11-02 指纹测试）

结论：**大概率是同一个底座模型，但是两套不同的推理部署，不能当成完全相同的东西**。

| 证据 | 结果 | 说明 |
|-|-|-|
| 分词器 | 相同 | 30 个专挑分词器差异的字符串（emoji、泰语、base64、罕见汉字、控制字符等），Lightning 的输入 token 都比 swe-2 多 38～41，几乎与内容无关 |
| 模型的"怪癖" | 相同 | "随机 1～1000 的整数"两边都偏爱 742 / 427 / 417，次数几乎一样（p=0.90）；"随便说个动物"swe-2 有 44/48、Lightning 有 46/48 说 otter |
| 能力 | 相同 | 数位和题 14/14 全对；截止知识年份都是 2024；都自称 Codeium |
| 隐藏提示词 | **不同** | 同一输入 Lightning 固定多 41 个 token。不是 CPA 加的（CPA 只传模型名），是 Devin 服务端按模型套的模板 |
| 缓存粒度 | **不同** | swe-2 的缓存命中量是 64 的整数倍（最常见 448、64）；Lightning 是 1024 的整数倍（最常见 8192、13312），所以 500 token 的小提示词在 Lightning 上不会命中缓存 |
| 速度 | **不同** | e2e 约 3.5 倍（60～120 对 190～400 tok/s） |
| 开放写作风格 | **不同** | 开头词分布 p=0.001，回答长度中位数 250 对 220 字符（p=0.002）：Lightning 更简短，措辞更不集中 |

没法确定的：这些差异来自量化、微调/蒸馏，还是仅仅因为隐藏提示词和采样参数不同；也无法证明权重逐字节相同。输入 token 数是 Devin 自己上报的，假设它来自真实分词器。附带观察：swe-2 对同一输入的 token 计数在个别内容（泰语、JSON、base64 等）上会跳动 ±3，Lightning 从不跳动，原因不明。

---

## 9. 快速排障

| 现象 | 先看 |
|-|-|
| Codex 或 opencode 全部失败 | `systemctl --user is-active cliproxy-yuanshu`；`cpa-stats doctor`；代理 7890 是否在监听 |
| 401 | `access.api-keys` 和客户端里的 key 是否一致 |
| `/v1/models` 为空 | Devin 凭据失效，按 2.5 重新登录 |
| 403 MCP configuration issue | 程序版本号是否带 `+devincodex`（`journalctl ... \| grep Version`） |
| 503 auth_unavailable | 之前的 403 触发了 30 分钟冷却，等过期，或用 `/v0/management/reset-quota` 清掉 |
| `/fast` 没效果 | `cpa-stats tail` 看 `response_model` 是否以 `-lightning` 结尾；`service_tier` 列是否为 `priority` |
| cpa-stats 面板显示 collector stale 或 error | `journalctl --user -u cpa-stats -n 50`；管理密钥是否被改过 |
| 面板没有新请求 | 采集服务是否在运行；usage-statistics-enabled 是否为 true |
