"""init 生成的配置模板：Windows 家用节点 + Tailscale + VPS 反向隧道。

主机名全是占位符，用前改成自己 ~/.ssh/config 里的别名：
- minipc-ts   经 Tailscale 直连节点的 ssh 别名
- minipc-vps  经 VPS 反向隧道连节点的 ssh 别名（ProxyJump 到 VPS，再连 127.0.0.1:隧道端口）
- vps.example.com  VPS 公网地址
"""

SSH = "ssh -o BatchMode=yes -o ConnectTimeout=6"
PS_UPTIME = (
    '"powershell -NoProfile -Command '
    "[int]((Get-Date)-(Get-CimInstance Win32_OperatingSystem).LastBootUpTime).TotalSeconds\""
)

TEMPLATE = {
    "node": "home-minipc",
    "timeout_s": 10,
    "reboot_tolerance_s": 180,
    "escalate_after": 3,
    "tailscale_bin": "tailscale",
    "notify": "macos",
    "notify_command": "",
    "channels": [
        {"name": "local", "label": "本机网络", "kind": "tcp", "role": "control",
         "host": "example.com", "port": 443},
        {"name": "vps", "label": "VPS", "kind": "tcp", "role": "control",
         "host": "vps.example.com", "port": 22, "requires": ["local"]},
        {"name": "tailscale", "label": "Tailscale", "kind": "tailscale", "role": "path",
         "host": "minipc", "requires": ["local"]},
        {"name": "tunnel", "label": "反向隧道", "kind": "cmd", "role": "path",
         "command": f"{SSH} minipc-vps echo lifeline-ok", "expect": "lifeline-ok",
         "requires": ["vps"]},
        {"name": "ssh", "label": "Tailscale SSH", "kind": "cmd", "role": "service",
         "command": f"{SSH} minipc-ts echo lifeline-ok", "expect": "lifeline-ok",
         "requires": ["tailscale"]},
        {"name": "egress", "label": "家宽出口", "kind": "cmd", "role": "service",
         "command": f"{SSH} minipc-ts curl.exe -s -m 6 https://example.com -o NUL -w %{{http_code}}",
         "expect": "200", "requires": ["ssh"]},
    ],
    "uptime": [
        {"via": "ssh", "command": f"{SSH} minipc-ts {PS_UPTIME}"},
        {"via": "tunnel", "command": f"{SSH} minipc-vps {PS_UPTIME}"},
    ],
    "repairs": [
        {"target": "local", "via": "*", "steps": [
            "这是你这边的问题，不是家里节点的：检查本机网络和代理是否把探测流量拦了"]},
        {"target": "vps", "via": "*", "steps": [
            "登录 VPS 服务商网页控制台，看实例是否在运行",
            "必要时从控制台重启实例；重启后反向隧道由节点侧自动重连"]},
        {"target": "tailscale", "via": "tunnel", "steps": [
            "ssh minipc-vps",
            "powershell: Restart-Service Tailscale",
            "tailscale status 确认已登录；如显示需要登录，执行 tailscale up --unattended 并在浏览器授权"],
         "note": "Tailscale 密钥过期也会表现为断开，去管理后台关掉该节点的密钥过期"},
        {"target": "tunnel", "via": "tailscale", "steps": [
            "ssh minipc-ts",
            "powershell: Get-ScheduledTask | Where TaskName -like '*tunnel*' 找到隧道任务",
            "Start-ScheduledTask -TaskName <隧道任务名>，或重启承载隧道的服务",
            "在 VPS 上确认隧道端口已监听"]},
        {"target": "ssh", "via": "tunnel", "steps": [
            "ssh minipc-vps",
            "powershell: Restart-Service sshd"]},
        {"target": "egress", "via": "ssh", "steps": [
            "ssh minipc-ts",
            "powershell: Get-NetAdapter 看网卡状态，ipconfig /flushdns",
            "Test-NetConnection example.com -Port 443 判断是 DNS 还是家宽断了"],
         "note": "如果 Tailscale 也同时变成 DERP 中继，多半是家宽本身不稳"},
        {"target": "egress", "via": "tunnel", "steps": [
            "ssh minipc-vps，按同样步骤排查家宽出口"]},
    ],
    "onsite": [
        "断电后没有自动开机：按电源键（并让人顺手在 BIOS 打开来电自启）",
        "光猫或路由器死机：拔电 30 秒再插上",
        "Windows 卡在更新、BitLocker 恢复或蓝屏界面：需要有人看屏幕操作",
        "网线松动或网卡硬件故障：重新插拔网线",
    ],
}
