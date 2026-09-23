# 仿真提交前的只读负载检查

Hub 提供 `GET /api/capacity`。Codex 或其他自动化程序可以在连接计算服务器、启动新仿真前，通过公网 HTTPS 读取当前采样。这个接口无需浏览器会话、不写数据库、不执行服务器命令，也不会替用户预留资源。

先估计任务实际需要的逻辑 CPU 核数、内存 GiB，以及单块 GPU 显存 GiB（如不使用 GPU，填 0）。例如：

```powershell
python scripts/check-capacity.py --url https://<hub>.ts.net --cpu-cores 32 --memory-gb 64
python scripts/check-capacity.py --url https://<hub>.ts.net --host-id compute-01 --cpu-cores 16 --memory-gb 48 --gpu-vram-gb 12 --json
```

可直接请求 `GET /api/capacity?host_id=compute-01&cpu_cores=16&memory_gb=48&gpu_vram_gb=12`；省略 `host_id` 返回所有服务器。响应含采样时间和时效、CPU 使用率与估算可用核数、可用内存、逐卡 GPU 估算显存、活跃任务/登记数量，以及 `verdict`：

- `likely_available`：当前采样满足所填需求，并保留 CPU 约 10%（至少 2 核）、内存 5%（至少 4 GiB）、单卡显存 5%（至少 1 GiB）的余量。
- `insufficient`：至少一项所填需求明确超出当前估算余量。
- `unknown`：服务器离线、采样过期或必要指标缺失；不能当作空闲。
- `not_assessed`：没有提供资源需求，只返回指标。

脚本退出码：`0` 至少一台当前估算有余量，`2` 全部明确不足，`3` 无法判断，`4` 网络或接口错误。API 只反映瞬时测量，间歇性负载、未来预约、求解器许可证、磁盘 I/O 和任务实际峰值可能改变可用性。启动前还要查看网页公告/任务登记，与组内预约核对；在真正执行仿真命令前再读一次，不把接口判断当成资源锁。

建议给会启动仿真的 Codex 工作区加入如下约定：先取得本次任务的 CPU、内存和 GPU 需求，运行上面的只读检查；仅在结果 `likely_available` 且没有冲突预约时继续准备提交。若结果为 `unknown`、`insufficient` 或需求不明，先说明证据并询问用户如何安排，不擅自启动计算。普通 SSH 登录、只读诊断不需要此预检。
