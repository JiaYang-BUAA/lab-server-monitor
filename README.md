# Lab Monitor · 实验室服务器监测

[![GitHub stars](https://img.shields.io/github/stars/JiaYang-BUAA/lab-server-monitor?style=flat)](https://github.com/JiaYang-BUAA/lab-server-monitor/stargazers)
[![Cross-platform checks](https://github.com/JiaYang-BUAA/lab-server-monitor/actions/workflows/test.yml/badge.svg)](https://github.com/JiaYang-BUAA/lab-server-monitor/actions/workflows/test.yml)

共享服务器的硬件状态、计算任务、SSH 连接和使用安排看板。Windows 与 Linux 可混合接入，服务器数量由配置决定。

- 顶部共享公告：所有成员可查看、编辑；纯文本换行、更新时间、署名，并发修改冲突提示。
- 实时 CPU、内存、磁盘、网络及可用 NVIDIA GPU 指标；未知数据明确显示未知。
- 每台服务器的资源、计算任务和 SSH 连接连续显示。任务行可从共享姓名列表选择成员、添加姓名，并直接填写共享备注；详细登记仍可设置人工预计结束时间。
- 可选从 Abaqus、Fluent、COMSOL 日志估算剩余时间；无可靠进度时不编造 ETA。
- 服务器卡片自适应排列，可搜索名称或 ID；SSH 数量是已建立 TCP 连接数，不等于人数。
- Windows 完整安装含 Prometheus/Grafana 历史趋势；Linux 基础安装含实时网页与协作，历史服务可自行接入。

## 快速开始

在 [Releases](https://github.com/JiaYang-BUAA/lab-server-monitor/releases) 下载完整源码包或独立 SSH 配置包。首版为预发布版本，Linux 真实服务器上的安装与登录仍需验收。

下载源码包并解压，在专用目录中按照 [部署说明](docs/deployment.md) 安装第一台 Hub，再为每台额外服务器安装 Agent。需要让任意网络的浏览器访问时，按 [公网 HTTPS 部署](docs/public-access.md) 把 Hub 放在回环地址后，通过 Tailscale Funnel 提供公网入口。Linux 细节见 [Linux 安装](docs/linux-install.md)。Windows 安装器可下载并校验固定版本的官方依赖，Python 应用本身仅使用标准库。

自动化仿真提交可先调用只读 `/api/capacity` 或 [Codex 预检脚本](docs/codex-preflight.md)，按任务的 CPU、内存、GPU 需求查看当前余量。服务器名称可在网页自定义并持久保存；同机的多条独立进程任务可人工归成一条后一次认领。共享公告按换行分段显示每段的编辑时间与姓名，旧版内容会注明缺少逐段历史。

已有 SSH 不需要重新配置。尚未配置时，把 SSH 代码包和自己的公钥带到服务器，运行对应平台脚本；可先 dry-run。详见 [SSH 配置说明](docs/ssh-setup.md)。SSH 安装与监测安装相互独立。

安装后访问 `http://<Hub地址>:8766/`。从任何受允许的浏览器点击顶部“编辑公告”，填写使用计划后保存；其他成员下一次自动刷新便可看到。任务姓名和备注也允许所有访问者修改；任务名称、预计结束时间、日志配置和释放登记仍由原登记浏览器管理。修改同一任务发生冲突时，页面提示刷新核对。

## 数据与使用范围

默认适合可信内网/VPN。网页未实现账户认证，姓名是自报信息；所有能访问网页的人都可编辑公告。配置公网 HTTPS 入口后，任何获得链接的人也能查看和修改公告；公网 API 会隐藏 SSH 来源 IP、系统账号、主机名和他人的日志路径。CPU 以整机全部逻辑处理器为分母，低 CPU 不等于任务完成；任务结束也不等于计算成功。人工预计和日志估算分别显示。未授权任何网页停止或执行计算功能。

Linux 自动安装的初始目标是 Ubuntu 22.04+/Debian 12+、Python 3.10+、systemd 247+；其它系统需自行适配。支持情况与实测边界见 [验证说明](docs/release-validation.md)、[Linux 指标口径](docs/linux-collector.md) 和 [访问边界](docs/security.md)。

## 开发与打包

```text
python -m unittest discover -s tests -v
node --check web/app.js
python scripts/build-release.py
```

生成的 `dist/lab-server-monitor-source.zip` 为完整源码包，`dist/lab-monitor-ssh-setup.zip` 为独立 SSH 配置包。包内不含运行配置、令牌、数据库、私钥、实验室地址或第三方二进制。不要直接压缩整个运行目录发布。安装器不会自动上传或发布仓库。

应用/API 契约见 [CONTRACT.md](CONTRACT.md)。本项目代码采用 [MIT](LICENSE) 许可证，允许使用、修改和再分发，需保留版权和许可声明；外部依赖继续遵循其各自许可证。

制作者：**+羊 · Codex**。
