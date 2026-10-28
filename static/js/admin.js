// -*- coding: utf-8 -*-
// 后台管理中心客户端控制器 (Admin Controller)

let adminRecords = [];
let currentConsensusFilter = 'ALL';
let currentReviewFilter = 'ALL';
let currentPage = 1;
let pageSize = 50;
let totalRecords = 0;
let debounceTimer = null;

let currentLogLevel = 'ALL';
let isAutoRefreshLogs = true;
let logsTimer = null;

// 管理端人工复核状态
let adminSelectedReviewStatus = 'CONFIRMED_COLLISION';
let adminCurrentReviewRecord = null;

document.addEventListener('DOMContentLoaded', () => {
    fetchAdminLogs();
    fetchSystemStatus();

    // 日志自动刷新定时器 (2秒)
    logsTimer = setInterval(() => {
        if (isAutoRefreshLogs) {
            fetchAdminLogs(true);
        }
    }, 2000);

    // 主机状态定期刷新 (5秒)
    setInterval(fetchSystemStatus, 5000);

    // 检查 URL 是否指定直接打开数据分析或特定标签页
    const hash = window.location.hash;
    const urlParams = new URLSearchParams(window.location.search);
    const tabParam = urlParams.get('tab');
    if (hash === '#tab-analytics' || tabParam === 'analytics') {
        const btn = document.getElementById('tabBtnAnalytics');
        if (btn) switchAdminTab('analytics', btn);
    } else {
        const recBtn = document.getElementById('tabBtnRecords');
        if (recBtn) switchAdminTab('records', recBtn);
        else fetchAdminRecords();
    }
});

// ---------------------------------------------------------------------------
// 标签页切换
// ---------------------------------------------------------------------------
function switchAdminTab(tabName, btn) {
    document.querySelectorAll('.admin-tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.admin-tab-content').forEach(c => c.classList.remove('active'));

    if (btn) {
        btn.classList.add('active');
    } else {
        const defaultBtn = document.getElementById(`tabBtn${tabName.charAt(0).toUpperCase() + tabName.slice(1)}`);
        if (defaultBtn) defaultBtn.classList.add('active');
    }

    const target = document.getElementById(`tab-${tabName}`);
    if (target) {
        target.classList.add('active');
    }

    if (tabName === 'analytics') {
        loadAnalyticsData();
    } else if (tabName === 'records') {
        fetchAdminRecords();
    } else if (tabName === 'logs') {
        fetchAdminLogs();
    } else if (tabName === 'system') {
        fetchSystemStatus();
        checkSystemUpdate(true);
        fetchAutostartStatus();
    }
}

// ---------------------------------------------------------------------------
// TAB 1: 诊断台账与记录管理
// ---------------------------------------------------------------------------
async function fetchAdminRecords() {
    const search = document.getElementById('recordSearchInput').value.trim();
    const url = `/api/admin/records?page=${currentPage}&page_size=${pageSize}&consensus=${currentConsensusFilter}&review_status=${currentReviewFilter}&search=${encodeURIComponent(search)}`;

    try {
        const res = await fetch(url);
        const data = await res.json();
        if (data.success) {
            adminRecords = data.records || [];
            totalRecords = data.total || 0;
            renderAdminRecordsTable();
            updatePaginationUI();
            updateAdminSummaryCounts();
        }
    } catch (e) {
        console.error('Fetch admin records error:', e);
    }
}

function updateAdminSummaryCounts() {
    // 统计各状态计数
    fetch('/api/history?limit=1')
        .then(res => res.json())
        .then(data => {
            if (data.success && data.kpi) {
                const kpi = data.kpi;
                document.getElementById('recStatTotal').innerText = kpi.total || 0;
                document.getElementById('recStatAlarm').innerText = (kpi.unanimous || 0) + (kpi.majority || 0);
                document.getElementById('recStatShielded').innerText = kpi.shielded || 0;
                document.getElementById('recStatNormal').innerText = kpi.normal || 0;
                document.getElementById('recStatClipped').innerText = kpi.clipped || 0;
            } else if (data.success && data.records) {
                const recs = data.records;
                document.getElementById('recStatTotal').innerText = recs.length;
                document.getElementById('recStatAlarm').innerText = recs.filter(r => r.is_alarm).length;
                document.getElementById('recStatShielded').innerText = recs.filter(r => r.consensus_level === 'ISOLATED_DISTURBANCE_SAFE').length;
                document.getElementById('recStatNormal').innerText = recs.filter(r => r.consensus_level === 'NORMAL_CLEAN_NOISE').length;
                document.getElementById('recStatClipped').innerText = recs.filter(r => r.is_clipped).length;
            }
        });
}

function renderAdminRecordsTable() {
    const tbody = document.getElementById('adminRecordsTableBody');

    if (adminRecords.length === 0) {
        tbody.innerHTML = `<tr><td colspan="12" style="text-align:center;padding:36px;color:var(--text-muted)">暂无符合条件的诊断记录</td></tr>`;
        return;
    }

    tbody.innerHTML = adminRecords.map((r, i) => {
        const idx = (currentPage - 1) * pageSize + i + 1;
        const meanScore = (r.mean_confidence_pct || 0).toFixed(1);
        const k_score = r.scores && r.scores['03K'] !== undefined ? r.scores['03K'].toFixed(1) : '0.0';
        const u_score = r.scores && r.scores['03U'] !== undefined ? r.scores['03U'].toFixed(1) : '0.0';
        const r_score = r.scores && r.scores['03R'] !== undefined ? r.scores['03R'].toFixed(1) : '0.0';
        const peak = r.peak_amplitude !== undefined ? r.peak_amplitude.toFixed(3) : '--';
        const rms = r.rms_amplitude !== undefined ? r.rms_amplitude.toFixed(4) : '--';

        const rev = r.review || { status: 'UNREVIEWED', status_text: '待复核', badge_class: 'badge-unreviewed' };
        const revNoteTip = rev.notes ? `\n备注: ${rev.notes}` : '';
        const revTip = rev.reviewer ? `复核人: ${rev.reviewer} (${rev.reviewed_at || ''})${revNoteTip}` : '待复核';

        return `
        <tr>
            <td>${idx}</td>
            <td style="font-family:var(--font-mono);color:var(--text-muted);white-space:nowrap">${r.timestamp || '--'}</td>
            <td style="font-family:var(--font-mono);font-weight:700">${r.filename}</td>
            <td><span class="badge ${r.consensus_badge}">${r.alarm_votes}/3 票</span></td>
            <td style="font-family:var(--font-mono);font-weight:700;color:${meanScore >= 90 ? '#dc2626' : '#ea580c'}">${meanScore}%</td>
            <td style="font-family:var(--font-mono)">${k_score}%</td>
            <td style="font-family:var(--font-mono)">${u_score}%</td>
            <td style="font-family:var(--font-mono)">${r_score}%</td>
            <td style="font-family:var(--font-mono)">${peak} / ${rms}</td>
            <td><span class="badge ${r.consensus_badge}">${r.consensus_level}</span></td>
            <td>
                <span class="badge ${rev.badge_class || 'badge-unreviewed'}" title="${revTip}" style="cursor:pointer" onclick="openAdminReviewModal(${i})">
                    ${rev.status_text || '待复核'}
                </span>
                ${rev.reviewer ? `<div style="font-size:10px;color:var(--text-muted);margin-top:2px;font-family:var(--font-mono)">${rev.reviewer}</div>` : ''}
            </td>
            <td style="text-align:center;white-space:nowrap">
                <button class="btn btn-outline btn-sm" onclick="openAdminReviewModal(${i})" title="人工复核">
                    ✍️ 复核
                </button>
                <button class="btn btn-outline btn-sm" onclick="showDetailModal(${i})" title="查看完整元数据">
                    🔍 详情
                </button>
                ${r.plot_file ? `
                <button class="btn btn-outline btn-sm" onclick="showAdminPlotByIndex(${i})" title="查看声学时频谱图">
                    📈 图谱
                </button>` : ''}
                ${r.wav_file ? `
                <a href="/records/${encodeURIComponent(r.wav_file)}" target="_blank" class="btn btn-outline btn-sm" title="下载原声试听" style="text-decoration:none">
                    🎧 试听
                </a>` : ''}
                <button class="btn btn-danger btn-sm" onclick="deleteSingleRecordByIndex(${i})" title="删除该条记录">
                    🗑️
                </button>
            </td>
        </tr>
        `;
    }).join('');
}

function updatePaginationUI() {
    const totalPages = Math.max(1, Math.ceil(totalRecords / pageSize));
    document.getElementById('pageCurrent').innerText = currentPage;
    document.getElementById('pageTotal').innerText = totalPages;
    document.getElementById('pageCount').innerText = totalRecords;
}

function changeRecordPage(delta) {
    const totalPages = Math.max(1, Math.ceil(totalRecords / pageSize));
    const newPage = currentPage + delta;
    if (newPage >= 1 && newPage <= totalPages) {
        currentPage = newPage;
        fetchAdminRecords();
    }
}

