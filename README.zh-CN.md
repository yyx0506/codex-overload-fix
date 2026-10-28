# Codex 满载修复 —— 通用版（universal edition）

> **本项目是一个 fork。** 原项目是
> [**NewBeeM4n/codex-overload-fix**](https://github.com/NewBeeM4n/codex-overload-fix)（MIT）。
> 核心诊断、「只在对内容提交前重试」的安全规则、单文件中继设计全部来自原作者。
> 本通用版让同一个中继支持 **两种部署形态**、**三大平台**：
>
> - **直连模式（direct）** —— Codex → 中继 → `chatgpt.com`，用你自己的账号（原版用法）。
> - **网关模式（gateway）** —— Codex → 中继 → **sub2api / cockpit-tools 等账号池网关**。
> - **Windows、macOS（M 系列和 Intel）、Linux 全支持。**
>
> 网关相关的改进最初来自
> [codex-overload-fix-cockpit](https://github.com/aiyinsitan520/codex-overload-fix-cockpit)，
> 已在本仓库合并并注明出处（见 [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md)）。
> 如果觉得有用，请先去给上游仓库点 star。

一个本地重试中继，让 **「Selected model is at capacity. Please try a different model.」** 不再打断你干活。

> **先说清楚它治不了什么。**
>
> 它不能修复模型降智，也不会绕过任何配额或付费。它重试的只是一种瞬时的准入拒绝
> —— 那种藏在 HTTP 200 SSE 流里的 `error` 事件 —— 让你和你的团队都看不到它。
> 回滚只需删除 `config.toml` 里的一行。

原版中继一晚上的实测数据：

| | 请求数 | 吸收的重试 | 用户看到的失败 |
|---|---|---|---|
| 不装中继 | — | — | 约每 2 轮被打断一次 |
| 装上中继 | 71 | 82 | **0** |

---

## 一屏看懂问题

满载报错**不是** HTTP 5xx，它藏在 **200 的 SSE 流**里：

```json
{"type":"error","sequence_number":3,
 "error":{"type":"service_unavailable_error",
          "code":"server_is_overloaded",
          "message":"Our servers are currently overloaded. Please try again later."}}
```

任何按状态码重试的层都看不到它。而且拒绝率随**请求体大小**陡增：500 KB 以下平均
约 2 次尝试；超过后平均 6.8 次（实测最差：一个 799 KB 请求连败 8 次后第 9 次成功）。
详见 [docs/findings.md](docs/findings.md)（英文）。

在账号池网关（sub2api、cockpit-tools 等）后面，同样的拒绝还会以别的形态出现
—— 整池冷却的 503、被网关改写的 `config.toml`、JSON 错误体 —— 见
[docs/gateway-notes.md](docs/gateway-notes.md)。

---

## 安装

纯 Python 3.9+ 标准库。零依赖、不需要管理员权限、没有编译产物 —— 这也是同一份代码
能在 Windows、macOS（M 系列 / Intel）和 Linux 上跑的原因。

<details open>
<summary><b>Windows</b></summary>

```powershell
# 直连模式（自己的账号）：
powershell -ExecutionPolicy Bypass -File scripts\install.ps1

# 网关模式（sub2api / cockpit-tools / 任意网关）：
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Mode Gateway -GatewayUrl http://127.0.0.1:8888
```

安装脚本会找到 Python、拒绝占用中的端口、把隐藏启动器 + 看门狗写入
`%LOCALAPPDATA%\codex-overload-fix`、注册到 `HKCU\...\Run`，并打印你所用模式对应的
`config.toml` 片段。卸载：`scripts\uninstall.ps1`。
</details>

<details open>
<summary><b>macOS —— M 系列和 Intel 通用</b></summary>

```bash
# 直连模式：
bash scripts/install-mac.sh

# 网关模式（sub2api / cockpit-tools / 任意网关）：
bash scripts/install-mac.sh --mode gateway --upstream http://127.0.0.1:8888
```

注册为用户级 LaunchAgent（launchd 自动重启，无需看门狗）。中继是纯 Python，
M 系列和 Intel 走的是完全相同的代码路径，任何 Python 3.9+ 都可以。
卸载：`bash scripts/uninstall-mac.sh`。
</details>

<details open>
<summary><b>Linux</b></summary>

```bash
bash scripts/install-linux.sh                                            # 直连
bash scripts/install-linux.sh --mode gateway --upstream http://127.0.0.1:8888
```

注册为 systemd `--user` 服务。卸载：`scripts/uninstall-linux.sh`。
</details>

然后把打印出来的片段粘进 `~/.codex/config.toml`（或参考
[config/config.example.toml](config/config.example.toml) /
[config/gateway.example.toml](config/gateway.example.toml)），完全退出并重启 Codex。

### 两种模式的 config.toml

**直连模式** —— 复用 Codex 已有的 ChatGPT OAuth 登录：

```toml
model_provider = "openai-retry"

[model_providers.openai-retry]
name = "OpenAI (retry)"
base_url = "http://127.0.0.1:8787"
wire_api = "responses"
requires_openai_auth = true
stream_max_retries = 20
request_max_retries = 10
```

**网关模式** —— Codex 用环境变量里的网关 API key 认证（中继不存储、不查看它）：

```toml
model_provider = "gateway-retry"

[model_providers.gateway-retry]
name = "Gateway (retry)"
base_url = "http://127.0.0.1:8788/v1"
wire_api = "responses"
env_key = "SUB2API_API_KEY"
stream_max_retries = 20
request_max_retries = 10
```

```bash
export SUB2API_API_KEY="sk-...你的网关key..."        # macOS/Linux
# [Environment]::SetEnvironmentVariable('SUB2API_API_KEY','sk-...','User')   # Windows
```

**公司团队共用一个网关**时，建议把中继集中部署在网关旁边：这样
`--max-concurrent` 和 `--retries-per-sec` 约束的是全公司的重试压力，
保护共享账号池。详见 [docs/gateway-notes.md](docs/gateway-notes.md)。

---

## 中继做了什么

```
Codex ──► 127.0.0.1:8787/8788 (relay.py) ──► chatgpt.com            (--mode direct)
                                      └──► sub2api/cockpit-tools    (--mode gateway)
```

全程字节级透明 —— 请求体（含 zstd 压缩）、`Authorization`、会话亲和头全部原样转发。
只检查 SSE 流开头几个事件来判断这次尝试是否成功。

安全规则：

1. **只在对内容提交前重试。** 一旦出现真实内容（`output_text.delta`、图像事件等），
   流原样直通 —— 永远不会重复生成。前置事件（`response.created`、
   `output_item.added`）只会「扣住」流头；真正的过载恰好发生在它们之后。
2. **按失败类型分配预算。** 过载：30 次 / 300 秒，带退避和抖动。上游挂了：
   3 次 / 15 秒 —— 网关没启动时几秒内报错，而不是让应用「假思考」五分钟。
3. **进程级闸门。** `--max-concurrent`（信号量）+ `--retries-per-sec`（令牌桶）
   限制并发尝试数和全局重试速率，重试层自己不会变成它想吸收的踩踏。
4. **感知客户端退出。** 退避睡眠期间轮询客户端 socket；取消的回合立即停止消耗配额。
5. **懂网关。** 微秒级返回的 503 被识别为整池冷却并等待（`--cooling-latency`，
   网关模式默认 0.2 秒，直连模式关闭）；`--upstream` 尾部 `/v1` 自动归一化
   （不会出现 `/v1/v1` 404）；非流式 JSON 响应原样透传。
6. **诚实的放弃。** 预算耗尽返回 `503` + `Retry-After` + JSON 错误 ——
   绝不返回挂着错误 Content-Type 的 200。

### 可观测性

```
curl http://127.0.0.1:8788/__relay/health     # JSON 计数：requests、retries、
                                               # overload events、giveups、mode...
python tools/relayctl.py status               # 同样的数据，格式化输出 + 结论
python tools/relayctl.py path                 # 中继真的在流量路径里吗？
python tools/relayctl.py probe                # 发一个极小请求，统计尝试次数
```

`path` 是最要紧的检查：会在切换账号时改写 `config.toml` 的网关，
可能让一个完全健康的中继被静默绕过。

### 测试

```
python tests/run_tests.py        # 49 项断言：吸收、不重复生成边界、图像提交、
                                 # JSON 处理、冷却、放弃形态、上游失联快速失败、双模式
```

CI 在 Ubuntu、macOS（Intel + ARM runner）和 Windows 上跑同一套测试：
[.github/workflows/tests.yml](.github/workflows/tests.yml)。

---

## 局限

- 它重试的是准入拒绝；不能让降智的模型变好，也不能提升任何配额。
- 根因随请求体大小增长。偶尔新开对话（并精简不用的 skills）是修复的另一半。
- 如果你的网关自己已经解析并重试 in-band 过载事件，这个中继就是重复兜底 ——
  加层之前先确认。
- 不共享账号、不绕过配额、不逆向协议。中继只是原样转发 Codex 已经在发的东西。

## 致谢

- [NewBeeM4n/codex-overload-fix](https://github.com/NewBeeM4n/codex-overload-fix) ——
  原始诊断、设计和实测数据（MIT）。
- [aiyinsitan520/codex-overload-fix-cockpit](https://github.com/aiyinsitan520/codex-overload-fix-cockpit) ——
  本仓库合并的网关模式改进（MIT）。

完整变更清单：[ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md)。English: [README.md](README.md)。
