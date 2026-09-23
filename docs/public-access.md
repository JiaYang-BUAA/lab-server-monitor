# 通过公网 HTTPS 访问网页

公网入口只转发 Hub 的网页端口；不要把 Agent、SSH、Prometheus 或 Grafana 端口映射到公网。下面以 Tailscale Funnel 为例：访客无需安装 Tailscale，任何能打开 HTTPS 链接的人都能查看看板、编辑共享公告和登记自己的任务。共享公告没有登录或审核；公开链接被转发后，任何人也能改写公告。

## 连接方式

```text
任意浏览器 ── HTTPS ── Tailscale Funnel ── 本机 127.0.0.1:8766 Hub
                                             │
                                             └── 内网 Agent（本机可达或经 SSH 本地转发）
```

Hub 所在的 Windows/Linux 电脑要保持开机、联网，Agent 也要可达。Funnel 只负责网页入口，不采集服务器指标；Hub 继续按现有配置轮询任意数量的 Windows/Linux Agent。若 Agent 只允许原 Hub 访问，可由管理员用 SSH 本地转发把 Agent 端口映射到 Hub 电脑的回环地址，再将 `servers[].agent_url` 指向这些本地端口；不要把 Agent token 放入网页、命令行或仓库。

## 准备 Hub

先按 [部署说明](deployment.md)安装并验证内网 Agent。公网 Hub 可以单独运行，不需要在这台电脑安装本机 Agent、Prometheus 或 Grafana。安装 Python 3.10+，将源码包放到固定目录，在该目录创建仅管理员可读的 `config/hub.json` 和 `data/hub`。下面是结构示例；实际 Agent token 只写在本机私有配置里：

```json
{
  "mode": "hub",
  "listen_host": "127.0.0.1",
  "port": 8766,
  "data_dir": "E:/lab-monitor/data/hub",
  "web_dir": "E:/lab-monitor/web",
  "grafana_url": null,
  "poll_seconds": 5,
  "stale_seconds": 30,
  "servers": [
    {"id": "compute-01", "name": "计算服务器 01", "platform": "linux", "agent_url": "http://127.0.0.1:18767", "token": "从 Agent 私密配置取得", "enabled": true}
  ]
}
```

运行 `python -m labmon --config config/hub.json`，在 Hub 电脑访问 `http://127.0.0.1:8766/`，确认服务器状态实时更新。正式运行时，应把 Hub 与需要的 SSH 转发配置为重启后自动启动。保留原 Hub 的公告和登记时，先停止旧 Hub 写入，再用 SQLite backup API 迁移其 `labmon.sqlite3`；不要直接复制正在写入的 SQLite 主文件，也不要同时运行两个可编辑 Hub。

## 开通 Funnel

1. 在 [Tailscale 官网](https://tailscale.com/download)安装并登录 Hub 电脑。Windows MSI 支持 `INSTALLDIR`，可将程序安装到 E:；Windows 服务及状态数据仍会按系统要求写入系统目录。开启 Windows 的 Run Unattended，避免退出登录后断开。
2. 运行 `tailscale funnel --bg 8766`。按浏览器提示在自己的 Tailscale 管理界面批准 Funnel，记下命令输出的 `https://<设备>.<tailnet>.ts.net` 地址。浏览网页的人不用登录 Tailscale。
3. 在 Hub 私有的 `config/hub.json` 中加入 `"public_origin": "https://<设备>.<tailnet>.ts.net"`，保持 `listen_host` 为 `127.0.0.1`，然后重启 Hub。`public_origin` 必须与 Funnel 给出的域名完全一致，不要加路径或端口。
4. 用手机蜂窝网络访问这个 HTTPS 地址，检查首页、`/api/state`、公告保存和刷新后的内容；随后检查 `tailscale funnel status`。本机 `http://127.0.0.1:8766/` 仍可用于管理员诊断。

Hub 在公网模式下拒绝绑定非回环地址；公网请求还会校验准确的 HTTPS Origin，并给浏览器会话 Cookie 加 `Secure`。公网 API 隐藏 SSH 来源 IP、系统账号、主机名及其他人填写的日志路径，但会公开服务器名称、资源负载、计算进程、登记姓名/备注和 SSH 连接数。请勿把私密内容写入共享公告或任务备注。

Funnel 的公网名称在设备保持同一 tailnet 身份时稳定；电脑或网络离线时网页无法实时访问。Funnel 目前有带宽限制，适合这个轻量看板，不适合作为大文件传输入口。如果改用其他 HTTPS 反向代理，也必须只代理本机 Hub、保留公网 `Host` 和浏览器 `Origin`，并把 `public_origin` 配成实际的 HTTPS 域名。
