# 部署与维护

每个被监测的系统运行一个 Agent，一个 Hub 汇总这些 Agent 并提供网页。Hub 所在服务器可以同时运行本机 Agent。新增服务器只需给它唯一 `host_id` 并在 Hub 的 `servers` 列表接入；Windows 和 Linux 可以混合使用。不需要每人安装客户端。

## Windows

将完整源码解压到专用目录，例如 `E:\WJY\lab-server-monitor`，在管理员 PowerShell 中进入该目录。需要 64 位 Windows 10/Server 2019 或更新版本，以下地址均为示例，必须换成实际内网地址。

```powershell
# 首台服务器，安装网页、采集和历史趋势
.\scripts\install.ps1 -Role hub -Address 192.168.50.10 `
  -HostId compute-01 -HostName '计算服务器 01' -WebAllowedFrom 192.168.50.0/24

# 其他被监测服务器，各自使用不同 HostId
.\scripts\install.ps1 -Role agent -Address 192.168.50.11 `
  -HostId compute-02 -HubAddress 192.168.50.10
```

安装器从官方地址下载锁定版本，校验 SHA-256 后安装。外部依赖约数百 MB，首次需要联网；已准备好 `downloads` 中的相同校验文件时可加 `-Offline`。`-PrepareOnly` 会下载、解压和生成配置，但不会安装/启动服务或添加防火墙规则。监测运行资产留在指定目录，SSH 系统组件使用系统规定位置。

## Linux

当前自动安装目标为 Ubuntu 22.04+/Debian 12+、Python 3.10+、systemd 247+。其他发行版可参考配置手动部署，但未承诺自动安装兼容。

```bash
sudo bash scripts/install-linux.sh --role hub --address 192.168.50.10 \
  --host-id compute-01 --name '计算服务器 01' --allow-from 192.168.50.0/24

sudo bash scripts/install-linux.sh --role agent --address 192.168.50.11 \
  --host-id compute-02 --hub-address 192.168.50.10
```

默认为 `/opt/lab-server-monitor`，可用 `--root` 修改。基础安装提供实时监测、任务登记和共享公告；不会自动安装 Prometheus/Grafana。详细权限、预览、systemd 与可选历史配置见 [Linux 安装](linux-install.md)。

## 将额外服务器接入 Hub

通过可信文件传输，把目标 Agent 的 `config/private.json`（其中含 `agent_token`）复制到 Hub 管理员专用目录。也可使用只含该 Agent token 的私密文本文件。不要把令牌粘贴到网页公告或公开仓库。运行：

```text
python scripts/connect-server.py --root <Hub安装目录> --id compute-02 --name "计算服务器 02" --platform linux --address 192.168.50.11 --token-file <私密令牌文件>
```

Windows 安装版的 Python 路径是 `runtime\python\python.exe`；Linux 使用 `python3`。**未配置 Prometheus 时加 `--without-history`**。自定义 Agent 端口使用 `--port`。脚本验证目标身份、平台和采集状态，备份后写配置，不会自动重启服务。重复运行相同参数不会重复添加服务器。当前接入助手接受 RFC1918 内网 IPv4；更复杂的 DNS、HTTPS 或 IPv6 地址需要管理员按 `CONTRACT.md` 手动设置 Hub 配置并核验网络。

接入后在 Hub 执行 `Restart-Service LabMonWeb`（Windows）或 `sudo systemctl restart labmon-web`（Linux）。启用了历史采集时还要重载 Prometheus；Windows 服务依赖下需先停止 `LabMonGrafana`，重启 `LabMonPrometheus`，再启动 `LabMonGrafana`。不要停止求解器或重启整台服务器。

网页默认在 `http://<Hub地址>:8766/`，远程 Agent 为 8767。Windows exporter/Prometheus/Grafana 仅监听回环地址 9182/9090/3000。新建规则将 Agent 入站限定为 Hub 来源、网页限定为指定成员网段；既有宽泛放行规则仍然有效，安装器不会收紧这些规则。Windows 重跑时保留同名规则，变更来源需要管理员明确修改现有规则。网络、防火墙和云安全组仍需允许这条路径。

## 备份、升级与恢复

先备份 `config` 和 `data/hub/labmon.sqlite3`，备份目录同样限制访问。运行时数据库用 SQLite backup API 备份，或先停止 Hub 再复制；不能只复制写入中的 SQLite 主文件而忽略 WAL。历史趋势位于 `data/prometheus`，Grafana 数据位于 `data/grafana`。

升级前保留代码和配置快照。停相应监测服务后更新代码，保留已有 `config`、`data`、令牌和成员登记，再启动并检查 `/healthz` 以及网页采样时间。Windows 安装器保留已有 Agent/Hub 配置，不会自动迁移旧指标格式；从旧版升级统一历史指标需将 Agent 的 `metrics_format` 设为 `unified`，把 Prometheus 目标改为带 token 的 Agent 8767，并用 `scripts/render-dashboard.py` 生成新版 Grafana dashboard。原始历史序列保留在数据库，新指标从迁移时开始记录。

共享公告随 Hub SQLite 持久保存，所有访问者可修改。若恢复旧备份，将恢复该时刻的公告与登记，恢复前应保留当前数据库以便找回后续修改。
