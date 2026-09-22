# SSH 配置代码包

把 `scripts/setup-ssh.ps1` 或 `scripts/setup-ssh.sh` 与你自己电脑的 **公钥 `.pub` 文件**带到服务器，就能为一个已有账号配置 SSH 密钥登录。代码包不含账号、地址、实验室公钥或任何私钥。SSH 配置与监测 Agent 安装相互独立；安装 SSH 不会自动安装监测服务。

## 支持范围

| 系统 | 本脚本支持 | 需要人工处理 |
|---|---|---|
| Windows | Windows 10 1809+/11、Server 2019+ 的官方 OpenSSH Server 可选组件；64 位 PowerShell 5.1/7；已有本地账号 | 域/Entra 账号、第三方 SSH 服务、嵌套管理员组、自定义服务命令行、自定义 AuthorizedKeysFile |
| Linux | 使用 systemd 的 Debian/Ubuntu，及提供 dnf 的发行版；预装 Python 3；已有非 root 本地账号及其正常 home | 容器、WSL、非 systemd 系统、网络目录账号、自定义启动参数/授权文件、符号链接 home、特殊挂载或访问控制 |

当前只接受**一条标准 `ssh-ed25519` 公钥**，支持末尾备注。拒绝私钥、多条公钥、`authorized_keys` 前置选项、损坏的 base64 和错误的密钥包结构。已有 RSA/ECDSA 等其他密钥会保留；不需要删除或替换它们。如果系统处于禁用 Ed25519 的 FIPS 模式，请按本地管理要求手动配置，本脚本不绕过该策略。

Windows 脚本使用 SID 识别本地 Administrators 组并设置 ACL，适用于不同系统语言。管理员公钥写入 `%ProgramData%\ssh\administrators_authorized_keys`，仅 Administrators 与 SYSTEM 具有权限，所有者为 Administrators。**这是管理员组共用授权文件，同一公钥可能用于其他本地管理员账号登录**；如需账号隔离，使用标准用户或由管理员设计独立授权策略。普通用户写入实际用户配置目录下的 `.ssh\authorized_keys`；先至少登录一次该账号，以创建配置目录。

Linux 使用账号数据库里的 home，检查路径组件、文件类型、所有者和权限，拒绝符号链接与硬链接授权文件；通过目录文件描述符打开授权文件，保留已有内容，设置 `.ssh` 为 `0700`、`authorized_keys` 为 `0600`，所有者为目标用户。不创建用户，不更改 home 本身的权限。

解压独立 SSH 包后进入 `scripts` 文件夹再执行下文命令；或者只把对应脚本复制到服务器上的工作目录。

## 1. 在管理电脑准备公钥

已有 `~/.ssh/id_ed25519.pub` 时可直接使用。没有时，在自己的电脑执行：

```text
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_labmonitor
```

提示输入 passphrase 时可为私钥设置口令。将生成的 **`id_ed25519_labmonitor.pub`** 传到目标服务器；保留不带 `.pub` 的私钥在自己的电脑上。可以使用已有的可信 SFTP/SSH 连接，或在服务器本地通过 U 盘复制。首次尚无 SSH 时，不依赖 SSH 自身传文件。

## 2. 在 Windows 服务器预览并执行

使用“以管理员身份运行”的 **64 位 PowerShell**。把下面的 `researcher`、文件路径和示例来源地址换成实际值。示例地址是文档保留地址，不能直接照抄连接。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup-ssh.ps1 `
  -UserName researcher -PublicKeyFile .\id_ed25519_labmonitor.pub `
  -AllowFrom 192.0.2.10 -DryRun

# 确认预览的用户、授权文件及来源正确后，移除 -DryRun 再执行。
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup-ssh.ps1 `
  -UserName researcher -PublicKeyFile .\id_ed25519_labmonitor.pub `
  -AllowFrom 192.0.2.10
```

允许多个来源时，在 PowerShell 中直接调用脚本并传数组：

```powershell
.\setup-ssh.ps1 -UserName researcher -PublicKeyFile .\id_ed25519_labmonitor.pub `
  -AllowFrom @('192.0.2.10', '198.51.100.0/24') -DryRun
```

`-ExecutionPolicy Bypass` 只对本次 PowerShell 进程生效，不更改整机执行策略。OpenSSH 是 Windows 系统组件，安装到系统目录，不会放入监测项目目录。组件源、Windows Update/WSUS 或权限错误会明确报错；不会自行替换为网上下载的可执行文件，也不会自动重启整机。

## 3. 在 Linux 服务器预览并执行

```bash
sudo bash ./setup-ssh.sh --user researcher \
  --public-key ./id_ed25519_labmonitor.pub \
  --allow-from 192.0.2.10 --dry-run

# 确认预览后，移除 --dry-run。
sudo bash ./setup-ssh.sh --user researcher \
  --public-key ./id_ed25519_labmonitor.pub \
  --allow-from 192.0.2.10
```

多个来源可重复 `--allow-from`；支持单个 IPv4/IPv6 地址和 CIDR。拒绝 `Any`、`0.0.0.0/0`、`::/0`、未指定地址及组播地址。脚本使用系统配置的软件包仓库安装 `openssh-server`。Debian/Ubuntu 首次安装期间临时使用 `policy-rc.d` 阻止包安装自动启动服务，完成校验后才显式启动；如已有该文件，则保留它并停止自动安装，交由管理员按原有软件包策略安装后重试。