function setRecordConsensus(lvl, btn) {
    currentConsensusFilter = lvl;
    currentPage = 1;
    btn.parentElement.querySelectorAll('.admin-filter-pill').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    fetchAdminRecords();
}

function setRecordReviewStatus(status, btn) {
    currentReviewFilter = status;
    currentPage = 1;
    btn.parentElement.querySelectorAll('.admin-filter-pill').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    fetchAdminRecords();
}

function filterRecordsDebounced() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
        currentPage = 1;
        fetchAdminRecords();
    }, 300);
}

async function deleteSingleRecordByIndex(index) {
    const record = adminRecords[index];
    if (!record) return;
    deleteSingleRecord(record.filename, record.timestamp);
}

async function deleteSingleRecord(filename, timestamp) {
    if (!confirm(`确定要从数据库中删除记录 "${filename}" 吗？此操作不可逆。`)) {
        return;
    }

    try {
        const res = await fetch('/api/admin/records/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filename, timestamp })
        });
        const data = await res.json();
        if (data.success) {
            fetchAdminRecords();
        } else {
            alert('删除记录失败: ' + (data.error || '未知错误'));
        }
    } catch (e) {
        alert('网络请求异常: ' + e);
    }
}

async function confirmClearAllRecords() {
    const pwd = prompt('警告：清空全部诊断历史记录将删除当前台账中所有诊断数据！\n请输入 CONFIRM 确认清空:');
    if (pwd !== 'CONFIRM') {
        alert('未输入确认口令，已取消操作。');
        return;
    }

    try {
        const res = await fetch('/api/admin/records/clear_all', { method: 'POST' });
        const data = await res.json();
        if (data.success) {
            alert(data.msg);
            fetchAdminRecords();
        } else {
            alert('清空失败: ' + (data.error || '未知错误'));
        }
    } catch (e) {
        alert('网络请求异常: ' + e);
    }
}

function showDetailModal(index) {
    const record = adminRecords[index];
    if (!record) return;

    document.getElementById('modalDetailTitle').innerText = `📋 诊断元数据 - ${record.filename}`;
    document.getElementById('modalJsonContent').innerText = JSON.stringify(record, null, 2);
    document.getElementById('detailModal').style.display = 'flex';
}

function closeDetailModal() {
    document.getElementById('detailModal').style.display = 'none';
}

function showAdminPlot(plotFile) {
    if (!plotFile) return;
    const modal = document.getElementById('adminImageModal');
    const img = document.getElementById('adminModalImg');
    img.src = `/records/${encodeURIComponent(plotFile)}?t=${Date.now()}`;
    modal.style.display = 'flex';
}

function showAdminPlotByIndex(index) {
    const record = adminRecords[index];
    if (record && record.plot_file) {
        showAdminPlot(record.plot_file);
    }
}

function closeAdminImageModal() {
    document.getElementById('adminImageModal').style.display = 'none';
}

// ---------------------------------------------------------------------------
// 管理端人工复核弹窗控制器
// ---------------------------------------------------------------------------

function openAdminReviewModal(index) {
    const record = adminRecords[index];
    if (!record) return;
    adminCurrentReviewRecord = record;

    document.getElementById('adminRevFilename').innerText = record.filename || '--';
    document.getElementById('adminRevTime').innerText = record.timestamp || '--';
    document.getElementById('adminRevConsensus').innerText = `${record.consensus_level || 'NORMAL_CLEAN_NOISE'} (${record.alarm_votes || 0}/3票)`;

    const currentStatus = (record.review && record.review.status && record.review.status !== 'UNREVIEWED')
        ? record.review.status
        : (record.is_alarm ? 'CONFIRMED_COLLISION' : 'CONFIRMED_NORMAL');

    selectAdminReviewOption(currentStatus);

    const savedReviewer = localStorage.getItem('admin_reviewer_name') || '管理端质检总工';
    const existingReviewer = (record.review && record.review.reviewer) ? record.review.reviewer : savedReviewer;
    document.getElementById('adminRevReviewer').value = existingReviewer;

    document.getElementById('adminRevNotes').value = (record.review && record.review.notes) ? record.review.notes : '';

    const modal = document.getElementById('adminReviewModal');
    if (modal) {
        modal.style.display = 'flex';
    }
}

function selectAdminReviewOption(status) {
    adminSelectedReviewStatus = status;
    const cards = document.querySelectorAll('#adminReviewModal .review-option-card');
    cards.forEach(c => {
        if (c.getAttribute('onclick') && c.getAttribute('onclick').includes(status)) {
            c.classList.add('selected');
        } else {
            c.classList.remove('selected');
        }
    });
}

function closeAdminReviewModal() {
    const modal = document.getElementById('adminReviewModal');
    if (modal) {
        modal.style.display = 'none';
    }
    adminCurrentReviewRecord = null;
}

