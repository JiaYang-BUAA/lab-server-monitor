'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const icons = {
    server: '<rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6.5h.01M7 17.5h.01M12 6.5h5M12 17.5h5"/>',
    cpu: '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 1v5m6-5v5M9 18v5m6-5v5M1 9h5m-5 6h5M18 9h5m-5 6h5"/><rect x="9" y="9" width="6" height="6"/>',
    memory: '<rect x="3" y="6" width="18" height="12" rx="1"/><path d="M7 10v4m5-4v4m5-4v4M6 18v3m4-3v3m4-3v3m4-3v3"/>',
    gpu: '<rect x="3" y="5" width="18" height="13" rx="2"/><circle cx="10" cy="11.5" r="3.5"/><path d="M16 9h2m-2 4h2M7 18v3m4-3v3m4-3v3"/>',
    terminal: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 8 3 4-3 4m6 0h4"/>',
    box: '<path d="m12 3 9 5v9l-9 5-9-5V8l9-5Zm0 9 9-4m-9 4L3 8m9 4v10"/>',
  };
  const svg = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.box}</svg>`;
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const numeric = (value) => typeof value === 'number' && Number.isFinite(value);
  const percent = (value) => numeric(value) ? `${value.toFixed(1)}%` : '—';
  const clamp = (value) => Math.max(0, Math.min(100, value));
  const validDate = (value) => value != null && value !== '' && Number.isFinite(new Date(value).getTime());
  const clockTime = (value) => validDate(value) ? new Date(value).toLocaleTimeString('zh-CN', {hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '尚无采样';
  const fullTime = (value) => validDate(value) ? new Date(value).toLocaleString('zh-CN', {hour12:false,month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) : '未知';
  const count = (value) => numeric(value) ? value.toLocaleString('zh-CN') : '—';
  function bytes(value) {
    if (!numeric(value) || value < 0) return '—';
    if (value === 0) return '0 B';
    const units = ['B','KiB','MiB','GiB','TiB'];
    const index = Math.min(4, Math.floor(Math.log(value) / Math.log(1024)));
    return `${(value / 1024 ** index).toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
  }
  function duration(seconds) {
    if (!numeric(seconds) || seconds < 0) return '未知';
    if (seconds < 60) return '不足 1 分钟';
    const minutes = Math.floor(seconds / 60), hours = Math.floor(minutes / 60), days = Math.floor(hours / 24);
    if (days) return `${days} 天 ${hours % 24} 小时`;
    if (hours) return `${hours} 小时 ${minutes % 60} 分钟`;
    return `${minutes} 分钟`;
  }
  const statusLabels = {online:'在线',stale:'数据过期',offline:'离线',unconfigured:'待接入'};
  const jobLabels = {computing:'计算中',running:'运行中',idle:'低占用',ended:'进程已结束',unknown:'状态未知'};
  let state = null, selectedId = null, filter = 'all', query = '', fetching = false;
  let timer = null, toastTimer = null, latestError = '', lastFetched = 0, editTarget = null, submitting = false;
  let serverQuery = '', boardLatest = null, boardEditing = false, boardBaseRevision = null, boardSaving = false;
  const expanded = new Set();
  const server = () => state?.servers?.find((item) => item.id === selectedId);
  const effectiveStatus = (item) => latestError && item.status === 'online' ? 'stale' : item.status;
  const canClaim = (item) => effectiveStatus(item) === 'online';
  const statusBadge = (status, label) => `<span class="badge ${esc(status)}">${status === 'online' ? '<span class="status-dot"></span>' : ''}${esc(label || statusLabels[status] || status)}</span>`;
  // SVG geometry stays dynamic under the page's CSP, which rejects inline styles.
  const meter = (value, color = '') => `<div class="meter ${esc(color)} ${numeric(value) ? '' : 'unknown'}" aria-hidden="true"><svg viewBox="0 0 100 4" preserveAspectRatio="none" focusable="false"><rect width="${numeric(value) ? clamp(value) : 0}" height="4"/></svg></div>`;
  const empty = (title, detail = '', icon = 'box') => `<div class="empty-state">${svg(icon)}<strong>${esc(title)}</strong>${esc(detail)}</div>`;

  async function api(path, method = 'GET', body) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(path, {method,credentials:'same-origin',cache:'no-store',headers:body === undefined ? {} : {'Content-Type':'application/json'},body:body === undefined ? undefined : JSON.stringify(body),signal:controller.signal});
      let payload = {};
      try { payload = await response.json(); } catch (_) { /* Surface a meaningful HTTP error below. */ }
      if (!response.ok) {
        const messages = {403:'此浏览器没有这条登记的编辑权限，或请求来源未获允许。',404:'任务或登记已不存在，请刷新后重试。',409:'任务登记已发生变化，请刷新后查看最新状态。',429:'操作过于频繁，请稍后重试。'};
        const detail = typeof payload.error === 'string' ? payload.error : typeof payload.message === 'string' ? payload.message : '';
        const boardMessages = {403:'请求来源未获允许，请从看板页面重新尝试。',409:'公告已被其他成员修改，请先核对最新版。'};
        const error = new Error((path === '/api/board' ? boardMessages[response.status] : messages[response.status]) || (detail ? `提交未完成：${detail}` : `服务暂时不可用（HTTP ${response.status}），请稍后重试。`));
        error.status = response.status; error.payload = payload;
        throw error;
      }
      return payload;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('监测服务响应超时，请检查网络后重试。');
      if (error instanceof TypeError) throw new Error('无法连接监测服务，请确认设备处于可访问服务器的网络。');
      throw error;
    } finally { clearTimeout(timeout); }
  }

  function schedulePoll() {
    clearTimeout(timer);
    if (!document.hidden) timer = setTimeout(refresh, 5000);
  }
  async function refresh() {
    if (fetching || document.hidden) return;
    fetching = true;
    $('refresh-button').disabled = true;
    try {
      const data = await api('/api/state');
      if (!Array.isArray(data.servers)) throw new Error('监测服务返回的数据格式不完整，请稍后重试。');
      state = data; lastFetched = Date.now(); latestError = '';
      if (!data.servers.some((item) => item.id === selectedId)) selectedId = data.servers.find((item) => item.status !== 'unconfigured')?.id || data.servers[0]?.id;
      render();
    } catch (error) {
      latestError = error.message;
      if (state) render();
      else {
        $('board-text').textContent = '暂时无法读取共享公告，连接恢复后自动重试。';
        $('server-list').innerHTML = empty('暂时无法读取服务器', '连接恢复后每 5 秒自动重试。', 'server');
        $('hardware-content').innerHTML = empty('没有可用的监测数据', '不会使用演示数据替代真实采样。');
        $('task-content').innerHTML = empty('任务状态未知', '请等待监测服务恢复。');
        $('ssh-content').innerHTML = empty('连接数未知', '请等待监测服务恢复。', 'terminal');
      }
    } finally {
      fetching = false; $('refresh-button').disabled = false;
      renderRefreshStatus(); schedulePoll();
    }
  }
  function renderRefreshStatus() {
    $('connection-banner').hidden = !latestError;
    $('connection-banner').textContent = latestError ? `${latestError}${state ? ' 当前展示上次取得的数据，不能视为实时占用。' : ''}` : '';
    $('refresh-state').textContent = document.hidden ? '页面已隐藏，暂停刷新' : latestError ? '连接中断 · 自动重试' : lastFetched ? `每 5 秒刷新 · ${clockTime(lastFetched)}` : '正在连接监测服务…';
  }
  function preserveFocus(callback) {
    const focused = document.activeElement;
    const focusKey = focused?.getAttribute('data-focus-key');
    callback();
    if (focusKey && !focused.isConnected) {
      const replacement = [...document.querySelectorAll('[data-focus-key]')].find((item) => item.getAttribute('data-focus-key') === focusKey);
      replacement?.focus({preventScroll:true});
    }
  }
  function render() {
    acceptBoard(state.board);
    renderHistoryLinks();
    preserveFocus(() => {
      renderServers(); renderHardware(); renderJobs(); renderSSH();
    });
    $('identity-name').textContent = state.identity?.name || '设置我的姓名';
    renderRefreshStatus();
  }
  function renderHistoryLinks() {
    let href = null;
    if (typeof state.grafana_url === 'string' && state.grafana_url.trim()) {
      try {
        const url = new URL(state.grafana_url, window.location.href);
        if (['http:', 'https:'].includes(url.protocol)) href = url.href;
      } catch (_) { /* An invalid history URL is not an actionable link. */ }
    }
    document.querySelectorAll('.grafana-link').forEach((link) => {
      link.hidden = !href;
      if (href) link.href = href;
      else link.removeAttribute('href');
    });
  }
  const boardMetadata = (board) => board?.updated_at ? `最后修改：${board.updated_by || '未署名'} · ${fullTime(board.updated_at)}` : '尚无修改记录';
  function boardStatus(message) {
    if ($('board-status').textContent !== message) $('board-status').textContent = message;
  }
  function acceptBoard(board) {
    if (!board || typeof board.text !== 'string' || !Number.isInteger(board.revision) || board.revision < 0) return;
    // A poll started before a save must not replace a newer response with an older revision.
    if (boardLatest && board.revision < boardLatest.revision) return;
    boardLatest = board;
    renderBoard();
  }
  function renderBoard() {
    if (!boardLatest) return;
    $('board-edit').disabled = boardSaving;
    $('board-edit').hidden = boardEditing;
    $('board-view').hidden = boardEditing;
    $('board-form').hidden = !boardEditing;
    $('board-text').textContent = boardLatest.text || '还没有公告。可以在这里写下资源安排、占用时间或需要大家知道的备注。';
    $('board-text').classList.toggle('board-empty', !boardLatest.text);
    $('board-meta').textContent = boardMetadata(boardLatest);
    const conflict = boardEditing && boardLatest.revision !== boardBaseRevision;
    $('board-conflict').hidden = !conflict;
    if (conflict) {
      const message = '公告已有新版本。你的草稿尚未保存，请先核对修改。';
      if ($('board-conflict-message').textContent !== message) $('board-conflict-message').textContent = message;
      $('board-latest-meta').textContent = `最新版 · ${boardMetadata(boardLatest)}`;
      $('board-latest-text').textContent = boardLatest.text || '（最新版公告为空）';
    }
    $('board-save').disabled = boardSaving || conflict;
    $('board-save').textContent = boardSaving ? '正在保存…' : '保存公告';
    ['board-cancel','board-reviewed','board-load-latest'].forEach((id) => $(id).disabled = boardSaving);
    $('board-draft').readOnly = boardSaving;
    $('board-editor-name').readOnly = boardSaving;
  }
  function boardCharacterCount() {
    $('board-character-count').textContent = `${$('board-draft').value.length.toLocaleString('zh-CN')} / 20,000`;
  }
  function openBoard() {
    if (!boardLatest || boardSaving) return;
    boardEditing = true; boardBaseRevision = boardLatest.revision;
    $('board-draft').value = boardLatest.text;
    $('board-editor-name').value = state?.identity?.name || '';
    $('board-error').hidden = true;
    boardStatus('正在编辑；自动刷新会保留你的草稿。');
    boardCharacterCount(); renderBoard(); $('board-draft').focus();
  }
  function cancelBoard() {
    if (boardSaving) return;
    boardEditing = false; boardBaseRevision = null;
    $('board-error').hidden = true;
    boardStatus('已取消编辑，显示当前公告。');
    renderBoard(); $('board-edit').focus();
  }
  function resolveBoardConflict(loadLatest) {
    if (!boardLatest || !boardEditing || boardSaving) return;
    if (loadLatest) $('board-draft').value = boardLatest.text;
    boardBaseRevision = boardLatest.revision;
    $('board-error').hidden = true;
    boardStatus(loadLatest ? '已载入最新版。可以继续编辑后保存。' : '已确认合并。请检查草稿，再保存公告。');
    boardCharacterCount(); renderBoard(); $('board-draft').focus();
  }
  async function saveBoard(event) {
    event.preventDefault();
    if (boardSaving || !boardEditing || !boardLatest) return;
    if (boardLatest.revision !== boardBaseRevision) {
      renderBoard(); $('board-reviewed').focus(); return;
    }
    if (!$('board-form').reportValidity()) return;
    const text = $('board-draft').value, editorName = $('board-editor-name').value.trim();
    if (text.length > 20000 || editorName.length > 40) {
      formError('board-error', new Error('公告最多 20,000 字符，修改者姓名最多 40 字符。')); return;
    }
    boardSaving = true; $('board-error').hidden = true; renderBoard();
    try {
      const data = await api('/api/board', 'PUT', {text,revision:boardBaseRevision,editor_name:editorName});
      if (!data.ok || !data.board || typeof data.board.text !== 'string' || !Number.isInteger(data.board.revision)) throw new Error('服务器未确认保存结果，草稿已保留，请刷新后核对。');
      acceptBoard(data.board);
      boardEditing = false; boardBaseRevision = null;
      boardStatus('公告已保存，所有成员将在自动刷新时看到更新。');
    } catch (error) {
      if (error.status === 409 && error.payload?.board) acceptBoard(error.payload.board);
      formError('board-error', error);
    } finally {
      boardSaving = false; renderBoard();
      if (!boardEditing) $('board-edit').focus();
    }
  }
  function platformLabel(item) {
    const platform = String(item.snapshot?.platform || item.platform || '').toLowerCase();
    return platform === 'windows' ? 'Windows' : platform === 'linux' ? 'Linux' : '平台待识别';
  }
  function renderServers() {
    const total = state.servers.length;
    const needle = serverQuery.trim().toLocaleLowerCase();
    const visible = state.servers.filter((item) => `${item.name} ${item.id}`.toLocaleLowerCase().includes(needle));
    $('server-count').textContent = String(total);
    $('server-summary').textContent = total ? `${state.servers.filter((item) => effectiveStatus(item) === 'online').length} 台在线 · 点击服务器查看资源、任务和连接` : '尚未配置服务器';
    $('server-search-clear').hidden = !serverQuery;
    $('server-search-status').hidden = !needle;
    const searchStatus = needle ? `找到 ${visible.length} / ${total} 台服务器${server() ? ` · 当前查看：${server().name}` : ''}` : '';
    if ($('server-search-status').textContent !== searchStatus) $('server-search-status').textContent = searchStatus;
    if (!total) {
      $('server-list').innerHTML = empty('还没有配置服务器','添加服务器连接后，资源、计算任务和 SSH 状态会显示在这里。','server'); return;
    }
    if (!visible.length) {
      $('server-list').innerHTML = empty('没有匹配的服务器','请调整名称或 ID 关键词，或清除搜索。','server'); return;
    }
    $('server-list').innerHTML = visible.map((item) => {
      const s = item.snapshot, status = effectiveStatus(item), pending = status === 'unconfigured';
      const jobs = (item.jobs || []).filter((j) => j.state !== 'ended');
      return `<button type="button" class="server-card ${item.id === selectedId ? 'selected' : ''} ${pending ? 'pending' : ''}" data-server="${esc(item.id)}" data-focus-key="server-${esc(item.id)}" aria-pressed="${item.id === selectedId}"><div class="server-card-heading"><span class="server-icon">${svg('server')}</span><div class="server-card-title"><h3>${esc(item.name)}</h3><div class="server-id">${esc(item.id)}</div></div>${statusBadge(status)}</div><div class="server-platform-row"><span class="platform-badge">${platformLabel(item)}</span>${numeric(s?.cpu?.logical_processors) ? `<span>${count(s.cpu.logical_processors)} 逻辑处理器</span>` : ''}</div>${pending ? '<div class="pending-copy"><strong>尚未配置监测连接</strong>接入后将显示硬件、计算任务和 SSH 状态。</div>' : `<div class="server-card-body"><div><div class="mini-label"><span>CPU${status !== 'online' ? ' · 上次采样' : ''}</span><b>${percent(s?.cpu?.percent)}</b></div>${meter(s?.cpu?.percent)}</div><div><div class="mini-label"><span>内存</span><b>${percent(s?.memory?.percent)}</b></div>${meter(s?.memory?.percent,'teal')}</div></div>`}<div class="server-card-footer"><span>${pending ? '等待配置' : s ? `${jobs.length} 个计算任务 · ${count(s.ssh?.tcp_connections)} 个 SSH 连接` : '尚未获得有效采样'}</span><span>${pending ? '—' : item.id === selectedId ? '当前查看' : '查看详情 →'}</span></div></button>`;
    }).join('');
  }
  function metricCard(label, icon, value, unit, detail, valuePercent, color = '') {
    return `<article class="metric-card"><div class="metric-title">${esc(label)}${svg(icon)}</div><div class="metric-value">${esc(value)}${unit ? `<small>${esc(unit)}</small>` : ''}</div><p class="metric-detail">${esc(detail)}</p>${valuePercent !== undefined ? meter(valuePercent,color) : ''}</article>`;
  }
  function renderHardware() {
    const item = server();
    if (!item) {
      $('hardware-title').textContent = '资源监测'; $('hardware-subtitle').textContent = '等待接入服务器'; $('sample-time').textContent = '';
      $('hardware-content').innerHTML = `<div class="panel">${empty('没有可查看的服务器','接入服务器后显示硬件状态。','server')}</div>`; return;
    }
    const s = item.snapshot, status = effectiveStatus(item), isFresh = status === 'online';
    $('hardware-title').textContent = `${item.name} · 资源监测`;
    $('hardware-subtitle').textContent = status === 'unconfigured' ? '待接入后显示真实数据' : s?.cpu?.model || '监测数据来自服务器采样';
    $('sample-time').textContent = s ? `${isFresh ? '采样' : '上次采样'} ${fullTime(s.observed_at || item.last_seen)}` : '尚无采样';
    if (!s) {
      $('hardware-content').innerHTML = `<div class="panel">${empty(status === 'unconfigured' ? '这台服务器尚未接入' : '暂时没有硬件数据',status === 'unconfigured' ? '配置 SSH 与采集服务后，即可在这里查看。' : '请检查服务器与采集服务的连接。','server')}</div>`;
      return;
    }
    const gpus = Array.isArray(s.gpus) ? s.gpus : [], primaryGPU = gpus[0];
    const gpuUsed = primaryGPU?.memory_used_bytes, gpuTotal = primaryGPU?.memory_total_bytes;
    const gpuDetail = primaryGPU ? `${primaryGPU.name || 'GPU'}${numeric(gpuUsed) ? ` · ${bytes(gpuUsed)} / ${bytes(gpuTotal)}` : ' · 显存待采样'}` : '未检测到可用的 GPU 采样';
    const cpuDetail = `${count(s.cpu?.observed_processors)} / ${count(s.cpu?.logical_processors)} 个逻辑处理器已采集`;
    const usedMem = numeric(s.memory?.total_bytes) && numeric(s.memory?.available_bytes) ? s.memory.total_bytes - s.memory.available_bytes : null;
    const disks = Array.isArray(s.disks) ? s.disks : [];
    const coverageIssue = numeric(s.cpu?.logical_processors) && numeric(s.cpu?.observed_processors) && s.cpu.logical_processors !== s.cpu.observed_processors;
    const warnings = Array.isArray(s.warnings) ? s.warnings : [];
    $('hardware-content').innerHTML = `<div class="${isFresh ? '' : 'stale-data'}">${!isFresh ? `<p class="stale-caption">${status === 'offline' ? '服务器已离线' : '数据已过期'} · 以下为 ${esc(fullTime(s.observed_at || item.last_seen))} 的历史采样，不能判断当前是否空闲。</p>` : ''}${coverageIssue ? '<div class="notice">CPU 采集覆盖不完整，当前数值不能代表整台服务器。请检查采集服务。</div>' : ''}<div class="metric-grid">${metricCard('CPU 占用','cpu',numeric(s.cpu?.percent) ? s.cpu.percent.toFixed(1) : '—','%',cpuDetail,s.cpu?.percent)}${metricCard('内存占用','memory',numeric(s.memory?.percent) ? s.memory.percent.toFixed(1) : '—','%',`${bytes(usedMem)} / ${bytes(s.memory?.total_bytes)}`,s.memory?.percent,'teal')}${metricCard(gpus.length > 1 ? `GPU 0 占用 · 共 ${gpus.length} 张` : 'GPU 占用','gpu',numeric(primaryGPU?.utilization_pct) ? primaryGPU.utilization_pct.toFixed(1) : '—','%',gpuDetail,primaryGPU?.utilization_pct)}${metricCard('SSH 连接','terminal',count(s.ssh?.tcp_connections),'个','已建立的 TCP 连接；不代表人数',undefined)}</div><div class="detail-grid"><article class="panel"><div class="panel-head"><h3>近期资源趋势</h3><div class="legend"><span><i></i>CPU</span><span><i class="memory-line"></i>内存</span><span><i class="gpu-line"></i>GPU</span></div></div>${renderChart(item.history || [])}</article><article class="panel"><div class="panel-head"><h3>磁盘与网络</h3><span class="subtle">实时速率</span></div><div class="storage-content">${disks.length ? disks.map((disk) => {
      const used = numeric(disk.total_bytes) && numeric(disk.free_bytes) && disk.total_bytes > 0 ? 100 * (1 - disk.free_bytes / disk.total_bytes) : null;
      return `<div class="disk-row"><div class="disk-label"><b>${esc(disk.name)}</b><span>可用 ${bytes(disk.free_bytes)} / ${bytes(disk.total_bytes)}</span></div>${meter(used)}<p class="field-hint">读取 ${numeric(disk.read_bps) ? `${bytes(disk.read_bps)}/s` : '—'} · 写入 ${numeric(disk.write_bps) ? `${bytes(disk.write_bps)}/s` : '—'}</p></div>`;
    }).join('') : '<p class="subtle">磁盘信息暂不可用</p>'}<div class="network-row"><span>接收 ↓ <b>${numeric(s.network?.recv_bps) ? `${bytes(s.network.recv_bps)}/s` : '—'}</b></span><span>发送 ↑ <b>${numeric(s.network?.sent_bps) ? `${bytes(s.network.sent_bps)}/s` : '—'}</b></span></div></div></article></div>${gpus.length > 1 ? `<div class="hardware-note">${gpus.slice(1).map((gpu) => `GPU ${esc(gpu.id)} · ${esc(gpu.name)}：${percent(gpu.utilization_pct)}，显存 ${bytes(gpu.memory_used_bytes)} / ${bytes(gpu.memory_total_bytes)}`).join('<br>')}</div>` : ''}${warnings.length ? `<div class="hardware-note"><details data-preserve="warnings"><summary>${warnings.length} 条采集提示</summary><ul>${warnings.map((warning) => `<li>${esc(warning)}</li>`).join('')}</ul></details></div>` : ''}</div>`;
  }

  function renderChart(history) {
    const points = history.filter((p) => validDate(p.at)).slice(-120);
    if (points.length < 2 || !points.some((p) => ['cpu_pct','memory_pct','gpu_pct'].some((key) => numeric(p[key])))) return '<div class="chart-empty">正在积累采样，趋势会在数据充足后显示。</div>';
    const width=560,height=166,left=34,right=548,top=16,bottom=135;
    const start=new Date(points[0].at).getTime(),end=new Date(points[points.length-1].at).getTime();
    const x=(p)=>left+(new Date(p.at).getTime()-start)/Math.max(1,end-start)*(right-left),y=(n)=>bottom-clamp(n)/100*(bottom-top);
    function path(key){let d='',connected=false;for(const p of points){if(!numeric(p[key])){connected=false;continue;}d+=`${connected?'L':'M'}${x(p).toFixed(2)},${y(p[key]).toFixed(2)} `;connected=true;}return d;}
    const plot = `<svg class="trend-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="近期 CPU、内存和 GPU 百分比趋势。下方可展开数值表。" preserveAspectRatio="none">${[0,50,100].map((v)=>`<line x1="${left}" x2="${right}" y1="${y(v)}" y2="${y(v)}" stroke="#e9eef4" stroke-width="1"/><text x="0" y="${y(v)+3}">${v}%</text>`).join('')}<path d="${path('cpu_pct')}" stroke="#2758a2" stroke-width="2" fill="none"/><path d="${path('memory_pct')}" stroke="#087f86" stroke-width="2" stroke-dasharray="5 3" fill="none"/><path d="${path('gpu_pct')}" stroke="#8865a9" stroke-width="2" stroke-dasharray="2 3" fill="none"/><text x="${left}" y="158">${esc(clockTime(points[0].at))}</text><text x="${right}" y="158" text-anchor="end">${esc(clockTime(points[points.length-1].at))}</text></svg>`;
    return `<div class="chart-wrap">${plot}</div><details class="chart-access" data-preserve="chart"><summary>查看最近 ${points.length} 次采样数值</summary><div class="chart-data-scroll"><table><thead><tr><th>采样时间</th><th>CPU</th><th>内存</th><th>GPU</th></tr></thead><tbody>${points.slice().reverse().map((p)=>`<tr><td>${esc(clockTime(p.at))}</td><td>${percent(p.cpu_pct)}</td><td>${percent(p.memory_pct)}</td><td>${percent(p.gpu_pct)}</td></tr>`).join('')}</tbody></table></div></details>`;
  }
  function autoEstimate(estimate) {
    if (!estimate) return '未接入日志';
    const labels={unknown:'暂无可靠估算',warming:'采样积累中', 'no-progress':'暂未观察到进度',completed:'日志已达到目标',error:'日志读取失败'};
    if (labels[estimate.status]) return labels[estimate.status];
    if (validDate(estimate.estimated_end)) return `约 ${fullTime(estimate.estimated_end)}`;
    if (numeric(estimate.remaining_seconds)) return `约剩 ${duration(estimate.remaining_seconds)}`;
    return '暂无可靠估算';
  }
  function renderJobs() {
    const item=server();
    if (!item) {$('task-count').textContent='—';$('task-content').innerHTML=empty('尚未接入服务器','接入后显示计算任务。');return;}
    const jobs=Array.isArray(item.jobs)?item.jobs:[];
    $('task-count').textContent=item.status==='unconfigured'||!item.snapshot?'—':String(jobs.filter((job)=>job.state!=='ended').length);
    if (item.status==='unconfigured') {$('task-content').innerHTML=empty('等待接入服务器','接入前无法判断任务状态。');return;}
    if (!item.snapshot) {$('task-content').innerHTML=empty('任务状态未知','尚未取得有效采样，不能判断是否空闲。');return;}
    const filtered=jobs.filter((job)=>{
      const matches=filter==='unclaimed'?!job.claim&&job.state!=='ended':filter==='mine'?job.claim?.can_edit:true;
      return matches&&`${job.name} ${job.software} ${job.claim?.owner_name||''} ${job.claim?.task_name||''}`.toLocaleLowerCase().includes(query.toLocaleLowerCase());
    });
    if (!filtered.length) {$('task-content').innerHTML=empty(jobs.length?'没有匹配的任务':effectiveStatus(item)==='online'?'尚未识别到计算任务':'上次采样未识别到计算任务',jobs.length?'可以切换筛选条件，或调整搜索关键词。':'任务识别按软件进程规则进行；请同时参考硬件占用。');return;}
    $('task-content').innerHTML=`${effectiveStatus(item)!=='online'?'<div class="table-footnote stale-caption">以下是历史任务状态；服务器恢复采样后再判断资源是否可用。</div>':''}<table class="task-table"><thead><tr><th class="task-col">任务 / 软件</th><th class="owner-col">使用成员</th><th class="load-col">资源占用</th><th class="eta-col">预计结束</th><th class="action-col">协作登记</th></tr></thead><tbody>${filtered.map((job)=>jobRow(item,job)).join('')}</tbody></table>`;
  }
  function jobRow(item,job) {
    const claim=job.claim, open=expanded.has(job.id),overdue=validDate(claim?.expected_end)&&new Date(claim.expected_end).getTime()<Date.now()&&job.state!=='ended';
    const action=claim?.can_edit?`<button type="button" class="button link-button" data-edit="${esc(job.id)}" data-focus-key="edit-${esc(job.id)}">编辑登记</button>`:claim?'<span class="subtle">已登记</span>':`<button type="button" class="button link-button" data-claim="${esc(job.id)}" data-focus-key="claim-${esc(job.id)}" ${canClaim(item)&&job.state!=='ended'?'':'disabled'}>认领任务</button>`;
    return `<tr class="job-row"><td><div class="task-name">${esc(claim?.task_name||job.name||job.software||'计算任务')}</div>${statusBadge(job.state,jobLabels[job.state]||'状态未知')}<span class="task-meta"> ${esc(job.software||'未知软件')}</span><p class="task-meta">${count(job.process_count)} 个进程 · 已运行 ${esc(duration(job.elapsed_seconds))}</p></td><td data-label="使用成员"><span class="task-owner ${claim?'':'unclaimed-text'}">${esc(claim?.owner_name||'待认领')}</span>${claim?.can_edit?'<p class="task-meta">本浏览器登记</p>':''}</td><td data-label="资源占用"><div class="load-value">${percent(job.cpu_pct)} <span class="task-meta">CPU</span></div><p class="task-meta">${bytes(job.memory_bytes)} 内存</p></td><td class="eta-cell" data-label="预计结束"><div class="eta-line ${overdue?'overdue':''}"><span class="eta-label">人工</span><strong>${validDate(claim?.expected_end)?esc(fullTime(claim.expected_end))+(overdue?' · 已超过预期':''):'未填写'}</strong></div><div class="eta-line log"><span class="eta-label">日志</span><strong>${esc(autoEstimate(job.estimate))}</strong></div>${numeric(job.estimate?.progress_pct)?`<p class="task-meta">日志进度 ${percent(job.estimate.progress_pct)}</p>`:''}</td><td class="action-cell"><div class="task-actions">${action}<button type="button" class="detail-button" data-detail="${esc(job.id)}" data-focus-key="detail-${esc(job.id)}" aria-expanded="${open}"><span aria-hidden="true">${open?'−':'+'}</span>进程详情</button></div></td></tr>${open?`<tr class="detail-row"><td colspan="5">${jobDetails(item,job)}</td></tr>`:''}`;
  }
  function jobDetails(item,job) {
    const keys=new Set(job.process_keys||[]),pids=new Set(job.pids||[]);
    const processes=(item.snapshot?.processes||[]).filter((p)=>keys.size?keys.has(p.key):pids.has(p.pid));
    return `<div class="job-detail-grid"><div>${processes.length?`<table class="process-table"><caption>归组进程 · CPU 为整机占比</caption><thead><tr><th>进程</th><th>PID</th><th>CPU</th><th>内存</th><th>GPU 显存</th></tr></thead><tbody>${processes.map((p)=>`<tr><td>${esc(p.name)}</td><td>${esc(p.pid ?? '—')}</td><td>${percent(p.cpu_pct)}</td><td>${bytes(p.memory_bytes)}</td><td>${bytes(p.gpu_memory_bytes)}</td></tr>`).join('')}</tbody></table>`:'<p class="subtle">当前采样中未找到这些进程。</p>'}</div><div class="detail-notes"><p><b>任务开始</b> ${esc(fullTime(job.started_at))}</p><p><b>登记备注</b> ${esc(job.claim?.notes||'未填写')}</p><p><b>估算说明</b> ${esc(job.estimate?.detail||'尚无可用日志估算。')}</p>${job.estimate?.source?`<p><b>日志来源</b> ${esc(job.estimate.source)}</p>`:''}<p>日志达到目标不等同于求解成功；请以计算结果和软件日志为准。</p></div></div>`;
  }
  function renderSSH() {
    const item=server();
    if (!item) {$('ssh-content').innerHTML=empty('尚未接入服务器','接入后显示 SSH 连接状态。','terminal');return;}
    if(item.status==='unconfigured'){$('ssh-content').innerHTML=empty('尚未接入 SSH 监测','服务器配置完成后显示已建立的连接。','terminal');return;}
    const ssh=item.snapshot?.ssh;
    const connections=Array.isArray(ssh?.connections)?ssh.connections:[];
    $('ssh-content').innerHTML=`<div class="ssh-summary"><div class="ssh-number">${count(ssh?.tcp_connections)}<small>连接</small></div><div><p><b>${effectiveStatus(item)==='online'?'SSH TCP 连接状态':'上次采样的连接状态'}</b></p><p>一人可能建立多个连接；计算也可能在 SSH 断开后继续运行。</p><p>采样时间 ${esc(fullTime(ssh?.observed_at))}${!numeric(ssh?.tcp_connections)?' · 暂无可靠连接数':''}</p></div></div>${connections.length?`<details class="ssh-details" data-preserve="ssh"><summary>查看 ${connections.length} 条连接来源</summary><div class="ssh-connections">${connections.map((c)=>`<span class="connection-chip">${esc(c.remote_address)}${numeric(c.remote_port)?`:${c.remote_port}`:''}</span>`).join('')}</div></details>`:''}`;
  }

  function showToast(message){clearTimeout(toastTimer);$('toast').textContent=message;$('toast').hidden=false;toastTimer=setTimeout(()=>$('toast').hidden=true,6500);}
  function localDateValue(value){if(!validDate(value))return '';const d=new Date(value);const p=(n)=>String(n).padStart(2,'0');return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;}
  function openClaim(jobId){
    const item=server(),job=item?.jobs?.find((j)=>j.id===jobId);
    if(!job)return;
    const claim=job.claim;
    if(claim&&!claim.can_edit){showToast('这条登记属于其他浏览器，仅可查看。');return;}
    editTarget={hostId:item.id,jobId:job.id,claimId:claim?.id||null};
    $('claim-form').reset();$('claim-error').hidden=true;$('log-options').open=Boolean(claim?.log_path);
    $('claim-title').textContent=claim?'编辑任务登记':'认领计算任务';
    $('claim-context').innerHTML=`<b>${esc(item.name)}</b> · ${esc(job.software||job.name)} · ${count(job.process_count)} 个进程`;
    $('owner-name').value=claim?.owner_name||state.identity?.name||'';
    $('task-name').value=claim?.task_name||job.name||'';
    $('expected-end').value=localDateValue(claim?.expected_end);
    $('task-notes').value=claim?.notes||'';$('log-path').value=claim?.log_path||'';
    $('log-path').placeholder=platformLabel(item)==='Linux'?'/srv/lab/project/job.sta':'E:\\WJY\\project\\job.sta';
    $('log-kind').value=claim?.log_kind||(['abaqus','fluent','comsol'].includes(String(job.software).toLowerCase())?String(job.software).toLowerCase():'abaqus');
    $('total-units').value=numeric(claim?.total_units)?claim.total_units:'';
    $('release-button').hidden=!claim;$('save-claim').textContent='保存登记';
    $('claim-dialog').showModal();
    ($('owner-name').value?$('task-name'):$('owner-name')).focus();
  }
  function setSubmitting(value){submitting=value;$('save-claim').disabled=value;$('release-button').disabled=value;$('save-claim').textContent=value?'正在保存…':'保存登记';}
  function formError(id,error){$(id).textContent=error.message;$(id).hidden=false;$(id).focus?.();}
  async function saveClaim(event){
    event.preventDefault();if(submitting||!editTarget)return;
    if(!$('claim-form').reportValidity())return;
    const owner=$('owner-name').value.trim(),task=$('task-name').value.trim();
    if(!owner||!task){formError('claim-error',new Error('请填写姓名和任务名称，不能仅使用空格。'));return;}
    const expected=$('expected-end').value;
    const total=$('total-units').value===''?null:Number($('total-units').value);
    if(total!==null&&(!numeric(total)||total<=0)){formError('claim-error',new Error('目标总进度必须是大于 0 的数字。'));return;}
    const body={host_id:editTarget.hostId,job_id:editTarget.jobId,owner_name:owner,task_name:task,expected_end:expected?new Date(expected).toISOString():null,notes:$('task-notes').value.trim(),log_path:$('log-path').value.trim(),log_kind:$('log-kind').value,total_units:total};
    setSubmitting(true);$('claim-error').hidden=true;
    try{await api(editTarget.claimId?`/api/claims/${encodeURIComponent(editTarget.claimId)}`:'/api/claims',editTarget.claimId?'PATCH':'POST',body);$('claim-dialog').close();showToast('任务登记已保存。计算进程保持运行。');await refresh();}
    catch(error){formError('claim-error',error);}
    finally{setSubmitting(false);}
  }
  async function releaseClaim(){
    if(submitting||!editTarget?.claimId)return;
    setSubmitting(true);$('release-button').textContent='正在释放…';
    try{await api(`/api/claims/${encodeURIComponent(editTarget.claimId)}`,'DELETE',{});$('claim-dialog').close();showToast('已释放登记；计算进程不会停止。');await refresh();}
    catch(error){formError('claim-error',error);}
    finally{setSubmitting(false);$('release-button').textContent='释放登记';}
  }
  // Keep native disclosure and keyboard focus stable while fresh snapshots arrive.
  const renderHardwareRaw=renderHardware,renderSSHRaw=renderSSH;
  function withDisclosure(callback,container){const open=new Set([...$(container).querySelectorAll('details[open][data-preserve]')].map((d)=>d.dataset.preserve));callback();$(container).querySelectorAll('details[data-preserve]').forEach((d)=>d.open=open.has(d.dataset.preserve));}
  renderHardware=()=>withDisclosure(renderHardwareRaw,'hardware-content');
  renderSSH=()=>withDisclosure(renderSSHRaw,'ssh-content');
  $('server-list').addEventListener('click',(event)=>{const target=event.target.closest('[data-server]');if(!target)return;selectedId=target.dataset.server;expanded.clear();render();});
  $('server-search').addEventListener('input',(event)=>{serverQuery=event.target.value;if(state)renderServers();});
  $('server-search-clear').addEventListener('click',()=>{serverQuery='';$('server-search').value='';if(state)renderServers();$('server-search').focus();});
  $('board-edit').addEventListener('click',openBoard);
  $('board-cancel').addEventListener('click',cancelBoard);
  $('board-draft').addEventListener('input',boardCharacterCount);
  $('board-reviewed').addEventListener('click',()=>resolveBoardConflict(false));
  $('board-load-latest').addEventListener('click',()=>resolveBoardConflict(true));
  $('board-form').addEventListener('submit',saveBoard);
  $('task-content').addEventListener('click',(event)=>{const claim=event.target.closest('[data-claim],[data-edit]');if(claim){openClaim(claim.dataset.claim||claim.dataset.edit);return;}const detail=event.target.closest('[data-detail]');if(detail){expanded.has(detail.dataset.detail)?expanded.delete(detail.dataset.detail):expanded.add(detail.dataset.detail);preserveFocus(renderJobs);}});
  document.querySelectorAll('[data-filter]').forEach((button)=>button.addEventListener('click',()=>{filter=button.dataset.filter;document.querySelectorAll('[data-filter]').forEach((b)=>{b.classList.toggle('selected',b===button);b.setAttribute('aria-pressed',String(b===button));});renderJobs();}));
  $('task-search').addEventListener('input',(event)=>{query=event.target.value;renderJobs();});
  $('refresh-button').addEventListener('click',()=>{clearTimeout(timer);refresh();});
  $('claim-form').addEventListener('submit',saveClaim);$('release-button').addEventListener('click',releaseClaim);
  document.querySelectorAll('[data-close]').forEach((button)=>button.addEventListener('click',()=>{if(!submitting)$(button.dataset.close).close();}));
  $('claim-dialog').addEventListener('cancel',(event)=>{if(submitting)event.preventDefault();});
  $('identity-button').addEventListener('click',()=>{$('identity-input').value=state?.identity?.name||'';$('identity-error').hidden=true;$('identity-dialog').showModal();$('identity-input').focus();});
  $('identity-form').addEventListener('submit',async(event)=>{event.preventDefault();const name=$('identity-input').value.trim();if(!name){formError('identity-error',new Error('请填写姓名。'));return;}const submit=event.target.querySelector('[type="submit"]');submit.disabled=true;try{await api('/api/identity','POST',{name});$('identity-dialog').close();showToast('姓名已保存，下次登记时会自动填写。');await refresh();}catch(error){formError('identity-error',error);}finally{submit.disabled=false;}});
  document.addEventListener('visibilitychange',()=>{clearTimeout(timer);renderRefreshStatus();if(!document.hidden)refresh();});
  document.querySelectorAll('.nav-item[href^="#"]').forEach((link)=>link.addEventListener('click',()=>{document.querySelectorAll('.nav-item').forEach((item)=>item.classList.remove('active'));link.classList.add('active');}));
  window.addEventListener('online',()=>{clearTimeout(timer);refresh();});
  refresh();
})();
