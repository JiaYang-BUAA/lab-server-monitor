# Linux 安装与接入

Linux 入口安装基础看板或单独的采集 Agent。目标环境为 Ubuntu 22.04+、Debian 12+ 等使用 systemd 的服务器，要求 Python 3.10+、systemd 247+、Bash 和 sudo 权限。只使用 Python 标准库，不需要 pip、Node.js、Windows exporter、Docker 或 WSL。

当前安装包不会修改 SSH、重启服务器或操作计算进程。SSH 引导脚本与监测安装相互独立。基础安装**不包含 Prometheus/Grafana 历史服务**，看板仍显示实时采样及本次服务运行期间的近期趋势；历史入口默认隐藏。需要持久化历史时另行配置 Prometheus/Grafana，并采集 Agent `/metrics` 提供的统一指标。

## 准备

将完整的开源源码包解压到任意工作目录，先阅读脚本。所有示例 IP 都需要替换为自己的内网地址；当前安装入口使用 RFC1918 IPv4。源包不包含现有实验室的配置、令牌和登记数据。

先检查：

```bash
python3 --version
systemctl --version
```

若 Python 缺失，请通过系统正常的软件安装流程安装 Python 3.10+。安装器不会自动升级系统 Python。NVIDIA 指标需要服务器已有可工作的驱动和 `nvidia-smi`，安装器不安装驱动。缺少 GPU 或采集权限时应显示未知/采集提示，不虚构为 0。

## 一台 Linux 服务器承载看板

在源码包目录执行，先预览再安装：

```bash
sudo bash scripts/install-linux.sh --role hub \
  --address 192.168.1.10 --host-id compute-01 --name "计算服务器 1" \
  --allow-from 192.168.1.0/24 --dry-run

sudo bash scripts/install-linux.sh --role hub \
  --address 192.168.1.10 --host-id compute-01 --name "计算服务器 1" \
  --allow-from 192.168.1.0/24
```

安装后从允许的网络访问 `http://192.168.1.10:8766/`。本机 Agent 只监听 `127.0.0.1:8767`，Hub 的初始服务器列表只有这一台。可以继续添加任意数量的 Windows/Linux Agent。

## Linux 服务器接入已有看板

在这台 Linux 计算服务器上执行：

```bash
sudo bash scripts/install-linux.sh --role agent \
  --address 192.168.1.11 --host-id compute-02 \
  --hub-address 192.168.1.10
```

Agent 监听指定网卡的 `8767` 端口，并生成独立随机令牌。通过已有的可信 SSH/SFTP 通道把 `/opt/lab-server-monitor/config/private.json` 传给 Hub 的受保护目录，再在 Hub 使用 `scripts/connect-server.py` 接入，指定此 Agent 的 ID、地址和令牌文件。不要把令牌粘贴进聊天、命令行参数、Git 仓库或网页公告中。该文件只能由管理员读取；Hub 必须能够访问 Agent 的 8767 端口。

## 日志目录、安装路径与防火墙

- 默认路径 `/opt/lab-server-monitor`，可用 `--root /srv/lab-monitor` 修改。为避免 systemd 路径解释差异，安装根目录不能含空格、特殊控制字符或符号链接。
- Hub 服务使用 `ProtectHome=true`：安装根目录和 Python 解释器（包括符号链接的真实目标）不能位于 `/home`、`/root` 或 `/run/user` 下，否则预检会拒绝。请使用 `/opt`、`/srv` 等服务目录和系统 Python；解压源码的临时工作目录可以位于个人 home。
- Hub 的 `PrivateTmp=true` 还会遮蔽 `/tmp` 和 `/var/tmp`。真正安装及其 `--dry-run` 会拒绝这些位置中的安装根目录/解释器（含真实目标）；`--prepare-only` 可以在临时目录生成供检查的文件，但这些路径不能直接作为持久运行服务的安装位置。
- 日志读取默认关闭，即 `allowed_log_roots=[]`。需要估算时显式添加 `--allow-log-root /srv/calculations`；多个目录重复该参数，不允许把整个 `/` 作为日志根。
- Agent 的防火墙来源是单个 `--hub-address`；Hub 的网页来源是必填的 `--allow-from CIDR`。安装器仅在 **UFW 已启用** 时添加该精确来源到指定网卡、端口的 TCP 规则。
- 若 UFW 未安装、未启用或使用其他防火墙，安装器明确报告 `FIREWALL NOT CONFIGURED`，不会启用、关闭或清空防火墙。请由管理员在现有防火墙/网络策略中配置相同范围。安装器保留原有规则，既有的宽泛放行规则仍然生效；新增精确规则不会收紧那些规则。
- 看板面向可信组内网络，成员可共同编辑公告。不要直接暴露在公网；跨网络访问使用实验室 VPN 等受控网络。