如果终端或机器在 apt 安装期间被强制终止，可能来不及清理临时 `/usr/sbin/policy-rc.d`。只有确认它的完整内容恰好是以下三行时，才删除这个本脚本临时文件后继续；其他内容属于既有配置，不能删除：

```sh
#!/bin/sh
# temporary Lab Monitor SSH package-start guard
exit 101
```

## 4. 核对指纹并测试新连接

脚本输出主机名、配置/实际监听端口、Ed25519 主机指纹和连接示例。在客户端首次连接时，通过服务器本地终端显示的指纹核对身份，核对一致后再信任。不要为了连接成功关闭主机密钥验证；指纹变化需要先查明原因。

```text
ssh -i ~/.ssh/id_ed25519_labmonitor -p 22 researcher@服务器地址
```

`22` 应替换为脚本报告的实际端口。保持原会话打开，在**新终端**验证实际登录成功后再关闭旧会话。脚本启动成功只证明本机设置/监听状态，不代表客户端登录成功。

脚本不会改写既有 `sshd_config`，不禁用密码、不放开 root、不更改自定义端口。已有 `AllowUsers`、`DenyUsers`、`AuthenticationMethods`、`Match`、账号过期/锁定等策略继续生效，可能仍阻止登录。只新增授权文件内容时，sshd 会在下次登录读取，因此已有运行服务无需重启。Linux 已启用的 `ssh.socket` 保持原样，防火墙使用它实际监听的端口。

## 防火墙与网络边界

- Windows 新增名称为 `LabMonitor-SSH-<摘要>` 的受限来源规则；已有同名规则不改写。首次安装组件自动创建的宽泛默认规则会被禁用；安装前已存在的规则会保留。
- Linux 仅向**已开启**的 UFW 或 firewalld 添加受限来源规则，不 flush 规则、不自动开启/关闭防火墙。firewalld 只修改默认 zone，需核对接收连接的网卡是否归属该 zone；其他 zone 由管理员处理。不执行全局 reload，以免清掉其他运行时规则。
- **新增一条受限允许规则不能收窄其他既有允许规则。** 默认允许入站、已有全网 SSH 规则、云安全组或其他规则仍可能放行其他来源。脚本会提示这一点，不声称实现“只有这些来源能连接”。
- 没有已开启的受支持防火墙时，会明确报告 `FIREWALL NOT CONFIGURED`，继续保留原防火墙状态；需要自行处理 nftables/iptables 或外部安全组。
- 来源应填写服务器实际看到的客户端地址；经过 VPN/NAT 时可能与电脑网卡地址不同。脚本不修改路由、NAT、端口映射、云安全组或校园网策略，**不会让内网服务器自动具备公网可达性**。

## 备份与回退

执行时保留终端输出，记录新增防火墙规则、原服务启动状态及备份路径。脚本不提供“一键卸载 SSH”，因为 SSH 可能同时服务其他用户。

1. 授权文件修改前，会生成 `authorized_keys.labmon-before-<时间>` 一类备份；Windows 还保存原 ACL 的 SDDL 文本，普通用户目录 ACL 同样保存。回退时先比较当前文件，**仅移除本次新增的那条公钥**。有其他人后来新增密钥时，不要整文件覆盖。已有相同公钥时，本次不重复追加，也不移除其原有 `command=`/`from=` 等限制。
2. 仅删除本次确实新增的防火墙规则。Windows 可按终端输出的精确名称使用 `Remove-NetFirewallRule -Name '<规则名>'`；UFW 使用 `sudo ufw status numbered` 核对来源、端口和 `lab-monitor-ssh` 注释，再删对应条目。firewalld 按输出的 zone 和完整 rich rule，分别删除运行时与永久规则；不要删执行前已存在的相同规则。
3. 原文件权限确需恢复时，先确认不会破坏 SSH 安全要求。Linux 根据终端记录的 owner/mode 恢复；Windows 可将对应 `.acl.txt` 中的 SDDL 通过 `SetSecurityDescriptorSddlForm` / `Set-Acl` 恢复到**原文件**。
4. 只有确认服务为本次新装且没有其他使用者时，才考虑停用/卸载 OpenSSH。不要删除整个 `.ssh`、`%ProgramData%\ssh` 或 `/etc/ssh`，其中可能有其他人的密钥、主机身份和原配置。脚本不更换已有主机私钥。

## 验证边界与依据

本代码包在 Windows 开发机进行了 PowerShell 5.1 语法解析、纯参数/密钥验证测试，以及 Linux 内嵌 Python 的编译与参数校验。另有 Linux 文件安全与保留已有授权测试，需要 POSIX 的 openat/chown 支持，Windows 测试时会明确跳过。`--dry-run` 不写文件、不装包、不更改服务或防火墙。**尚未在干净 Windows 或 Linux 虚拟机上执行完整安装流程；不应把这些自动测试当作跨发行版实机验证。** 发布前建议在可回滚的 Ubuntu/Debian 和目标 Windows 测试机中，分别验证首次安装、已有非 22 端口、重复运行、受限来源和第二客户端实际登录。

实现依据：[Microsoft Windows OpenSSH 配置](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh-server-configuration)、[Microsoft 密钥管理](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_keymanagement)、[Ubuntu OpenSSH Server](https://ubuntu.com/server/docs/how-to/security/openssh-server/)、[OpenSSH sshd 手册](https://man.openbsd.org/sshd)、[Ubuntu UFW 手册](https://manpages.ubuntu.com/manpages/noble/man8/ufw.8.html)。项目自有代码按 MIT 许可证发布；系统软件仍遵循各自许可证。
