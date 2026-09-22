# Linux 采集定义与限制

`labmon.linux_collector.LinuxCollector(host_id)` 与 Windows Collector 返回同一快照结构，并附带 `platform: "linux"`。仅依赖 Python 3.10+ 标准库，读取 `/proc`、`/sys` 和本地文件系统信息；可选运行只读 `nvidia-smi` 查询。不会启动、停止或修改计算任务，也不会读取或返回进程命令行、环境变量。

## 运行范围

在 Linux 宿主机上运行 Agent。为读取其他账号的进程可执行文件、sshd 的 socket 文件描述符，通常需要 root 或相应系统权限。缺失权限时保留可读指标，返回中文警告；不把未知写成零。

容器中隔离的 PID、网络、挂载命名空间不代表整台宿主机。本版不宣称支持容器内整机监控。`hidepid` 会使进程清单不完整，即使读取没有报错，也会禁止自动判定任务结束。`proc_root`、`sys_root` 和时钟/文件系统/命令执行注入主要用于测试。

## 指标口径

- **CPU**：使用 `/proc/stat` 的整机累计值，核对 `/sys/devices/system/cpu/online` 与所有 `cpuN` 行。占用率为两次采样间的忙碌时间占比；`idle + iowait` 视为空闲。`guest` 和 `guest_nice` 已包含在 user/nice 中，不再重复相加。首次采样、CPU 热插拔、覆盖不完整、计数器回退时返回 `null`。内核说明 iowait 可能回退，因此该情况重新建立基线。
- **进程 CPU**：`utime + stime` 的增量除以实际经过秒数和全机在线逻辑处理器数。一条线程占满一台 192 线程机器时约为 0.52%。不累加子进程 CPU，以免父子重复计算。CPU 数变化后重新预热。
- **进程身份**：`PID + boot_id + start_ticks`，boot_id 无法读取时可用已读取的 btime 作为启动标识。显示的创建时间为 `btime + start_ticks / SC_CLK_TCK`；内核 btime 是整秒，进程相对启动时间的分辨率为一个时钟 tick。它不是可保证微秒准确度的墙钟时间。扫描前后再次读 stat，拒绝 PID 在扫描过程中复用导致的混合记录。
- **进程完整性**：扫描中退出、身份变化、权限受限或目录不可读会令 `process_status = "error"`。保留本次有效记录，但不允许这次不完整清单作为任务结束证据。GPU 查询失败不影响进程完整性。已明确进入 Z 状态的僵尸进程已退出，不保留为占用任务；任务结束仍不代表计算成功。用户名无法解析时显示数字 UID，不隐藏对应进程。
- **内存**：整机使用 `MemTotal`、`MemAvailable`。缺失 MemAvailable 时不拿 MemFree 冒充可用内存。进程使用 VmRSS，必要时退回 stat 的 RSS 页数；RSS 是近似驻留内存，共享页可能被多个进程重复计算，不等于任务独占内存。
- **磁盘**：读取 mountinfo 中常见本地文件系统，按 major:minor 去重 bind mount，选择最短挂载路径显示；不探测 NFS、SMB、tmpfs。容量来自 statvfs，可用容量使用普通用户可用的 `f_bavail`。读写速率仅在挂载设备能对应 diskstats 的 major:minor 时计算，扇区单位固定为 512 字节；ZFS 等无法唯一映射的情况返回 `null`。这是挂载块设备的 I/O，不是某个目录的 I/O。首次或重置后速率未知。
- **网络**：累加 `/proc/net/dev` 除 `lo` 外的接口字节增量。包括虚拟接口，复杂桥接/隧道拓扑可能重复计量同一份流量；当前指标表示接口流量之和，不保证是物理网卡唯一流量。
- **SSH**：从 sshd/sshd-session/sshd-auth 进程的 fd 确定监听 socket inode，再匹配 `/proc/net/tcp` 与 tcp6 中相同监听地址和本地端口的 Established 连接。支持 IPv4、IPv6、非 22 端口，不把 sshd 进程数当连接数，也不把向外建立的 SSH 客户端连接计入。未完成认证的 Established TCP 也可能计入，因此不是登录人数。一个复用的 TCP 连接可能承载多个终端。无法核验权限、进程可见范围或监听 socket 时为 `null`。此版识别 OpenSSH，不宣称支持 Dropbear 或容器内 SSH 服务。
- **GPU**：NVIDIA 工具可用时显示整卡负载、显存、温度、功耗；单次查询最多等待 3 秒，驱动返回 N/A 的字段保留 `null`。未安装工具不等于没有 GPU。AMD/Intel GPU 与逐进程 GPU 显存暂未实现，并在快照警告中说明。

## 验证

`python -m unittest tests.test_linux_collector -v` 的 procfs fixture 测试可在 Windows 执行，覆盖 CPU 核数/guest/iowait、PID 复用与扫描竞态、权限及 hidepid、IPv4/IPv6 非默认端口 SSH、磁盘去重、未知值、GPU 超时和敏感字段隔离。fixture 通过不代表已经在真实 Linux 发行版上部署通过；上线时仍需在目标宿主机核对系统服务、真实硬件读数、sshd 和网络可达性。

实现参考 Linux 官方文档：[procfs](https://www.kernel.org/doc/html/latest/filesystems/proc.html)、[TCP 表](https://www.kernel.org/doc/html/latest/networking/proc_net_tcp.html)、[块设备 I/O 统计](https://www.kernel.org/doc/html/latest/admin-guide/iostats.html)。