async function submitAdminReviewAction() {
    if (!adminCurrentReviewRecord) return;

    const reviewer = document.getElementById('adminRevReviewer').value.trim() || '管理端质检总工';
    const notes = document.getElementById('adminRevNotes').value.trim();
    const btn = document.getElementById('btnAdminSubmitReview');

    btn.disabled = true;
    btn.innerText = '⏳ 正在保存复核记录...';

    try {
        const payload = {
            filename: adminCurrentReviewRecord.filename,
            timestamp: adminCurrentReviewRecord.timestamp,
            status: adminSelectedReviewStatus,
            reviewer: reviewer,
            notes: notes
        };

        const res = await fetch('/api/records/review', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        const data = await res.json();
        if (data.success) {
            localStorage.setItem('admin_reviewer_name', reviewer);
            closeAdminReviewModal();
            fetchAdminRecords();
        } else {
            alert('提交复核失败: ' + (data.error || '未知错误'));
        }
    } catch (e) {
        alert('网络请求异常: ' + e);
    } finally {
        btn.disabled = false;
        btn.innerText = '💾 确认提交复核结论';
    }
}

function exportRecordsCSV() {
    fetch('/api/history')
        .then(res => res.json())
        .then(data => {
            if (!data.success || !data.records || data.records.length === 0) {
                alert('暂无可导出的台账记录');
                return;
            }
            let csv = '\uFEFF时间戳,文件名,3模型票数,平均置信度%,03K置信%,03U置信%,03R置信%,峰值幅值,RMS有效值,削顶状态,仲裁结论,是否确诊告警,人工复核结论,复核人员,复核时间,勘验备注\n';
            data.records.forEach(r => {
                const k = r.scores && r.scores['03K'] !== undefined ? r.scores['03K'] : 0.0;
                const u = r.scores && r.scores['03U'] !== undefined ? r.scores['03U'] : 0.0;
                const r_val = r.scores && r.scores['03R'] !== undefined ? r.scores['03R'] : 0.0;
                const rev = r.review || {};
                const revStatus = rev.status_text || '待复核';
                const reviewer = rev.reviewer || '';
                const revTime = rev.reviewed_at || '';
                const revNotes = (rev.notes || '').replace(/"/g, '""');

                csv += `"${r.timestamp}","${r.filename}",${r.alarm_votes},${(r.mean_confidence_pct || 0).toFixed(2)},${k},${u},${r_val},${r.peak_amplitude},${r.rms_amplitude},"${r.is_clipped ? '削顶' : '正常'}","${r.consensus_level}",${r.is_alarm ? 1 : 0},"${revStatus}","${reviewer}","${revTime}","${revNotes}"\n`;
            });
            const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
            const link = document.createElement('a');
            link.href = URL.createObjectURL(blob);
            link.download = `3模型全量诊断台账_${new Date().toISOString().slice(0, 10)}.csv`;
            link.click();
        });
}

// ---------------------------------------------------------------------------
// TAB 2: 实时运行与系统日志
// ---------------------------------------------------------------------------
async function fetchAdminLogs(isSilent = false) {
    const search = document.getElementById('logSearchInput').value.trim();
    const url = `/api/admin/logs?level=${currentLogLevel}&search=${encodeURIComponent(search)}&limit=400`;

    try {
        const res = await fetch(url);
        const data = await res.json();
        if (data.success) {
            renderLogsConsole(data.logs || []);
            document.getElementById('logCounterText').innerText = `共 ${data.count} / ${data.total} 行日志`;
        }
    } catch (e) {
        if (!isSilent) console.error('Fetch logs error:', e);
    }
}

function renderLogsConsole(logs) {
    const container = document.getElementById('logConsoleBody');
    if (logs.length === 0) {
        container.innerHTML = `<div style="text-align:center;padding:40px;color:#64748b">暂无匹配的运行日志</div>`;
        return;
    }

    // 最新在下 (按时间正序)
    const sorted = [...logs].reverse();
    container.innerHTML = sorted.map(l => `
        <div class="log-entry">
            <span class="log-time">[${l.time}]</span>
            <span class="log-tag ${l.level}">${l.level}</span>
            <span class="log-msg">${escapeHtml(l.msg)}</span>
        </div>
    `).join('');

    // 自动滚动到最新底部
    container.scrollTop = container.scrollHeight;
}

function setLogLevel(lvl, btn) {
    currentLogLevel = lvl;
    document.querySelectorAll('#tab-logs .admin-filter-pill').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    fetchAdminLogs();
}

function toggleAutoRefreshLogs(cb) {
    isAutoRefreshLogs = cb.checked;
}

async function confirmClearLogs() {
    if (!confirm('确定清空当前所有运行日志吗？')) return;

    try {
        const res = await fetch('/api/admin/logs/clear', { method: 'POST' });
        const data = await res.json();
        if (data.success) {
            fetchAdminLogs();
        }
    } catch (e) {
        alert('清空日志异常: ' + e);
    }
}

function escapeHtml(str) {
    if (!str) return '';
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ---------------------------------------------------------------------------
// TAB 3: 引擎配置与资源监控
// ---------------------------------------------------------------------------
let isAlgorithmRunning = true;

function updateAlgorithmRunningUI(running) {
    isAlgorithmRunning = !!running;

    // Header 元素
    const hBadge = document.getElementById('headerAlgoStatusBadge');
    const hStopBtn = document.getElementById('btnHeaderStopAlgo');
    const hStartBtn = document.getElementById('btnHeaderStartAlgo');
    // Tab 3 Form 按钮
    const fStopBtn = document.getElementById('btnStopEngineForm');
    const fStartBtn = document.getElementById('btnStartEngineForm');
    // Tab 3 Card 元素
    const cBadge = document.getElementById('algoStatusBadgeBottom');
    const cStopBtn = document.getElementById('btnStopEngineCard');
    const cStartBtn = document.getElementById('btnStartEngineCard');

    if (isAlgorithmRunning) {
        if (hBadge) {
            hBadge.className = 'status-pill status-ready';
            hBadge.innerText = '● 算法检测运行中';
        }
        if (hStopBtn) hStopBtn.style.display = 'inline-flex';
        if (hStartBtn) hStartBtn.style.display = 'none';

        if (fStopBtn) fStopBtn.style.display = 'inline-flex';
        if (fStartBtn) fStartBtn.style.display = 'none';

        if (cBadge) {
            cBadge.className = 'status-pill status-ready';
            cBadge.innerText = '● 算法检测运行中 (RUNNING)';
        }
        if (cStopBtn) cStopBtn.style.display = 'inline-flex';
        if (cStartBtn) cStartBtn.style.display = 'none';
    } else {
        if (hBadge) {
            hBadge.className = 'status-pill status-stopped';
            hBadge.innerText = '● 算法检测已暂停';
        }
        if (hStopBtn) hStopBtn.style.display = 'none';
        if (hStartBtn) hStartBtn.style.display = 'inline-flex';

        if (fStopBtn) fStopBtn.style.display = 'none';
        if (fStartBtn) fStartBtn.style.display = 'inline-flex';

        if (cBadge) {
            cBadge.className = 'status-pill status-stopped';
            cBadge.innerText = '● 算法检测已暂停 (STOPPED)';
        }
        if (cStopBtn) cStopBtn.style.display = 'none';
        if (cStartBtn) cStartBtn.style.display = 'inline-flex';
    }
}

async function stopAlgorithmEngine() {
    if (!confirm('确定停止算法检测吗？\n\n停止后系统将立即暂停后台待测音频目录扫描与推理计算，不会占用任何后台计算性能。您可以随时在设置界面恢复启动。')) {
        return;
    }
    try {
        const res = await fetch('/api/admin/algorithm/stop', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        const data = await res.json();
        if (data.success) {
            updateAlgorithmRunningUI(false);
            alert('⏸️ ' + data.msg);
            fetchAdminLogs();
        } else {
            alert('❌ 停止算法失败: ' + (data.error || '未知错误'));
        }
    } catch (e) {
        alert('请求异常: ' + e);
    }
}

async function startAlgorithmEngine() {
    try {
        const res = await fetch('/api/admin/algorithm/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        const data = await res.json();
        if (data.success) {
            updateAlgorithmRunningUI(true);
            alert('▶️ ' + data.msg);
            fetchAdminLogs();
        } else {
            alert('❌ 启动算法失败: ' + (data.error || '未知错误'));
        }
    } catch (e) {
        alert('请求异常: ' + e);
    }
}

// 宿主机资源限制交互控制
function onCpuSliderInput(val) {
    const lbl = document.getElementById('valCpuSliderText');
    if (lbl) lbl.innerText = `${val}%`;
    const disp = document.getElementById('dispMaxCpuPct');
    if (disp) disp.innerText = `${val}%`;
}

function onMemSliderInput(val) {
    const lbl = document.getElementById('valMemSliderText');
    if (lbl) lbl.innerText = `${val}%`;
    const disp = document.getElementById('dispMaxMemPct');
    if (disp) disp.innerText = `${val}%`;
}

function setCpuPreset(val) {
    const el = document.getElementById('cfgMaxCpuPercent');
    if (el) {
        el.value = val;
        onCpuSliderInput(val);
    }
}

function setMemPreset(val) {
    const el = document.getElementById('cfgMaxMemPercent');
    if (el) {
        el.value = val;
        onMemSliderInput(val);
    }
}

function toggleResourceLimitEnable(cb) {
    const badge = document.getElementById('resLimitActiveBadge');
    if (badge) {
        if (cb.checked) {
            badge.className = 'status-pill status-ready';
            badge.innerText = '● 保护生效中';
        } else {
            badge.className = 'status-pill status-stopped';
            badge.innerText = '○ 保护已关闭';
        }
    }
}

async function applyRealtimeResourceLimits() {
    const resEnable = document.getElementById('cfgResourceLimitEnable') ? document.getElementById('cfgResourceLimitEnable').checked : true;
    const maxCpu = parseFloat(document.getElementById('cfgMaxCpuPercent') ? document.getElementById('cfgMaxCpuPercent').value : 50);
    const maxMem = parseFloat(document.getElementById('cfgMaxMemPercent') ? document.getElementById('cfgMaxMemPercent').value : 50);
    const priority = document.getElementById('cfgProcessPriority') ? document.getElementById('cfgProcessPriority').value : 'BELOW_NORMAL';

    try {
        const res = await fetch('/api/admin/update_resource_limits', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                enable: resEnable,
                max_cpu_percent: maxCpu,
                max_memory_percent: maxMem,
                process_priority: priority
            })
        });
        const data = await res.json();
        if (data.success) {
            alert('⚡ ' + data.msg);
            fetchSystemStatus();
            fetchAdminLogs();
        } else {
            alert('❌ 资源限制调整失败: ' + (data.error || '未知错误'));
        }
    } catch (e) {
        alert('请求异常: ' + e);
    }
}

async function fetchSystemStatus() {
    try {
        const res = await fetch('/api/admin/system_status');
        const data = await res.json();
        if (data.success && data.status) {
            const s = data.status;
            document.getElementById('sysCpuVal').innerText = `${s.cpu_percent.toFixed(1)} %`;
            document.getElementById('sysCpuCores').innerText = `${s.cpu_cores} 逻辑核心`;
            document.getElementById('sysRamVal').innerText = `${s.ram_percent.toFixed(1)} %`;
            document.getElementById('sysRamSub').innerText = `${(s.ram_total_gb * (s.ram_percent/100)).toFixed(1)} / ${s.ram_total_gb} GB`;
            document.getElementById('sysProcMemVal').innerText = `${s.process_memory_mb} MB`;
            document.getElementById('sysRecordsDiskVal').innerText = `${s.records_size_mb} MB`;
            document.getElementById('sysRecordsCount').innerText = `共 ${s.records_file_count} 个文件`;

            // 同步算法启动/暂停状态
            if (s.is_algorithm_running !== undefined) {
                updateAlgorithmRunningUI(s.is_algorithm_running);
            }

            // 宿主机资源限制状态与表单联动
            if (s.resource_limit) {
                const rl = s.resource_limit;
                const statusEl = document.getElementById('sysResourceLimitStatus');
                const subEl = document.getElementById('sysResourceLimitSub');
                if (statusEl) {
                    if (rl.enabled) {
                        statusEl.innerText = `已限制 ${rl.max_cpu_percent}% 资源`;
                        statusEl.style.color = '#10b981';
                    } else {
                        statusEl.innerText = `全量性能 (未限制)`;
                        statusEl.style.color = '#f59e0b';
                    }
                }
                if (subEl) {
                    subEl.innerText = `CPU ${rl.allowed_cores_count}/${rl.total_cores}核 | 内存上限 ${rl.max_allowed_ram_gb}GB`;
                }

                // 调控面板实时展示字段联动
                const dispCpu = document.getElementById('dispMaxCpuPct');
                const dispCores = document.getElementById('dispCpuCores');
                const dispMem = document.getElementById('dispMaxMemPct');
                const dispMemGb = document.getElementById('dispMaxMemGb');
                const dispThreads = document.getElementById('dispPyTorchThreads');
                const dispPrio = document.getElementById('dispProcessPriority');

                if (dispCpu) dispCpu.innerText = `${rl.max_cpu_percent}%`;
                if (dispCores) dispCores.innerText = `绑定 ${rl.allowed_cores_count} / ${rl.total_cores} 核心`;
                if (dispMem) dispMem.innerText = `${rl.max_memory_percent}%`;
                if (dispMemGb) dispMemGb.innerText = `上限 ${rl.max_allowed_ram_gb} GB`;
                if (dispThreads) dispThreads.innerText = `${rl.pytorch_threads} 线程`;
                if (dispPrio) dispPrio.innerText = rl.process_priority || 'BELOW_NORMAL';

                // 仅当用户未聚焦输入框/滑块时同步滑块与下拉框值
                const activeEl = document.activeElement;
                const isUserInteracting = activeEl && (activeEl.id === 'cfgMaxCpuPercent' || activeEl.id === 'cfgMaxMemPercent');
                if (!isUserInteracting) {
                    const enCb = document.getElementById('cfgResourceLimitEnable');
                    const cpuIn = document.getElementById('cfgMaxCpuPercent');
                    const memIn = document.getElementById('cfgMaxMemPercent');
                    const prioSel = document.getElementById('cfgProcessPriority');

                    if (enCb) {
                        enCb.checked = !!rl.enabled;
                        toggleResourceLimitEnable(enCb);
                    }
                    if (cpuIn) {
                        cpuIn.value = rl.max_cpu_percent || 50;
                        const t = document.getElementById('valCpuSliderText');
                        if (t) t.innerText = `${cpuIn.value}%`;
                    }
                    if (memIn) {
                        memIn.value = rl.max_memory_percent || 50;
                        const t = document.getElementById('valMemSliderText');
                        if (t) t.innerText = `${memIn.value}%`;
                    }
                    if (prioSel && rl.process_priority) {
                        prioSel.value = rl.process_priority;
                    }
                }
            }

            // 填充常规表单默认值 (仅当输入框未处于编辑聚焦状态时)
            const activeId = document.activeElement ? document.activeElement.id : '';
            if (activeId !== 'cfgAlarmThresh' && activeId !== 'cfgScanInterval' && activeId !== 'cfgMinRmsThresh') {
                document.getElementById('cfgAlarmThresh').value = s.alarm_threshold_pct;
                document.getElementById('cfgScanInterval').value = s.scan_interval_sec;
                if (document.getElementById('cfgMinRmsThresh')) {
                    document.getElementById('cfgMinRmsThresh').value = s.min_rms_threshold !== undefined ? s.min_rms_threshold : 0.02;
                }
            }
            if (activeId !== 'cfgWatchDir') {
                document.getElementById('cfgWatchDir').value = s.watch_dir || 'incoming_audio';
            }
            if (activeId !== 'cfgProcessedDir') {
                document.getElementById('cfgProcessedDir').value = s.processed_dir || '';
            }
            if (document.getElementById('cfgWatchDirResolved')) {
                document.getElementById('cfgWatchDirResolved').innerText = `当前实际物理路径: ${s.watch_dir_abs || s.watch_dir || '--'}`;
            }
            if (document.getElementById('cfgProcessedDirResolved')) {
                document.getElementById('cfgProcessedDirResolved').innerText = `当前实际归档路径: ${s.processed_dir_abs || s.processed_dir || '--'}`;
            }
        }
    } catch (e) {
        console.error('Fetch system status error:', e);
    }
}

async function saveSystemConfig(e) {
    e.preventDefault();
    const alarmThresh = parseFloat(document.getElementById('cfgAlarmThresh').value);
    const scanInterval = parseFloat(document.getElementById('cfgScanInterval').value);
    const minRmsThresh = parseFloat(document.getElementById('cfgMinRmsThresh') ? document.getElementById('cfgMinRmsThresh').value : 0.02);
    const watchDir = document.getElementById('cfgWatchDir') ? document.getElementById('cfgWatchDir').value.trim() : 'incoming_audio';
    const processedDir = document.getElementById('cfgProcessedDir') ? document.getElementById('cfgProcessedDir').value.trim() : '';
    const resEnable = document.getElementById('cfgResourceLimitEnable') ? document.getElementById('cfgResourceLimitEnable').checked : true;
    const maxCpu = parseFloat(document.getElementById('cfgMaxCpuPercent') ? document.getElementById('cfgMaxCpuPercent').value : 50);
    const maxMem = parseFloat(document.getElementById('cfgMaxMemPercent') ? document.getElementById('cfgMaxMemPercent').value : 50);
    const priority = document.getElementById('cfgProcessPriority') ? document.getElementById('cfgProcessPriority').value : 'BELOW_NORMAL';

    try {
        const res = await fetch('/api/admin/update_config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                alarm_threshold_pct: alarmThresh,
                scan_interval_sec: scanInterval,
                min_rms_threshold: minRmsThresh,
                watch_dir: watchDir,
                processed_dir: processedDir,
                resource_limit_enable: resEnable,
                max_cpu_percent: maxCpu,
                max_memory_percent: maxMem,
                process_priority: priority
            })
        });
        const data = await res.json();
        if (data.success) {
            alert('✅ 系统配置更新成功！检测路径与各项参数已即时热生效。');
            fetchSystemStatus();
            fetchAdminLogs();
        } else {
            alert('保存配置失败: ' + (data.error || '未知错误'));
        }
    } catch (err) {
        alert('保存异常: ' + err);
    }
}

