# homenode-lifeline

给一台放在远方、没人看着的家用节点（比如留在海外家里的 Mini-PC）当生命线监控。

它定时回答四个问题：

1. 每条远控通道现在通不通（Tailscale 直连、VPS 反向隧道、SSH、家宽出口）
2. 节点开机多久了，中间有没有断过电
3. 某条通道断了，从哪条还活着的通道去修，具体敲什么
4. 所有通道都断了时，哪些事只能找人到现场做

纯标准库，Python 3.10+，没有第三方依赖。

## 快速体验

```bash
python3 -m homenode_lifeline demo
```

离线回放一段故事：正常、Tailscale 掉线、家里停电、来电自启、修好、你这边断网。不碰任何网络。

## 接到自己的节点上

```bash
python3 -m homenode_lifeline init      # 生成 ~/.homenode-lifeline/config.json
# 编辑配置，把 vps.example.com、minipc、minipc-ts、minipc-vps 改成自己的
python3 -m homenode_lifeline check     # 探一轮
python3 -m homenode_lifeline onsite    # 看修复矩阵和现场清单
```

模板假设你在 `~/.ssh/config` 里有两个别名，并且都配了免密钥登录：

```
Host minipc-ts            # 经 Tailscale
  HostName minipc
  User me
Host minipc-vps           # 经 VPS 反向隧道
  HostName 127.0.0.1
  Port 22222
  User me
  ProxyJump vps.example.com
```

## 常驻

```bash
python3 -m homenode_lifeline watch --interval 300
```

只在状态变化、发现重启、或某通道连续断满 `escalate_after` 轮时才通知，平时安静。Mac 上可以用 `examples/com.example.homenode-lifeline.plist` 交给 launchd 开机自启。

也可以不常驻，用 cron 每 5 分钟跑一次：

```
*/5 * * * * cd /path/to/homenode-lifeline && python3 -m homenode_lifeline check --notify >/dev/null
```

`check` 的退出码：0 正常，1 部分异常，2 失联，3 观测端自己断网，4 配置错误。

## 看历史

```bash
python3 -m homenode_lifeline history --hours 72
```

每条通道的可用率、重启记录、最近事件。数据存在 `~/.homenode-lifeline/lifeline.db`（SQLite）。

## 配置说明

通道有三种角色：

| role | 含义 | 挂了意味着 |
| --- | --- | --- |
| control | 观测前提，如本机网络、VPS 本身 | 依赖它的通道本轮不探，标记为被连累 |
| path | 到节点的远控通道 | 只要还有一条 path 活着，就能远程修 |
| service | 节点上的服务，如 SSH、家宽出口 | 节点还在，只是某个功能坏了 |

探针类型：`tcp`（host + port）、`cmd`（任意命令，可选 `expect` 子串）、`tailscale`（`tailscale ping`，能分辨直连还是 DERP 中继）。

`requires` 声明依赖：前置通道不通就不探，避免一个根因刷出一堆告警。

`uptime` 按顺序尝试，从第一条活着的通道读开机时长。命令输出可以是秒数、ISO 开机时间戳或 `3 days, 04:05:06`。开机时刻比上次记录晚超过 `reboot_tolerance_s` 秒就算重启；如果中间节点失联过，会提示很可能是断电后来电自启。失联后恢复但开机时刻没变，会提示只是断网、没断电。

`repairs` 是修复预案：`target` 断了时，如果 `via` 那条通道活着，就把 `steps` 列出来。`via` 写 `*` 表示不依赖任何通道（例如去 VPS 服务商网页控制台）。

`notify` 设为 `macos` 用系统通知；设 `notify_command` 可以推到手机，标题和正文在环境变量 `LIFELINE_TITLE`、`LIFELINE_BODY` 里：

```json
"notify_command": "curl -s -d \"$LIFELINE_BODY\" -H \"Title: $LIFELINE_TITLE\" https://ntfy.example.com/my-topic"
```

## 整体判定

- 正常：全部通道都通
- 部分异常：有通道挂了，但还有远控通道活着，按修复路线处理
- 失联：所有远控通道都挂了。如果是被上游连累（比如 VPS 和 Tailscale 同时断），先修上游；否则先等 10 到 30 分钟，再考虑找人到现场
- 无法判断：你这边自己断网了，不算节点故障

## 测试

```bash
python3 -m pytest -q
```