UFW 的来源与端口规则用法参考 [Ubuntu Server 防火墙文档](https://documentation.ubuntu.com/server/how-to/security/firewalls/index.html)。

## 服务身份与权限

`labmon-agent.service` 使用 root，以读取完整的 `/proc` 进程和 SSH 连接信息。Agent 对计算系统仅进行读取；日志估算限于管理员列出的目录，不提供执行命令、启动或杀死计算进程的 HTTP 接口。

`labmon-web.service` 使用独立的不可交互登录用户 `labmon`。配置和令牌目录为 `0700`、文件为 `0600`，所有者为 root；systemd 使用 `LoadCredential` 将 Hub 配置提供给此服务，避免为了运行网页而放宽原始配置权限。仅 `data/hub` 由 `labmon` 拥有。systemd 服务对程序目录只读，Hub 可写自己的数据库目录，Agent 可写自己的运行数据目录。

`LoadCredential` 与 `%d` 的用法参考 [systemd 官方凭据文档](https://systemd.io/CREDENTIALS/)。部署到更旧的 systemd 环境前需要自行适配，不应直接去掉权限限制。

## 重跑、升级与维护

重复使用相同参数运行安装器会保留令牌、配置、公告和任务登记。改变角色、ID、监听地址或防火墙来源会拒绝覆盖，先由管理员完成明确的迁移方案。现有 `allowed_log_roots` 在省略参数时保留；传入与现有值不同的目录会停止，需明确编辑私有配置。

升级时从新的解压目录运行安装器。只复制 `labmon/*.py`、指定网页/安装脚本与文档，不复制源包的 `config`、`data`、下载包或日志。发生变化的已有源码/服务文件先备份到 `backups/<时间>-install` 或 `backups/<时间>-units`；相同文件不重复备份。安装器不会自动清理旧源码；需要删除弃用模块时应单独审查。

```bash
sudo systemctl status labmon-agent.service labmon-web.service
sudo journalctl -u labmon-agent.service -u labmon-web.service -n 100 --no-pager
```

Hub 依赖本机 Agent。更新两者或重启 Agent 时顺序必须为：

```bash
sudo systemctl stop labmon-web.service
sudo systemctl restart labmon-agent.service
sudo systemctl start labmon-web.service
```

只改 Hub 配置或添加远端 Agent 后，执行 `sudo systemctl restart labmon-web.service`；systemd 会重新加载私有凭据。独立 Agent 只需 `sudo systemctl restart labmon-agent.service`。改动 systemd 单元文件后先执行 `sudo systemctl daemon-reload`。

如需回退，停止对应监测服务，将本次备份中需要恢复的源码/单元复制回原路径，执行 `daemon-reload` 后按上述顺序启动。不要删除或覆盖 `data/hub/labmon.sqlite3`，其中保存共享公告与登记。服务启动失败时安装器会保留配置和备份并报告错误；不会重启机器或改变求解任务。

## 仅准备与验证边界

`install-linux.sh --prepare-only` 在 Linux 上复制源码并生成配置，**不**创建服务用户、配置防火墙或启动服务。`--dry-run` 只校验，不写文件。Python 配置器本身始终只生成文件，可在 Windows 的临时目录运行：

```text
python scripts/configure-linux.py --root <绝对临时目录> --role agent --address 192.168.1.11 --host-id test-linux --hub-address 192.168.1.10 --prepare-only
```

Windows 仅用于参数、配置结构、幂等与语法测试，不能证明 POSIX 权限和 systemd 已运行。发布前建议 CI 至少包含 Ubuntu 22.04/24.04 与 Debian 12/13 的真实 Linux VM：

1. `python3 -m unittest discover -s tests`、`bash -n scripts/install-linux.sh`、`systemd-analyze verify services/*.service`。
2. 在隔离 VM 安装 Hub/Agent，确认服务用户、`600/700` 权限、SSH 配置未变与本机监听地址。
3. 实际读取 `/snapshot`，验证进程、全机 CPU、SSH、无 GPU 时的未知状态，以及 Hub 页面出现 Linux 节点。
4. 从允许与不允许的来源测试 UFW 已启用时的访问；另测 UFW 禁用时仍保持禁用且安装器准确报告未配置。
5. 写入测试公告和登记，重跑/升级后验证数据与令牌保留，模拟配置冲突确认无部分覆盖。

本地 Windows 的准备测试与 Bash 语法检查不能替代以上 Linux 系统验收。