function resetDefaultWatchDir() {
    const el = document.getElementById('cfgWatchDir');
    if (el) el.value = 'incoming_audio';
}

function resetDefaultProcessedDir() {
    const el = document.getElementById('cfgProcessedDir');
    if (el) el.value = 'incoming_audio/processed';
}

async function restartAlgorithmEngine() {
    if (!confirm('确定重启算法引擎与检测系统吗？\n\n系统将重新加载 Python 算法代码、CPD 启停机变点检测门禁与 3 大核心模型 (03K + 03U + 03R)，耗时约 8~12 秒。')) {
        return;
    }

    const btn = document.getElementById('btnHeaderRestart');
    const btnMain = document.getElementById('btnRestartEngine');
    const btnCard = document.getElementById('btnRestartEngineMain');
    const loadingBox = document.getElementById('restartLoadingStatus');
    const loadingText = document.getElementById('restartLoadingText');

    if (btn) { btn.disabled = true; btn.innerText = '⏳ 正在重启...'; }
    if (btnMain) { btnMain.disabled = true; btnMain.innerText = '⏳ 正在重启...'; }
    if (btnCard) { btnCard.disabled = true; btnCard.innerText = '⏳ 正在重载 03K/03U/03R 模型与 CPD 门禁...'; }
    if (loadingBox) {
        loadingBox.style.display = 'flex';
        loadingText.innerText = '正在重新加载算法模块与 3 大核心模型，重置常驻内存镜像，约需 8~12 秒，请稍候...';
    }

    try {
        const res = await fetch('/api/admin/restart_engine', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        const data = await res.json();
        if (data.success) {
            alert('✅ ' + data.msg);
            fetchSystemStatus();
            fetchAdminLogs();
        } else {
            alert('❌ 算法引擎重启失败: ' + (data.error || '未知错误'));
        }
    } catch (err) {
        alert('请求异常: ' + err);
    } finally {
        if (btn) { btn.disabled = false; btn.innerText = '🔄 重启算法'; }
        if (btnMain) { btnMain.disabled = false; btnMain.innerText = '🔄 热重载引擎'; }
        if (btnCard) { btnCard.disabled = false; btnCard.innerText = '🔄 热重载引擎'; }
        if (loadingBox) { loadingBox.style.display = 'none'; }
    }
}

async function restartProcessCold() {
    if (!confirm('确定完全重启整个 Python 监控后台服务进程 (冷重启) 吗？\n\n服务将短暂退出并重新加载启动，页面将在 15 秒后自动刷新。')) {
        return;
    }

    try {
        const res = await fetch('/api/admin/restart_service', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        const data = await res.json();
        alert('🔄 ' + (data.msg || '服务进程正在重启中...'));
        setTimeout(() => {
            location.reload();
        }, 15000);
    } catch (err) {
        alert('重启指令已发送 (服务正在退出重启中): ' + err);
        setTimeout(() => {
            location.reload();
        }, 15000);
    }
}

// ---------------------------------------------------------------------------
// 系统版本与在线自动升级 (System Auto-Update & Version Maintenance)
// ---------------------------------------------------------------------------
let isCheckingUpdate = false;

async function checkSystemUpdate(isSilent = false) {
    if (isCheckingUpdate) return;
    isCheckingUpdate = true;

    const btn = document.getElementById('btnCheckUpdate');
    const badge = document.getElementById('updateStatusBadge');
    const localShaEl = document.getElementById('updateLocalSha');
    const localMsgEl = document.getElementById('updateLocalMsg');
    const remoteShaEl = document.getElementById('updateRemoteSha');
    const remoteMsgEl = document.getElementById('updateRemoteMsg');
    const diffBox = document.getElementById('updateDiffBox');
    const btnApply = document.getElementById('btnApplyUpdate');

    if (btn && !isSilent) {
        btn.disabled = true;
        btn.innerText = '🔍 正在联网查询...';
    }
    if (badge) {
        badge.className = 'status-pill';
        badge.style.background = '#eff6ff';
        badge.style.color = '#2563eb';
        badge.innerText = '● 正在检查远程版本...';
    }

    try {
        const res = await fetch('/api/admin/check_update');
        const json = await res.json();
        if (json.success && json.data) {
            const d = json.data;
            if (localShaEl) {
                localShaEl.innerText = d.version ? `${d.version} (${d.local_commit || 'git'})` : (d.local_commit || '未知');
            }
            if (localMsgEl) localMsgEl.innerText = d.local_msg || '';

            if (d.error) {
                if (badge) {
                    badge.className = 'status-pill';
                    badge.style.background = '#fef2f2';
                    badge.style.color = '#dc2626';
                    badge.innerText = '● 检查失败';
                }
                if (remoteMsgEl) remoteMsgEl.innerText = d.error;
                if (!isSilent) alert('检查更新失败: ' + d.error);
                return;
            }

            if (remoteShaEl) remoteShaEl.innerText = d.remote_commit || '未知';

            if (d.has_update) {
                if (badge) {
                    badge.className = 'status-pill';
                    badge.style.background = '#fffbeb';
                    badge.style.color = '#d97706';
                    badge.innerText = `● 发现新版本 (落后 ${d.commits_behind} 个提交)`;
                }
                if (remoteMsgEl) remoteMsgEl.innerText = `远程包含 ${d.commits_behind} 个新提交，可立即升级`;
                if (diffBox) {
                    diffBox.style.display = 'block';
                    const changelogText = (d.changelog && d.changelog.length > 0)
                        ? '【更新日志预览】\n' + d.changelog.join('\n')
                        : '【更新日志预览】\n检测到远程仓库存在新版本代码提交。';
                    diffBox.innerText = changelogText;
                }
                if (btnApply) {
                    btnApply.disabled = false;
                    btnApply.style.opacity = '1';
                    btnApply.innerText = '🚀 在线升级';
                }
            } else {
                if (badge) {
                    badge.className = 'status-pill status-ready';
                    badge.style.background = '#ecfdf5';
                    badge.style.color = '#059669';
                    badge.innerText = '● 已是最新版本';
                }
                if (remoteMsgEl) remoteMsgEl.innerText = '当前代码与 GitHub 远程主线完全一致';
                if (diffBox) {
                    diffBox.style.display = 'none';
                }
                if (btnApply) {
                    btnApply.disabled = true;
                    btnApply.style.opacity = '0.6';
                    btnApply.innerText = '✅ 已是最新版本';
                }
            }
        } else {
            const errMsg = json.error || '检查接口返回异常';
            if (badge) {
                badge.className = 'status-pill';
                badge.style.background = '#fef2f2';
                badge.style.color = '#dc2626';
                badge.innerText = '● 检查失败';
            }
            if (!isSilent) alert('检查更新失败: ' + errMsg);
        }
    } catch (err) {
        if (!isSilent) alert('联网请求异常: ' + err);
    } finally {
        isCheckingUpdate = false;
        if (btn) {
            btn.disabled = false;
            btn.innerText = '🔍 检查更新';
        }
    }
}

async function applySystemUpdate() {
    if (!confirm('确定立即升级系统吗？\n\n系统将自动执行：\n1. 从 GitHub 仓库拉取最新代码 (git pull)\n2. 校验并安装更新依赖库 (requirements.txt)\n3. 校验并自动补全核心基础模型权重 (download_weights.py)\n4. 自动平滑热重启后台守护监控进程\n\n升级过程约持续 15~30 秒，期间监控服务将短暂重置。')) {
        return;
    }

    const btnApply = document.getElementById('btnApplyUpdate');
    const btnCheck = document.getElementById('btnCheckUpdate');
    const progressBox = document.getElementById('updateProgressBox');
    const progressText = document.getElementById('updateProgressText');

    if (btnApply) { btnApply.disabled = true; btnApply.innerText = '⏳ 正在升级...'; }
    if (btnCheck) { btnCheck.disabled = true; }
    if (progressBox) { progressBox.style.display = 'flex'; }

    try {
        const res = await fetch('/api/admin/apply_update', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        const json = await res.json();
        if (json.success) {
            let countdown = 25;
            const timer = setInterval(() => {
                countdown--;
                if (progressText) {
                    progressText.innerText = `升级进程正在后台执行代码拉取与依赖验证，服务即将自动重启... 页面将在 ${countdown} 秒后自动刷新。`;
                }
                if (countdown <= 0) {
                    clearInterval(timer);
                    location.reload();
                }
            }, 1000);
        } else {
            alert('❌ 触发更新失败: ' + (json.error || '未知错误'));
            if (btnApply) { btnApply.disabled = false; btnApply.innerText = '🚀 在线升级'; }
            if (btnCheck) { btnCheck.disabled = false; }
            if (progressBox) { progressBox.style.display = 'none'; }
        }
    } catch (err) {
        let countdown = 20;
        const timer = setInterval(() => {
            countdown--;
            if (progressText) {
                progressText.innerText = `更新指令已成功送达，后台正重启服务... 页面将在 ${countdown} 秒后自动刷新。`;
            }
            if (countdown <= 0) {
                clearInterval(timer);
                location.reload();
            }
        }, 1000);
    }
}

// ---------------------------------------------------------------------------
// 开机自启动管理 (Auto-Start Management)
// ---------------------------------------------------------------------------
let currentAutostartEnabled = false;

async function fetchAutostartStatus() {
    try {
        const res = await fetch('/api/admin/autostart_status');
        const json = await res.json();
        if (json.success && json.status) {
            currentAutostartEnabled = !!json.status.enabled;
            const badge = document.getElementById('autostartStatusBadge');
            const txt = document.getElementById('autostartStatusText');
            const btn = document.getElementById('btnToggleAutostart');
            const btnMain = document.getElementById('btnAutostartMain');

            if (currentAutostartEnabled) {
                if (badge) {
                    badge.className = 'status-pill status-ready';
                    badge.style.background = '#ecfdf5';
                    badge.style.color = '#059669';
                    badge.innerText = '● 已开启';
                }
                if (txt) {
                    txt.innerText = '已开启开机自启';
                    txt.style.color = '#059669';
                }
                if (btn) btn.innerText = '关闭';
                if (btnMain) {
                    btnMain.innerText = '🛑 关闭自启';
                }
            } else {
                if (badge) {
                    badge.className = 'status-pill';
                    badge.style.background = '#f1f5f9';
                    badge.style.color = '#64748b';
                    badge.innerText = '● 未开启';
                }
                if (txt) {
                    txt.innerText = '未开启自启动';
                    txt.style.color = 'var(--text-main)';
                }
                if (btn) btn.innerText = '开启';
                if (btnMain) {
                    btnMain.innerText = '⚙️ 开机自启';
                }
            }
        }
    } catch (e) {
        console.error('Fetch autostart status error:', e);
    }
}

async function toggleWebAutostart() {
    const targetState = !currentAutostartEnabled;
    const actionName = targetState ? '开启开机自启动' : '取消开机自启动';
    if (!confirm(`确定要${actionName}吗？\n\n开启后，风电机组工控机在每次断电开机或系统重启时，都将全自动静默唤醒监控后台并在大屏打开系统。`)) {
        return;
    }

    try {
        const res = await fetch('/api/admin/toggle_autostart', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enable: targetState })
        });
        const json = await res.json();
        if (json.success) {
            alert('✅ ' + json.msg);
            fetchAutostartStatus();
        } else {
            alert('❌ 操作失败: ' + (json.error || json.msg || '未知错误'));
        }
    } catch (err) {
        alert('请求异常: ' + err);
    }
}

