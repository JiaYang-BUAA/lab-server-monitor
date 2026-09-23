# Lab Server Monitor implementation contract

Any configured number of Windows and Linux engineering servers behind one Hub. Trusted LAN/VPN, no remote command execution or job termination through the website. stdlib Python agent/hub, static Chinese collaboration page and optional Prometheus/Grafana history. Display names are self-reported. Windows Collector uses windows_exporter and the PowerShell sidecar; LinuxCollector reads /proc, /sys and optional nvidia-smi.

## Collector module
`labmon.collector.Collector(exporter_url: str, sidecar_path: str, host_id: str)` exports `sample() -> dict`. Stateful deltas, first sample unknown. Exporter timestamps are counters/UTC. Snapshot schema:
```
{host_id, platform:'windows'|'linux', process_status:'ok'|'partial'|'error', observed_at: ISO8601 UTC, telemetry_status: 'ok'|'partial'|'error',
 cpu:{percent:number|null,logical_processors:int|null,observed_processors:int,model:str|null},
 memory:{total_bytes,available_bytes,percent},
 disks:[{name,total_bytes,free_bytes,read_bps,write_bps}],
 network:{recv_bps,sent_bps},
 gpus:[{id,name,utilization_pct,memory_used_bytes,memory_total_bytes,temperature_c,power_w}],
 processes:[{key:str,pid:int,parent_pid:int|null,name:str,owner:str|null,start_time:number|null,cpu_pct:number|null,memory_bytes:number|null,gpu_memory_bytes:number|null,software:str,role:str}],
 ssh:{tcp_connections:int|null,connections:[{remote_address,remote_port}],count_basis:'established_tcp',observed_at:str|null},warnings:[str]}
```
Process key is PID+creation timestamp, never PID alone. CPU percent denominator is all logical processors and counts all PDH core instances; expose coverage mismatch. Process CPU rate combines user+privileged. Do not export raw command lines or keys/passwords. Unknown is null, never 0. SSH count is Established TCP connections to actual sshd listening ports, not process counts/users. Sidecar JSON from persistent PowerShell loop may also supply nvidia-smi metrics, hostname and CPU model. WDDM per-process unavailable stays null. Sidecar staleness >30s means those fields unknown.

## Python server modes / config
`python -m labmon --config <json>` starts mode hub or agent. Config includes mode,listen_host,port,data_dir; agent includes host_id,token,exporter_url,sidecar_path,allowed_log_roots; hub includes servers:[{id,name,agent_url,token,enabled}],grafana_url,poll_seconds (5),stale_seconds (30). Tokens in deployment config, never web payload/logs. Agent binds LAN only when needed; Hub local agent on 127.0.0.1:8767. Hub public LAN :8766. Prometheus/Grafana/exporter remain loopback :9090/:3000/:9182. Grafana web access via hub reverse proxy /grafana/ (read-only anonymous Viewer, generated admin credential local-only).

Optional Internet mode sets `public_origin` to one exact HTTPS DNS origin and binds the Hub only to `127.0.0.1`; a separate HTTPS tunnel or reverse proxy forwards the public hostname to local Hub :8766. The Hub validates Host and HTTPS Origin, marks public session cookies Secure, and omits SSH source addresses, OS usernames, hostname, and other sessions' log paths from public state. Default LAN behavior is unchanged. Internet visitors need no account and may all edit the board; job claim editing still belongs to the creating browser session. The Internet tunnel never forwards Agent or SSH ports.

Agent routes bearer-token protected: GET /healthz, GET /snapshot, GET /metrics (unified platform-neutral gauges for metrics_format=unified; legacy Windows fixed-local-exporter proxy retained, 8MiB limit, redirects rejected); POST /estimate with {log_path,log_kind:'abaqus'|'fluent'|'comsol',total_units:number|null}. Read at most tail 256KiB from allowed roots, only approved log extensions; return metadata/estimation, never raw file. Reject traversal/reparse escape. Auto ETA requires monotonic progress with reliable wall-time samples and known end target; show unknown/warming/no-progress/completed rather than invent. Need tests for paths, restart/truncation and changing rates. Manual ETA displayed independently.

## Hub browser API
GET /healthz; GET /api/state returns {updated_at,poll_seconds,grafana_url:string|null,board:{text,revision,updated_at,updated_by},servers:[{id,name,status:'online'|'stale'|'offline'|'unconfigured',last_seen,error,snapshot:object|null,jobs:Job[],history:[{at,cpu_pct,memory_pct,gpu_pct}]}],recent_tasks:[],identity:{name:string}}.
Job = {id,host_id,name,software,state:'computing'|'running'|'idle'|'ended'|'unknown',process_count,pids:[],process_keys:[],cpu_pct,memory_bytes,started_at:ISO|null,elapsed_seconds:number|null,claim:null|{id,owner_name,task_name,expected_end:ISO|null,notes,log_path,log_kind,total_units,can_edit:boolean},estimate:null|{status,remaining_seconds,estimated_end,progress_pct,source,detail,updated_at}}.
POST /api/identity {name} stores browser identity, HttpOnly SameSite Strict random session cookie; no OS password. POST /api/claims {host_id,job_id,owner_name,task_name,expected_end:null|ISO,notes,log_path:'',log_kind:'abaqus'|'fluent'|'comsol',total_units:null|number}; returns {ok:true}. PATCH /api/claims/{id} same editable fields; DELETE /api/claims/{id} releases registration ONLY, never terminates process. Only owning browser session may edit/release a claim; return 409 for races, 403 for wrong session/origin. SQLite persists sessions/claims and changes. All mutation routes JSON and Origin/Host checked; no arbitrary fetch URL/path. Name maximum 40 chars, notes 500, task_name120. Stale data not shown as current; processes disappearing after healthy consecutive samples can be 'ended' but never auto label successful completion without log evidence.

## Page
web/index.html,app.js,styles.css, no CDN/build pipeline. Chinese, professional light/neutral blue scientific console; tabular figures, modest color, no hero. Adaptive server cards with name/ID search, live hardware/short trends, compute job table with claim/edit and estimates, expandable processes/SSH connections, unconfigured and empty-list states, Grafana link. Poll 5s, pause while hidden; stale/offline timestamp. Functional accessible modal for registration; display manual and log ETA separately. Do not replace unclaimed or unknown entries with fake examples. No kill/run/reboot controls. Local session identity from /api/identity optional; registration can send owner_name.

## Shared board
GET /api/board returns {text,revision,updated_at,updated_by}; PUT /api/board accepts {text,revision,editor_name?}, editable by any browser session. Plain text max 20000, optional editor max40. Atomic SQLite optimistic concurrency: wrong revision returns409 with latest board; valid update returns {ok:true,board}. Origin and Host checked as on other writes. Polling must never overwrite a dirty draft. Board appears above server overview and persists across service restarts.

## Platform configuration
Agent platform is auto/windows/linux; auto uses host OS. LinuxCollector(host_id) returns the same schema and uses boot identity+PID+start ticks for stable process keys. New installations use metrics_format=unified; metric names are defined by labmon/metrics.py and labmon/dashboard.py. Hub servers accept platform metadata and any number of unique IDs. grafana_url=null hides all history links. Installer allowed_log_roots defaults empty.