// ===========================================================================
// TAB 0: 数据分析与多维统计 (Analytics & Multi-Dimensional Charts)
// ===========================================================================
let currentAnalyticsDim = 'audio';
let currentAnalyticsRange = 'all';
let currentAnalyticsStartDate = '';
let currentAnalyticsEndDate = '';

let chartTrendInstance = null;
let chartTypesInstance = null;
let chartHourlyInstance = null;
let chartModelsInstance = null;
let chartReviewsInstance = null;
let chartPhysicsInstance = null;
let chartRmsDistInstance = null;
let chartRmsCategoryInstance = null;

const CHART_DEFAULTS = {
    fontFamily: "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
    gridColor: 'rgba(226, 232, 240, 0.7)',
    tickColor: '#64748b'
};

function setAnalyticsDimension(dim, btn) {
    currentAnalyticsDim = dim;
    document.querySelectorAll('#timeDimPills .admin-filter-pill').forEach(b => b.classList.remove('active'));
    if (btn) btn.classList.add('active');
    loadAnalyticsData();
}

function setAnalyticsRange(range, btn) {
    currentAnalyticsRange = range;
    document.querySelectorAll('#timeRangePills .admin-filter-pill').forEach(b => b.classList.remove('active'));
    if (btn) btn.classList.add('active');
    
    const customBox = document.getElementById('anaCustomDateBox');
    if (range === 'custom') {
        if (customBox) customBox.style.display = 'flex';
    } else {
        if (customBox) customBox.style.display = 'none';
        loadAnalyticsData();
    }
}

function applyCustomDateFilter() {
    const sInput = document.getElementById('anaStartDate');
    const eInput = document.getElementById('anaEndDate');
    currentAnalyticsStartDate = sInput ? sInput.value : '';
    currentAnalyticsEndDate = eInput ? eInput.value : '';
    if (currentAnalyticsStartDate && currentAnalyticsEndDate && currentAnalyticsStartDate > currentAnalyticsEndDate) {
        alert('起始日期不能大于结束日期！');
        return;
    }
    loadAnalyticsData();
}

async function loadAnalyticsData() {
    const catSelect = document.getElementById('anaCategorySelect') ? document.getElementById('anaCategorySelect').value : 'ALL';
    const url = `/api/admin/analytics?time_dim=${currentAnalyticsDim}&range=${currentAnalyticsRange}&start_date=${encodeURIComponent(currentAnalyticsStartDate)}&end_date=${encodeURIComponent(currentAnalyticsEndDate)}&consensus=${encodeURIComponent(catSelect)}`;

    try {
        const res = await fetch(url);
        const data = await res.json();
        if (!data || !data.success) {
            console.error('Failed to load analytics data:', data);
            return;
        }

        // 1. 同步时间选择器初始默认值
        const filters = data.filters || {};
        const sInput = document.getElementById('anaStartDate');
        const eInput = document.getElementById('anaEndDate');
        if (sInput && !sInput.value && filters.start_date) sInput.value = filters.start_date;
        if (eInput && !eInput.value && filters.end_date) eInput.value = filters.end_date;

        // 2. 渲染 6 大 KPI 卡片
        renderAnalyticsKPIs(data.kpis || {});

        // 3. 渲染 Chart.js 图表
        renderTrendChart(data.trend || {});
        renderTypesChart(data.types || {});
        renderHourlyChart(data.hourly || {});
        renderModelsChart(data.models || {});
        renderReviewsChart(data.reviews || {});
        renderPhysicsChart(data.physics || []);
        renderRmsDistChart(data.rms_analytics || {});
        renderRmsCategoryChart(data.rms_analytics || {});

        // 4. 渲染多维综合汇总表格
        renderMatrixTable(data.table_summary || [], filters.total_matched || 0);

    } catch (e) {
        console.error('loadAnalyticsData error:', e);
    }
}

function renderAnalyticsKPIs(kpis) {
    const setTxt = (id, val) => {
        const el = document.getElementById(id);
        if (el) el.innerText = val;
    };

    setTxt('anaStatTotal', kpis.total_samples || 0);
    setTxt('anaStatDays', kpis.distinct_dates_count || 0);
    setTxt('anaStatAlarm', kpis.alarm_count || 0);
    setTxt('anaStatAlarmRate', (kpis.alarm_rate_pct || 0) + '%');
    setTxt('anaStatUnanimous', kpis.unanimous_count || 0);
    setTxt('anaStatMajority', kpis.majority_count || 0);
    setTxt('anaStatShielded', kpis.shielded_count || 0);
    setTxt('anaStatShieldRate', (kpis.shielded_rate_pct || 0) + '%');
    setTxt('anaStatSafe', kpis.safe_count || 0);
    setTxt('anaStatNormal', kpis.normal_count || 0);
    setTxt('anaStatStates', (kpis.startup_count || 0) + (kpis.shutdown_count || 0));
    setTxt('anaStatClipped', kpis.clipped_count || 0);
    const clipRate = kpis.total_samples ? ((kpis.clipped_count / kpis.total_samples) * 100).toFixed(1) : 0;
    setTxt('anaStatClipRate', clipRate + '%');
    setTxt('anaStatReviewed', kpis.reviewed_count || 0);
    setTxt('anaStatReviewRate', (kpis.review_rate_pct || 0) + '%');
    setTxt('anaStatConfirmed', kpis.confirmed_collision_count || 0);
}

function renderTrendChart(trend) {
    const canvas = document.getElementById('chartTrendCanvas');
    if (!canvas || !window.Chart) return;

    if (chartTrendInstance) {
        chartTrendInstance.destroy();
        chartTrendInstance = null;
    }

    const ctx = canvas.getContext('2d');
    chartTrendInstance = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: trend.labels || [],
            datasets: trend.datasets || []
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: {
                mode: 'index',
                intersect: false
            },
            plugins: {
                legend: {
                    position: 'top',
                    labels: {
                        boxWidth: 12,
                        padding: 10,
                        font: { size: 11, family: CHART_DEFAULTS.fontFamily }
                    }
                },
                tooltip: {
                    backgroundColor: '#1e293b',
                    titleFont: { size: 12, weight: 'bold' },
                    bodyFont: { size: 11 },
                    padding: 10,
                    cornerRadius: 6
                }
            },
            scales: {
                x: {
                    stacked: true,
                    grid: { color: CHART_DEFAULTS.gridColor },
                    ticks: { color: CHART_DEFAULTS.tickColor, font: { size: 11, family: 'JetBrains Mono' } }
                },
                y: {
                    stacked: true,
                    beginAtZero: true,
                    grid: { color: CHART_DEFAULTS.gridColor },
                    ticks: { color: CHART_DEFAULTS.tickColor, font: { size: 11 }, precision: 0 }
                }
            }
        }
    });
}

function renderTypesChart(types) {
    const canvas = document.getElementById('chartTypesCanvas');
    if (!canvas || !window.Chart) return;

    if (chartTypesInstance) {
        chartTypesInstance.destroy();
        chartTypesInstance = null;
    }

    const ctx = canvas.getContext('2d');
    chartTypesInstance = new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels: types.labels || [],
            datasets: [{
                data: types.counts || [],
                backgroundColor: types.colors || [],
                borderWidth: 2,
                borderColor: '#ffffff',
                hoverOffset: 6
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            cutout: '62%',
            plugins: {
                legend: {
                    position: 'right',
                    labels: {
                        boxWidth: 12,
                        padding: 8,
                        font: { size: 11, family: CHART_DEFAULTS.fontFamily }
                    }
                },
                tooltip: {
                    backgroundColor: '#1e293b',
                    padding: 10,
                    cornerRadius: 6,
                    callbacks: {
                        label: function(context) {
                            const val = context.raw || 0;
                            const total = context.dataset.data.reduce((a, b) => a + b, 0);
                            const pct = total ? ((val / total) * 100).toFixed(1) : 0;
                            return ` ${context.label}: ${val} 份 (${pct}%)`;
                        }
                    }
                }
            }
        }
    });
}

function renderHourlyChart(hourly) {
    const canvas = document.getElementById('chartHourlyCanvas');
    if (!canvas || !window.Chart) return;

    if (chartHourlyInstance) {
        chartHourlyInstance.destroy();
        chartHourlyInstance = null;
    }

    const ctx = canvas.getContext('2d');
    chartHourlyInstance = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: hourly.labels || [],
            datasets: [
                {
                    label: '🔴 结构碰撞告警',
                    data: hourly.alarms || [],
                    backgroundColor: '#ef4444',
                    stack: 'Stack 0'
                },
                {
                    label: '🟡 扰动/变点/饱和',
                    data: hourly.disturbances || [],
                    backgroundColor: '#f59e0b',
                    stack: 'Stack 0'
                },
                {
                    label: '🟢 正常平稳底噪',
                    data: hourly.normals || [],
                    backgroundColor: '#10b981',
                    stack: 'Stack 0'
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
                legend: {
                    position: 'top',
                    labels: { boxWidth: 12, padding: 8, font: { size: 11 } }
                },
                tooltip: { backgroundColor: '#1e293b', padding: 10, cornerRadius: 6 }
            },
            scales: {
                x: {
                    stacked: true,
                    grid: { color: CHART_DEFAULTS.gridColor },
                    ticks: { color: CHART_DEFAULTS.tickColor, font: { size: 10, family: 'JetBrains Mono' }, maxRotation: 45 }
                },
                y: {
                    stacked: true,
                    beginAtZero: true,
                    grid: { color: CHART_DEFAULTS.gridColor },
                    ticks: { color: CHART_DEFAULTS.tickColor, precision: 0 }
                }
            }
        }
    });
}

function renderModelsChart(models) {
    const canvas = document.getElementById('chartModelsCanvas');
    if (!canvas || !window.Chart) return;

    if (chartModelsInstance) {
        chartModelsInstance.destroy();
        chartModelsInstance = null;
    }

    const hasCollision = Boolean(models.has_collision || (models.collision_avg_conf && models.collision_avg_conf.some(v => v !== null && v > 0)));
    const maxVotes = models.alarm_votes && models.alarm_votes.length ? Math.max(...models.alarm_votes) : 0;

    // 动态同步卡片小标题状态徽章
    const subtitleEl = canvas.closest('.chart-box')?.querySelector('.chart-subtitle');
    if (subtitleEl) {
        const baseTitle = '03K (Dasheng) / 03U (ECHOv2) / 03R (GRAM)';
        if (!hasCollision) {
            subtitleEl.innerHTML = `${baseTitle} <span style="margin-left:8px;padding:2px 7px;border-radius:4px;background:#ecfdf5;color:#059669;font-size:11px;font-weight:600;">🛡️ 全量底噪高置信放行 (0 告警)</span>`;
        } else {
            subtitleEl.innerHTML = `${baseTitle} <span style="margin-left:8px;padding:2px 7px;border-radius:4px;background:#fef2f2;color:#dc2626;font-size:11px;font-weight:600;">⚠️ 包含确诊碰撞事件对比</span>`;
        }
    }

    const ctx = canvas.getContext('2d');
    chartModelsInstance = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: models.labels || [],
            datasets: [
                {
                    label: '碰撞均值置信度 (%)',
                    data: models.collision_avg_conf || [],
                    backgroundColor: '#ef4444',
                    borderRadius: 4,
                    yAxisID: 'y'
                },
                {
                    label: '底噪均值置信度 (%)',
                    data: models.normal_avg_conf || [],
                    backgroundColor: '#10b981',
                    borderRadius: 4,
                    yAxisID: 'y'
                },
                {
                    label: '告警表决票数 (票)',
                    data: models.alarm_votes || [],
                    backgroundColor: '#3b82f6',
                    borderRadius: 4,
                    yAxisID: 'y1'
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'top',
                    labels: { 
                        boxWidth: 12, 
                        padding: 8, 
                        font: { size: 11, family: CHART_DEFAULTS.fontFamily } 
                    }
                },
                tooltip: { 
                    backgroundColor: '#1e293b', 
                    padding: 10, 
                    cornerRadius: 6,
                    callbacks: {
                        label: function(context) {
                            const dsLabel = context.dataset.label || '';
                            const val = context.raw;
                            if (dsLabel.includes('碰撞均值置信度')) {
                                if (val === null || val === undefined) {
                                    return `${dsLabel}: 时段内无确诊碰撞 (0起)`;
                                }
                                return `${dsLabel}: ${val}%`;
                            }
                            if (dsLabel.includes('底噪均值置信度')) {
                                return `${dsLabel}: ${val}% (高确信平稳正常)`;
                            }
                            return `${dsLabel}: ${val} 票`;
                        },
                        afterBody: function(contexts) {
                            if (!contexts || !contexts.length) return [];
                            const idx = contexts[0].dataIndex;
                            const inferMs = models.avg_infer_ms && models.avg_infer_ms[idx];
                            const lines = [];
                            if (inferMs) {
                                lines.push(`⚡ 单音频推理均耗: ${inferMs} ms`);
                            }
                            if (!hasCollision) {
                                lines.push('ℹ️ 模型性能评价: 深度特征极低碰撞概率，正常底噪辨识率极高');
                            }
                            return lines;
                        }
                    }
                }
            },
            scales: {
                x: {
                    grid: { color: CHART_DEFAULTS.gridColor },
                    ticks: { color: CHART_DEFAULTS.tickColor, font: { size: 11, weight: 'bold' } }
                },
                y: {
                    type: 'linear',
                    position: 'left',
                    beginAtZero: true,
                    max: 100,
                    grid: { color: CHART_DEFAULTS.gridColor },
                    ticks: { color: CHART_DEFAULTS.tickColor, callback: val => val + '%' }
                },
                y1: {
                    type: 'linear',
                    position: 'right',
                    display: maxVotes > 0, // 仅当真实产生了告警票数时才显示右侧 Y 轴，无票时彻底隐藏右侧 0~1 孤立刻度
                    beginAtZero: true,
                    suggestedMax: Math.max(10, maxVotes * 1.2),
                    grid: { drawOnChartArea: false },
                    ticks: { color: '#3b82f6', precision: 0 }
                }
            }
        }
    });
}

function renderReviewsChart(reviews) {
    const canvas = document.getElementById('chartReviewsCanvas');
    if (!canvas || !window.Chart) return;

    if (chartReviewsInstance) {
        chartReviewsInstance.destroy();
        chartReviewsInstance = null;
    }

    const ctx = canvas.getContext('2d');
    chartReviewsInstance = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: reviews.labels || [],
            datasets: [{
                label: '复核样本数',
                data: reviews.counts || [],
                backgroundColor: reviews.colors || [],
                borderRadius: 4
            }]
        },
        options: {
            indexAxis: 'y',
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false },
                tooltip: { backgroundColor: '#1e293b', padding: 10, cornerRadius: 6 }
            },
            scales: {
                x: {
                    beginAtZero: true,
                    grid: { color: CHART_DEFAULTS.gridColor },
                    ticks: { color: CHART_DEFAULTS.tickColor, precision: 0 }
                },
                y: {
                    grid: { display: false },
                    ticks: { color: CHART_DEFAULTS.tickColor, font: { size: 11, weight: '600' } }
                }
            }
        }
    });
}

function renderPhysicsChart(physicsPoints) {
    const canvas = document.getElementById('chartPhysicsCanvas');
    if (!canvas || !window.Chart) return;

    if (chartPhysicsInstance) {
        chartPhysicsInstance.destroy();
        chartPhysicsInstance = null;
    }

    const collisionGroup = [];
    const clippedGroup = [];
    const disturbanceGroup = [];
    const normalGroup = [];
    const stateGroup = [];

    (physicsPoints || []).forEach(pt => {
        if (pt.cat === 'CRITICAL_COLLISION_UNANIMOUS' || pt.cat === 'MAJORITY_COLLISION_ALARM') {
            collisionGroup.push(pt);
        } else if (pt.cat === 'HARDWARE_CLIPPING_DISCARD') {
            clippedGroup.push(pt);
        } else if (pt.cat === 'ISOLATED_DISTURBANCE_SAFE') {
            disturbanceGroup.push(pt);
        } else if (pt.cat === 'MACHINE_STARTUP_SAFE' || pt.cat === 'MACHINE_SHUTDOWN_SAFE') {
            stateGroup.push(pt);
        } else {
            normalGroup.push(pt);
        }
    });

    const ctx = canvas.getContext('2d');
    chartPhysicsInstance = new Chart(ctx, {
        type: 'scatter',
        data: {
            datasets: [
                {
                    label: '🔴 碰撞告警 (3票/2票)',
                    data: collisionGroup,
                    backgroundColor: 'rgba(239, 68, 68, 0.75)',
                    pointRadius: 5,
                    pointHoverRadius: 7
                },
                {
                    label: '🟣 幅值饱和',
                    data: clippedGroup,
                    backgroundColor: 'rgba(139, 92, 246, 0.75)',
                    pointRadius: 4.5,
                    pointHoverRadius: 7
                },
                {
                    label: '🟡 孤立扰动 (1票)',
                    data: disturbanceGroup,
                    backgroundColor: 'rgba(234, 179, 8, 0.75)',
                    pointRadius: 4,
                    pointHoverRadius: 6
                },
                {
                    label: '🚀 现场启停工况',
                    data: stateGroup,
                    backgroundColor: 'rgba(6, 182, 212, 0.75)',
                    pointRadius: 4.5,
                    pointHoverRadius: 6
                },
                {
                    label: '🟢 正常平稳底噪',
                    data: normalGroup,
                    backgroundColor: 'rgba(16, 185, 129, 0.4)',
                    pointRadius: 3,
                    pointHoverRadius: 5
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'top',
                    labels: { boxWidth: 12, padding: 10, font: { size: 11 } }
                },
                tooltip: {
                    backgroundColor: '#1e293b',
                    padding: 10,
                    cornerRadius: 6,
                    callbacks: {
                        label: function(context) {
                            const p = context.raw;
                            return ` [${p.cat}] ${p.fn} | 峰值: ${p.y} | RMS: ${p.x} | 置信度: ${p.conf}%`;
                        }
                    }
                }
            },
            scales: {
                x: {
                    title: { display: true, text: 'RMS 有效值 (能量基准)', color: CHART_DEFAULTS.tickColor, font: { size: 11, weight: 'bold' } },
                    grid: { color: CHART_DEFAULTS.gridColor },
                    ticks: { color: CHART_DEFAULTS.tickColor, font: { family: 'JetBrains Mono', size: 10 } }
                },
                y: {
                    title: { display: true, text: '峰值幅值 Peak (瞬态冲击)', color: CHART_DEFAULTS.tickColor, font: { size: 11, weight: 'bold' } },
                    grid: { color: CHART_DEFAULTS.gridColor },
                    ticks: { color: CHART_DEFAULTS.tickColor, font: { family: 'JetBrains Mono', size: 10 } }
                }
            }
        }
    });
}

function renderRmsDistChart(rmsAnalytics) {
    const canvas = document.getElementById('chartRmsDistCanvas');
    if (!canvas || !window.Chart) return;

    if (chartRmsDistInstance) {
        chartRmsDistInstance.destroy();
        chartRmsDistInstance = null;
    }

    const kpis = rmsAnalytics.kpis || {};
    const hist = rmsAnalytics.histogram || [];

    const setTxt = (id, val) => {
        const el = document.getElementById(id);
        if (el) el.innerText = val;
    };
    setTxt('rmsStatMedian', kpis.median !== undefined ? kpis.median : '--');
    setTxt('rmsStatAvg', kpis.avg !== undefined ? kpis.avg : '--');
    setTxt('rmsStatP95', kpis.p95 !== undefined ? kpis.p95 : '--');
    setTxt('rmsStatStd', kpis.std !== undefined ? kpis.std : '--');
    setTxt('rmsStatRange', (kpis.min !== undefined && kpis.max !== undefined) ? `${kpis.min} ~ ${kpis.max}` : '--');

    const labels = hist.map(h => `${h.label} (${h.tier_name})`);
    const counts = hist.map(h => h.count);
    const colors = hist.map(h => h.color);

    const ctx = canvas.getContext('2d');
    chartRmsDistInstance = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: labels,
            datasets: [{
                label: '音频切片样本数',
                data: counts,
                backgroundColor: colors,
                borderRadius: 4
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false },
                tooltip: {
                    backgroundColor: '#1e293b',
                    padding: 10,
                    cornerRadius: 6,
                    callbacks: {
                        label: function(context) {
                            const item = hist[context.dataIndex] || {};
                            return `样本数量: ${item.count || 0} 份 (占比 ${item.pct || 0}%)`;
                        },
                        afterLabel: function(context) {
                            const item = hist[context.dataIndex] || {};
                            return `工况特征: ${item.tier_name || ''}`;
                        }
                    }
                }
            },
            scales: {
                x: {
                    grid: { color: CHART_DEFAULTS.gridColor },
                    ticks: {
                        color: CHART_DEFAULTS.tickColor,
                        font: { size: 10, family: CHART_DEFAULTS.fontFamily },
                        maxRotation: 35,
                        minRotation: 20
                    }
                },
                y: {
                    beginAtZero: true,
                    grid: { color: CHART_DEFAULTS.gridColor },
                    ticks: {
                        color: CHART_DEFAULTS.tickColor,
                        font: { size: 11 },
                        precision: 0
                    }
                }
            }
        }
    });
}

function renderRmsCategoryChart(rmsAnalytics) {
    const canvas = document.getElementById('chartRmsCategoryCanvas');
    if (!canvas || !window.Chart) return;

    if (chartRmsCategoryInstance) {
        chartRmsCategoryInstance.destroy();
        chartRmsCategoryInstance = null;
    }

    const byCat = rmsAnalytics.by_category || [];
    const labels = byCat.map(c => c.name || c.full_name);
    const avgData = byCat.map(c => c.avg_rms);
    const p95Data = byCat.map(c => c.p95_rms);

    const ctx = canvas.getContext('2d');
    chartRmsCategoryInstance = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: labels,
            datasets: [
                {
                    label: '平均有效值 (Mean RMS)',
                    data: avgData,
                    backgroundColor: 'rgba(37, 99, 235, 0.85)',
                    borderRadius: 4
                },
                {
                    label: 'P95 高能阈值 (95th% RMS)',
                    data: p95Data,
                    backgroundColor: 'rgba(220, 38, 38, 0.85)',
                    borderRadius: 4
                }
            ]
        },
        options: {
            indexAxis: 'y',
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'top',
                    labels: { boxWidth: 12, padding: 10, font: { size: 11 } }
                },
                tooltip: {
                    backgroundColor: '#1e293b',
                    padding: 10,
                    cornerRadius: 6,
                    callbacks: {
                        afterBody: function(contexts) {
                            if (!contexts || contexts.length === 0) return '';
                            const idx = contexts[0].dataIndex;
                            const item = byCat[idx] || {};
                            return `样本总数: ${item.count} 份\n中位数: ${item.median_rms} | 最大值: ${item.max_rms}`;
                        }
                    }
                }
            },
            scales: {
                x: {
                    beginAtZero: true,
                    grid: { color: CHART_DEFAULTS.gridColor },
                    ticks: {
                        color: CHART_DEFAULTS.tickColor,
                        font: { size: 10, family: 'JetBrains Mono' }
                    }
                },
                y: {
                    grid: { display: false },
                    ticks: {
                        color: CHART_DEFAULTS.tickColor,
                        font: { size: 11, weight: '600' }
                    }
                }
            }
        }
    });
}

function renderMatrixTable(summaryList, totalMatched) {
    const tbody = document.getElementById('anaMatrixTableBody');
    const tfoot = document.getElementById('anaMatrixTableFoot');
    if (!tbody) return;

    if (!summaryList || summaryList.length === 0) {
        tbody.innerHTML = '<tr><td colspan="11" style="text-align:center;padding:30px;color:var(--text-muted)">所选时间范围无样本数据</td></tr>';
        if (tfoot) tfoot.innerHTML = '';
        return;
    }

    let rowsHtml = '';
    let sumCount = 0;
    let sumReviewed = 0;
    let sumConfirmed = 0;
    let weightedConfSum = 0;
    let weightedPeakSum = 0;
    let weightedRmsSum = 0;

    summaryList.forEach((row, i) => {
        sumCount += row.count;
        sumReviewed += row.reviewed_count;
        sumConfirmed += row.confirmed_collision_count;
        weightedConfSum += (row.avg_conf * row.count);
        weightedPeakSum += (row.avg_peak * row.count);
        weightedRmsSum += (row.avg_rms * row.count);

        rowsHtml += `
            <tr>
                <td style="font-family:var(--font-mono);font-size:11px;color:var(--text-muted);">${i + 1}</td>
                <td>
                    <span class="badge ${row.badge}" style="font-size:11.5px;padding:3px 8px;">${row.name}</span>
                </td>
                <td style="font-family:var(--font-mono);font-weight:700;">${row.count}</td>
                <td>
                    <div style="display:flex;align-items:center;gap:6px;">
                        <span style="font-family:var(--font-mono);font-size:11.5px;min-width:40px;">${row.pct}%</span>
                        <div style="flex:1;background:#e2e8f0;height:5px;border-radius:3px;overflow:hidden;min-width:50px;">
                            <div style="background:${row.color};height:100%;width:${row.pct}%;"></div>
                        </div>
                    </div>
                </td>
                <td style="font-family:var(--font-mono);">${row.avg_conf}%</td>
                <td style="font-family:var(--font-mono);">${row.avg_peak.toFixed(4)}</td>
                <td style="font-family:var(--font-mono);">${row.avg_rms.toFixed(5)}</td>
                <td style="font-family:var(--font-mono);">${row.avg_crest.toFixed(2)}</td>
                <td style="font-family:var(--font-mono);">${row.reviewed_count}</td>
                <td style="font-family:var(--font-mono);${row.confirmed_collision_count > 0 ? 'color:#dc2626;font-weight:bold;' : ''}">${row.confirmed_collision_count}</td>
                <td style="font-size:12px;color:var(--text-main);">${row.takeaway}</td>
            </tr>
        `;
    });

    tbody.innerHTML = rowsHtml;

    if (tfoot) {
        const avgConf = sumCount ? (weightedConfSum / sumCount).toFixed(2) : '0.00';
        const avgPeak = sumCount ? (weightedPeakSum / sumCount).toFixed(4) : '0.0000';
        const avgRms = sumCount ? (weightedRmsSum / sumCount).toFixed(5) : '0.00000';
        tfoot.innerHTML = `
            <tr>
                <td>-</td>
                <td>合计 / 系统加权均值</td>
                <td style="font-family:var(--font-mono);font-size:13px;color:#2563eb;">${sumCount}</td>
                <td style="font-family:var(--font-mono);">100%</td>
                <td style="font-family:var(--font-mono);">${avgConf}%</td>
                <td style="font-family:var(--font-mono);">${avgPeak}</td>
                <td style="font-family:var(--font-mono);">${avgRms}</td>
                <td>-</td>
                <td style="font-family:var(--font-mono);">${sumReviewed}</td>
                <td style="font-family:var(--font-mono);color:#dc2626;">${sumConfirmed}</td>
                <td style="color:var(--text-muted);font-weight:normal;font-size:11.5px;">纳管分析覆盖 ${totalMatched} 个声学切片</td>
            </tr>
        `;
    }
}

function exportAnalyticsCSV() {
    const catSelect = document.getElementById('anaCategorySelect') ? document.getElementById('anaCategorySelect').value : 'ALL';
    const url = `/api/admin/analytics/export_csv?time_dim=${currentAnalyticsDim}&range=${currentAnalyticsRange}&start_date=${encodeURIComponent(currentAnalyticsStartDate)}&end_date=${encodeURIComponent(currentAnalyticsEndDate)}&consensus=${encodeURIComponent(catSelect)}`;
    window.open(url, '_blank');
}
