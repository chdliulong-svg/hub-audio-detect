// -*- coding: utf-8 -*-
// 3 核心声学大模型实时监控客户端控制器 (03K + 03U + 03R 黄金铁三角)

let historyRecords = [];
let currentFilteredRecords = [];
let currentFilter = 'all';
let currentConsensusFilter = 'ALL';
let currentSearchQuery = '';
let lastSeenFilename = '';

// 历史记录表格分页控制器状态
let totalHistoryRecords = 0;
let totalHistoryPages = 1;
let currentHistoryPage = 1;
let historyPageSize = 15;

// 当前看板遥测对象与回放模式状态机
let currentHeroData = null;
let isPlaybackMode = false;
let currentPlaybackKey = '';

// 人工复核交互状态
let selectedReviewStatus = 'CONFIRMED_COLLISION';
let currentReviewRecord = null;

document.addEventListener('DOMContentLoaded', () => {
    fetchLatestTelemetry();
    fetchHistory();
    // 轮询周期: 每 1.5 秒更新一次实时流 (回放模式下仅在后台静默刷新台账数据)
    setInterval(fetchLatestTelemetry, 1500);
});

async function fetchLatestTelemetry() {
    try {
        const res = await fetch('/api/latest');
        const data = await res.json();
        
        // 算法运行/暂停状态同步
        if (data && data.is_algorithm_running !== undefined) {
            updateSystemRunningStatus(data.is_algorithm_running);
        }

        // 待测监听路径同步
        if (data && data.watch_dir) {
            const dirEl = document.getElementById('headerWatchDirCode');
            if (dirEl && dirEl.innerText !== data.watch_dir) {
                dirEl.innerText = data.watch_dir;
            }
        }

        // 如果处于历史回放模式，锁定当前看板，不被实时流覆盖
        if (isPlaybackMode) {
            return;
        }

        if (data && data.success && data.filename !== lastSeenFilename) {
            lastSeenFilename = data.filename;
            updateHeroView(data);
            // 仅在用户位于第 1 页且未开启筛选搜索时，静默刷新表格首屏
            if (currentHistoryPage === 1 && currentFilter === 'all' && currentConsensusFilter === 'ALL' && !currentSearchQuery) {
                fetchHistory(1);
            }
        }
    } catch (e) {
        console.error('Fetch latest error:', e);
    }
}

function updateSystemRunningStatus(isRunning) {
    const ind = document.getElementById('sysOnlineIndicator');
    const txt = document.getElementById('sysOnlineText');
    if (!ind || !txt) return;

    if (isRunning) {
        ind.className = 'status-indicator online';
        txt.innerText = '实时在线监测中 (ONLINE)';
    } else {
        ind.className = 'status-indicator paused';
        txt.innerText = '算法已暂停检测 (PAUSED)';
    }
}

async function fetchHistory(targetPage) {
    if (targetPage !== undefined) {
        currentHistoryPage = parseInt(targetPage, 10) || 1;
    }

    // 映射工况与复核状态参数给后端 SQLite 极速索引检索
    let consensusParam = 'ALL';
    let reviewParam = 'ALL';

    if (currentConsensusFilter && currentConsensusFilter !== 'ALL') {
        consensusParam = currentConsensusFilter;
    } else if (currentFilter && currentFilter !== 'all') {
        const filterMap = {
            'vote3': 'VOTE3',
            'vote2': 'VOTE2',
            'vote1': 'VOTE1',
            'shielded': 'VOTE1',
            'vote0': 'VOTE0',
            'normal': 'VOTE0',
            'alarms': 'ALARMS',
            'startup': 'STARTUP',
            'shutdown': 'SHUTDOWN'
        };
        if (filterMap[currentFilter]) {
            consensusParam = filterMap[currentFilter];
        } else if (currentFilter === 'unreviewed') {
            reviewParam = 'UNREVIEWED';
        } else if (currentFilter === 'reviewed') {
            reviewParam = 'REVIEWED';
        }
    }

    const queryParams = new URLSearchParams({
        page: currentHistoryPage,
        page_size: historyPageSize,
        consensus: consensusParam,
        review_status: reviewParam,
        search: currentSearchQuery || ''
    });

    try {
        const res = await fetch(`/api/history?${queryParams.toString()}`);
        const data = await res.json();
        if (data && data.success) {
            totalHistoryRecords = (data.total_matched !== undefined) ? data.total_matched : (data.total || 0);
            totalHistoryPages = data.total_pages || Math.max(1, Math.ceil(totalHistoryRecords / historyPageSize));
            currentFilteredRecords = data.records || [];

            // 保持首屏热数据同步，以便模态框或顶栏兜底检索
            if (currentHistoryPage === 1 && consensusParam === 'ALL' && reviewParam === 'ALL' && !currentSearchQuery) {
                historyRecords = currentFilteredRecords;
            }

            if (data.kpi) {
                updateKPICounters(data.kpi);
            }

            // 联动更新主表格分类横幅数量
            const banner = document.getElementById('kpiTabActiveBanner');
            if (banner && banner.style.display !== 'none') {
                const countEl = document.getElementById('tabBannerCount');
                if (countEl) countEl.innerText = `${totalHistoryRecords} 份文件`;
            }

            renderHistoryTable();
        }
    } catch (e) {
        console.error('Fetch history error:', e);
    }
}

function updateKPICounters(kpiOrRecords) {
    let total = 0, unanimous = 0, majority = 0, shielded = 0, normal = 0, startup = 0, shutdown = 0;
    if (kpiOrRecords && typeof kpiOrRecords === 'object' && !Array.isArray(kpiOrRecords)) {
        total = kpiOrRecords.total || 0;
        unanimous = kpiOrRecords.unanimous || 0;
        majority = kpiOrRecords.majority || 0;
        shielded = kpiOrRecords.shielded || 0;
        normal = kpiOrRecords.normal || 0;
        startup = kpiOrRecords.startup || 0;
        shutdown = kpiOrRecords.shutdown || 0;
    } else if (Array.isArray(kpiOrRecords)) {
        total = kpiOrRecords.length;
        unanimous = kpiOrRecords.filter(r => r.alarm_votes === 3).length;
        majority = kpiOrRecords.filter(r => r.alarm_votes === 2 && r.is_alarm).length;
        shielded = kpiOrRecords.filter(r => r.consensus_level === 'ISOLATED_DISTURBANCE_SAFE').length;
        normal = kpiOrRecords.filter(r => r.consensus_level === 'NORMAL_CLEAN_NOISE').length;
        startup = kpiOrRecords.filter(r => r.consensus_level === 'MACHINE_STARTUP_SAFE').length;
        shutdown = kpiOrRecords.filter(r => r.consensus_level === 'MACHINE_SHUTDOWN_SAFE').length;
    }

    if (document.getElementById('kpiTotal')) document.getElementById('kpiTotal').innerHTML = `${total} <span class="kpi-unit">份</span>`;
    if (document.getElementById('kpiUnanimous')) document.getElementById('kpiUnanimous').innerHTML = `${unanimous} <span class="kpi-unit">份</span>`;
    if (document.getElementById('kpiMajority')) document.getElementById('kpiMajority').innerHTML = `${majority} <span class="kpi-unit">份</span>`;
    if (document.getElementById('kpiShielded')) document.getElementById('kpiShielded').innerHTML = `${shielded} <span class="kpi-unit">次</span>`;
    if (document.getElementById('kpiNormal')) document.getElementById('kpiNormal').innerHTML = `${normal} <span class="kpi-unit">份</span>`;
    if (document.getElementById('kpiStartup')) document.getElementById('kpiStartup').innerHTML = `${startup} <span class="kpi-unit">次</span>`;
    if (document.getElementById('kpiShutdown')) document.getElementById('kpiShutdown').innerHTML = `${shutdown} <span class="kpi-unit">次</span>`;
}

function updateHeroView(data) {
    if (!data) return;
    currentHeroData = data;

    // 1. 仲裁决策横幅 (Decision Banner)
    const banner = document.getElementById('decisionBanner');
    banner.className = 'decision-banner';
    
    let icon = '🍃';
    if (data.consensus_level === 'CRITICAL_COLLISION_UNANIMOUS') {
        banner.classList.add('state-crit');
        icon = '🚨';
    } else if (data.consensus_level === 'MAJORITY_COLLISION_ALARM') {
        banner.classList.add('state-high');
        icon = '⚠️';
    } else if (data.consensus_level === 'ISOLATED_DISTURBANCE_SAFE') {
        banner.classList.add('state-safe');
        icon = '🛡️';
    } else if (data.consensus_level === 'MACHINE_STARTUP_SAFE') {
        banner.classList.add('state-startup');
        icon = '🚀';
    } else if (data.consensus_level === 'MACHINE_SHUTDOWN_SAFE') {
        banner.classList.add('state-shutdown');
        icon = '🛑';
    } else if (data.consensus_level === 'HARDWARE_CLIPPING_DISCARD') {
        banner.classList.add('state-crit');
        icon = '⚡';
    } else {
        banner.classList.add('state-normal');
        icon = '🍃';
    }

    document.getElementById('bannerIcon').innerText = icon;
    document.getElementById('bannerLevel').innerText = data.consensus_level || 'NORMAL_CLEAN_NOISE';
    document.getElementById('bannerTitle').innerText = data.consensus_title || '正常放行';
    document.getElementById('bannerVotes').innerText = `${data.alarm_votes !== undefined ? data.alarm_votes : 0} / 3 票`;
    document.getElementById('bannerMeanConf').innerText = `${(data.mean_confidence_pct || 0.0).toFixed(1)}%`;

    // 1.1 人工复核状态徽标同步
    const heroReviewBadge = document.getElementById('heroReviewBadge');
    if (heroReviewBadge) {
        const rev = data.review || { status: 'UNREVIEWED', status_text: '待复核', badge_class: 'badge-unreviewed' };
        heroReviewBadge.className = `badge ${rev.badge_class || 'badge-unreviewed'}`;
        heroReviewBadge.innerText = rev.status_text || '待复核';
        heroReviewBadge.title = rev.reviewer ? `复核人: ${rev.reviewer} (${rev.reviewed_at || ''})${rev.notes ? ' - ' + rev.notes : ''}` : '尚未经过人工专家复核';
    }

    // 2. 3 核心模型置信度与表决结果
    for (const code of ['03K', '03U', '03R']) {
        const score = (data.scores && data.scores[code] !== undefined) ? data.scores[code] : 0.0;
        const isVoted = (data.alarms && data.alarms[code] === 1);
        
        const fillEl = document.getElementById(`mFill-${code}`);
        const scoreEl = document.getElementById(`mScore-${code}`);
        const voteEl = document.getElementById(`mVote-${code}`);
        
        if (fillEl) {
            fillEl.style.width = `${Math.min(100, Math.max(0, score))}%`;
            fillEl.style.background = score >= 90 ? '#ef4444' : (score >= 50 ? '#f97316' : '#3b82f6');
        }
        
        if (scoreEl) {
            scoreEl.innerText = `${score.toFixed(1)}%`;
            scoreEl.style.color = score >= 90 ? '#f87171' : (score >= 50 ? '#fb923c' : '#93c5fd');
        }
        
        if (voteEl) {
            if (isVoted) {
                voteEl.innerText = '🔴 投告警票';
                voteEl.className = 'm-vote-badge active-vote';
            } else {
                voteEl.innerText = '🟢 未投告警';
                voteEl.className = 'm-vote-badge';
            }
        }
    }

    // 3. 声学物理特征指标
    document.getElementById('valFilename').innerText = data.filename || '--';
    document.getElementById('valPeak').innerText = data.peak_amplitude !== undefined ? data.peak_amplitude.toFixed(3) : '--';
    document.getElementById('valRms').innerText = data.rms_amplitude !== undefined ? data.rms_amplitude.toFixed(4) : '--';
    document.getElementById('valCrest').innerText = data.crest_factor !== undefined ? data.crest_factor.toFixed(1) : '--';
    
    const clipEl = document.getElementById('valClip');
    if (clipEl) {
        clipEl.innerText = data.is_clipped ? `幅值饱和 (${data.rail_voltage}V)` : '未饱和 (正常)';
        clipEl.style.color = data.is_clipped ? '#f43f5e' : '#34d399';
    }
    
    document.getElementById('valInferTime').innerText = data.infer_ms ? `${data.infer_ms} ms` : '--';

    // 4. 音频试听播放器同步更新
    if (data.wav_file) {
        const player = document.getElementById('audioPlayer');
        if (player) {
            // 加载带时间戳的音频防止浏览器缓存 (使用 encodeURIComponent 保证包含 #、空格等特殊字符文件名正常解析)
            player.src = `/records/${encodeURIComponent(data.wav_file)}?t=${Date.now()}`;
            player.load();
        }
    }

    // 5. 声学时频谱诊断图同步强制更新
    const plotImg = document.getElementById('diagnosticPlotImg');
    const placeholder = document.getElementById('plotPlaceholder');
    const plotFileBadge = document.getElementById('plotFileBadge');
    const plotSection = document.getElementById('plotPanelSection');

    if (plotFileBadge) {
        plotFileBadge.innerText = `${data.filename || ''} (${data.timestamp ? data.timestamp.slice(11) : ''})`;
    }

    if (data.plot_file) {
        // 使用 encodeURIComponent 保证包含 #、空格等特殊字符文件名正常请求
        const freshPlotUrl = `/records/${encodeURIComponent(data.plot_file)}?t=${Date.now()}`;
        
        // 视觉平滑过渡与强制重载
        plotImg.classList.add('loading');
        plotImg.onload = () => {
            plotImg.classList.remove('loading');
            plotImg.style.display = 'block';
            if (placeholder) placeholder.style.display = 'none';
        };
        plotImg.onerror = () => {
            plotImg.classList.remove('loading');
            console.warn('诊断图加载异常:', freshPlotUrl);
        };
        plotImg.src = freshPlotUrl;

        // 如果放大弹窗当前处于开启状态，也同步更新弹窗中的高清大图
        const modalImg = document.getElementById('modalImg');
        const modalBox = document.getElementById('imageModal');
        if (modalImg && modalBox && modalBox.style.display === 'flex') {
            modalImg.src = freshPlotUrl;
        }

        // 面板高亮微闪提示用户图谱已更新
        if (plotSection) {
            plotSection.classList.add('highlight-flash');
            setTimeout(() => plotSection.classList.remove('highlight-flash'), 600);
        }
    } else {
        if (plotImg) plotImg.style.display = 'none';
        if (placeholder) placeholder.style.display = 'block';
    }
}

// ---------------------------------------------------------------------------
// 历史记录表格渲染与回放控制器
// ---------------------------------------------------------------------------
function renderHistoryTable() {
    const tbody = document.getElementById('historyTableBody');
    if (!tbody) return;

    // 页面边界校验
    if (currentHistoryPage > totalHistoryPages) currentHistoryPage = totalHistoryPages;
    if (currentHistoryPage < 1) currentHistoryPage = 1;

    const startIdx = (currentHistoryPage - 1) * historyPageSize;
    const endIdx = startIdx + currentFilteredRecords.length;

    const searchTip = currentSearchQuery ? ` (搜索: "${currentSearchQuery}")` : '';
    const countEl = document.getElementById('historyCount');
    if (countEl) {
        countEl.innerText = `共 ${totalHistoryRecords} 条记录${searchTip}`;
    }

    // 实时更新分页控件状态
    updateHistoryPaginationUI(totalHistoryRecords, totalHistoryPages, startIdx, endIdx);

    if (currentFilteredRecords.length === 0) {
        tbody.innerHTML = `<tr><td colspan="12" style="text-align:center;color:var(--text-muted);padding:24px">无匹配记录${currentSearchQuery ? ' (未找到包含关键词的诊断事件)' : ''}</td></tr>`;
        return;
    }

    tbody.innerHTML = currentFilteredRecords.map((r, i) => {
        const globalIdx = startIdx + i;
        const itemKey = `${r.filename}_${r.timestamp}`;
        const isCurrentActive = isPlaybackMode && (itemKey === currentPlaybackKey || r.filename === currentPlaybackKey);
        const rowClass = isCurrentActive ? 'class="active-replay-row"' : '';
        const rev = r.review || { status: 'UNREVIEWED', status_text: '待复核', badge_class: 'badge-unreviewed' };
        const revNoteTip = rev.notes ? `\n备注: ${rev.notes}` : '';
        const revTip = rev.reviewer ? `复核人: ${rev.reviewer} (${rev.reviewed_at || ''})${revNoteTip}` : '待复核';

        return `
        <tr ${rowClass}>
            <td>${globalIdx + 1}</td>
            <td style="font-family:var(--font-mono);color:var(--text-muted)">${r.timestamp ? r.timestamp.slice(11) : '--'}</td>
            <td style="font-family:var(--font-mono);font-weight:700">${r.filename}</td>
            <td><span class="badge ${r.consensus_badge}">${r.alarm_votes}/3 票</span></td>
            <td style="font-family:var(--font-mono);font-weight:700;color:${(r.mean_confidence_pct || 0) >= 90 ? '#ef4444' : '#f97316'}">${(r.mean_confidence_pct || 0).toFixed(1)}%</td>
            <td style="font-family:var(--font-mono)">${r.scores && r.scores['03K'] !== undefined ? r.scores['03K'].toFixed(1) : '0.0'}%</td>
            <td style="font-family:var(--font-mono)">${r.scores && r.scores['03U'] !== undefined ? r.scores['03U'].toFixed(1) : '0.0'}%</td>
            <td style="font-family:var(--font-mono)">${r.scores && r.scores['03R'] !== undefined ? r.scores['03R'].toFixed(1) : '0.0'}%</td>
            <td style="font-family:var(--font-mono)">${r.peak_amplitude !== undefined ? r.peak_amplitude.toFixed(3) : '--'} / ${r.rms_amplitude !== undefined ? r.rms_amplitude.toFixed(4) : '--'}</td>
            <td><span class="badge ${r.consensus_badge}">${r.consensus_level}</span></td>
            <td>
                <span class="badge ${rev.badge_class || 'badge-unreviewed'}" title="${revTip}" style="cursor:pointer" onclick="openReviewModalByIndex(${i})">
                    ${rev.status_text || '待复核'}
                </span>
                ${rev.reviewer ? `<div style="font-size:10px;color:var(--text-muted);margin-top:2px;font-family:var(--font-mono)">${rev.reviewer}</div>` : ''}
            </td>
            <td style="white-space:nowrap">
                <button class="btn btn-outline" style="padding:3px 8px;font-size:11px;font-weight:600" onclick="openReviewModalByIndex(${i})" title="进行人工复核判定">
                    ✍️ 复核
                </button>
                <button class="btn btn-outline" style="padding:3px 8px;font-size:11px;font-weight:600" onclick="playbackRecord(${i})">
                    ${isCurrentActive ? '👀 回放中' : '⏮ 回放'}
                </button>
            </td>
        </tr>
        `;
    }).join('');
}

// ---------------------------------------------------------------------------
// 历史记录表格分页控制器
// ---------------------------------------------------------------------------
function updateHistoryPaginationUI(totalCount, totalPages, startIdx, endIdx) {
    const curEl = document.getElementById('histPageCurrent');
    const totEl = document.getElementById('histPageTotal');
    const showEl = document.getElementById('histPageShowing');
    const cntEl = document.getElementById('histTotalCount');
    const jumpInput = document.getElementById('histJumpPageInput');

    if (curEl) curEl.innerText = totalCount === 0 ? 0 : currentHistoryPage;
    if (totEl) totEl.innerText = totalPages;
    if (showEl) showEl.innerText = Math.max(0, endIdx - startIdx);
    if (cntEl) cntEl.innerText = totalCount;
    if (jumpInput) {
        jumpInput.max = totalPages;
        jumpInput.value = currentHistoryPage;
    }

    const btnFirst = document.getElementById('btnHistFirst');
    const btnPrev = document.getElementById('btnHistPrev');
    const btnNext = document.getElementById('btnHistNext');
    const btnLast = document.getElementById('btnHistLast');

    if (btnFirst) btnFirst.disabled = (currentHistoryPage <= 1);
    if (btnPrev) btnPrev.disabled = (currentHistoryPage <= 1);
    if (btnNext) btnNext.disabled = (currentHistoryPage >= totalPages || totalCount === 0);
    if (btnLast) btnLast.disabled = (currentHistoryPage >= totalPages || totalCount === 0);

    // 智能渲染页码数字按钮 (当前页前后滑动窗口)
    const numContainer = document.getElementById('histPageNumberButtons');
    if (!numContainer) return;

    if (totalCount === 0 || totalPages <= 1) {
        numContainer.innerHTML = totalCount > 0 ? `<button class="page-num-btn active">1</button>` : '';
        return;
    }

    let pagesToRender = [];
    if (totalPages <= 7) {
        for (let p = 1; p <= totalPages; p++) pagesToRender.push(p);
    } else {
        pagesToRender.push(1);
        let start = Math.max(2, currentHistoryPage - 1);
        let end = Math.min(totalPages - 1, currentHistoryPage + 1);

        if (currentHistoryPage <= 3) {
            start = 2;
            end = 4;
        } else if (currentHistoryPage >= totalPages - 2) {
            start = totalPages - 3;
            end = totalPages - 1;
        }

        if (start > 2) pagesToRender.push('...');
        for (let p = start; p <= end; p++) pagesToRender.push(p);
        if (end < totalPages - 1) pagesToRender.push('...');
        pagesToRender.push(totalPages);
    }

    numContainer.innerHTML = pagesToRender.map(p => {
        if (p === '...') return `<span class="page-ellipsis">...</span>`;
        const activeClass = (p === currentHistoryPage) ? 'active' : '';
        return `<button class="page-num-btn ${activeClass}" onclick="gotoHistoryPage(${p})">${p}</button>`;
    }).join('');
}

function changeHistoryPage(delta) {
    const target = currentHistoryPage + delta;
    if (target >= 1 && target <= totalHistoryPages) {
        fetchHistory(target);
    }
}

function gotoHistoryPage(p) {
    const pageNum = parseInt(p, 10);
    if (!isNaN(pageNum) && pageNum >= 1 && pageNum <= totalHistoryPages) {
        fetchHistory(pageNum);
    }
}

function gotoHistoryLastPage() {
    gotoHistoryPage(totalHistoryPages);
}

function changeHistoryPageSize(newSize) {
    historyPageSize = parseInt(newSize, 10) || 15;
    fetchHistory(1);
}

function jumpToHistoryPage() {
    const input = document.getElementById('histJumpPageInput');
    if (!input) return;
    const val = parseInt(input.value, 10);
    if (!isNaN(val)) {
        gotoHistoryPage(val);
    }
}

// 点击回放指定历史记录
function playbackRecord(index) {
    const record = currentFilteredRecords[index];
    if (!record) return;

    // 激活历史回放模式
    isPlaybackMode = true;
    currentPlaybackKey = `${record.filename}_${record.timestamp}`;

    // 更新回放提示横幅
    const noticeBar = document.getElementById('playbackNotice');
    if (noticeBar) {
        noticeBar.style.display = 'flex';
        document.getElementById('playbackFileName').innerText = record.filename;
        document.getElementById('playbackTime').innerText = record.timestamp;
    }

    // 同步更新看板所有指标、音频试听与声学时频谱诊断图
    updateHeroView(record);

    // 重新渲染表格以高亮当前被选中的行
    renderHistoryTable();

    // 优雅平滑回滚到顶部监控看板
    const banner = document.getElementById('decisionBanner');
    if (banner) {
        banner.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
}

// 退出回放模式，返回实时监测流
function exitPlaybackMode() {
    isPlaybackMode = false;
    currentPlaybackKey = '';

    const noticeBar = document.getElementById('playbackNotice');
    if (noticeBar) {
        noticeBar.style.display = 'none';
    }

    // 重新拉取最新遥测数据并刷新看板与表格高亮
    lastSeenFilename = '';
    fetchLatestTelemetry();
    renderHistoryTable();
}

// ---------------------------------------------------------------------------
// KPI 选项卡分类配置与模态详细文件信息控制器
// ---------------------------------------------------------------------------
const KPI_TAB_CONFIG = {
    'all': {
        name: '累计处理音频 (全部清单)',
        icon: '📊',
        desc: '现场 20s 声学切片连续监测全部历史样本全景列表',
        filterKey: 'all',
        filterCheck: (r) => true
    },
    'vote3': {
        name: '3/3 票全票确诊碰撞',
        icon: '🚨',
        desc: '03K + 03U + 03R 三大模型全票确诊重大金属碰撞，触发最高级别红色告警',
        filterKey: 'vote3',
        filterCheck: (r) => r.alarm_votes === 3 || r.consensus_level === 'CRITICAL_COLLISION_UNANIMOUS'
    },
    'vote2': {
        name: '2/3 票多数确诊碰撞',
        icon: '⚠️',
        desc: '两项模型投票确诊且均值置信度 ≥ 80%，多数表决通过，触发高优先级橙警',
        filterKey: 'vote2',
        filterCheck: (r) => r.alarm_votes === 2 || r.consensus_level === 'MAJORITY_COLLISION_ALARM'
    },
    'vote1': {
        name: '孤立扰动门禁拦截',
        icon: '🛡️',
        desc: '仅 1/3 票偶发扰动触发，零误报协同仲裁门禁精准拦截放行，杜绝现场偶发误报',
        filterKey: 'vote1',
        filterCheck: (r) => r.alarm_votes === 1 || r.consensus_level === 'ISOLATED_DISTURBANCE_SAFE'
    },
    'vote0': {
        name: '正常平稳底噪放行',
        icon: '🍃',
        desc: '0/3 票全票安全放行，机组平稳运行背景底噪',
        filterKey: 'vote0',
        filterCheck: (r) => r.alarm_votes === 0 || r.consensus_level === 'NORMAL_CLEAN_NOISE'
    },
    'startup': {
        name: '现场机组启动工况',
        icon: '🚀',
        desc: 'CPD 变点检测算法检出阶跃爬升工况，判定为起机并网提速，安全放行不误报',
        filterKey: 'startup',
        filterCheck: (r) => r.consensus_level === 'MACHINE_STARTUP_SAFE' || (r.review && r.review.status === 'CONFIRMED_STARTUP')
    },
    'shutdown': {
        name: '现场机组停机工况',
        icon: '🛑',
        desc: 'CPD 变点检测算法检出能量单向跳水跌落工况，判定为刹车脱网或惰行停机，安全放行不误报',
        filterKey: 'shutdown',
        filterCheck: (r) => r.consensus_level === 'MACHINE_SHUTDOWN_SAFE' || (r.review && r.review.status === 'CONFIRMED_SHUTDOWN')
    }
};

let currentKpiModalTab = 'all';
let currentKpiModalSearch = '';

// 点击顶部任意 KPI 选项卡触发详细文件信息展示 (兼顾弹窗沉浸浏览与主表格联动)
function selectKpiTab(tabKey, cardEl, openModal = true) {
    currentFilter = tabKey;
    currentConsensusFilter = 'ALL';
    currentHistoryPage = 1;

    // 1. 同步顶部选项卡高亮样式
    document.querySelectorAll('.kpi-card').forEach(c => c.classList.remove('active-tab'));
    if (!cardEl) {
        cardEl = document.getElementById(`kpiCard-${tabKey}`);
    }
    if (cardEl) {
        cardEl.classList.add('active-tab');
    }

    // 2. 联动激活主表格过滤按钮组
    document.querySelectorAll('.filter-btn').forEach(btn => {
        const oc = btn.getAttribute('onclick') || '';
        btn.classList.toggle('active', oc.includes(`'${tabKey}'`));
    });

    // 3. 联动同步表头工况下拉选框
    const thSel = document.getElementById('thConsensusSelect');
    if (thSel) {
        if (tabKey === 'vote0') thSel.value = 'NORMAL_CLEAN_NOISE';
        else if (tabKey === 'vote3') thSel.value = 'CRITICAL_COLLISION_UNANIMOUS';
        else if (tabKey === 'vote2') thSel.value = 'MAJORITY_COLLISION_ALARM';
        else if (tabKey === 'vote1') thSel.value = 'ISOLATED_DISTURBANCE_SAFE';
        else if (tabKey === 'startup') thSel.value = 'MACHINE_STARTUP_SAFE';
        else if (tabKey === 'shutdown') thSel.value = 'MACHINE_SHUTDOWN_SAFE';
        else thSel.value = 'ALL';
    }

    // 4. 同步主表格横幅提示条
    const banner = document.getElementById('kpiTabActiveBanner');
    if (banner) {
        if (tabKey === 'all') {
            banner.style.display = 'none';
        } else {
            const cfg = KPI_TAB_CONFIG[tabKey] || KPI_TAB_CONFIG['all'];
            banner.style.display = 'flex';
            document.getElementById('tabBannerName').innerText = `${cfg.icon} ${cfg.name}`;
            const count = historyRecords.filter(cfg.filterCheck).length;
            document.getElementById('tabBannerCount').innerText = `${count} 份文件`;
        }
    }

    // 5. 渲染页面主表格
    renderHistoryTable();

    // 6. 弹出分类详细文件信息模态清单 (提供沉浸式文件浏览、回放与图谱查看)
    if (openModal) {
        openKpiDetailModal(tabKey);
    }
}

let kpiModalRecordsMap = {};

// 打开分类详细文件信息模态框
async function openKpiDetailModal(tabKey) {
    currentKpiModalTab = tabKey || 'all';
    currentKpiModalSearch = '';
    const searchInput = document.getElementById('kpiModalSearchInput');
    if (searchInput) searchInput.value = '';

    renderKpiModalTable();
    const modal = document.getElementById('kpiDetailModal');
    if (modal) modal.style.display = 'flex';

    // 若需要查看具体分类且数据量可能超过本地热缓存，异步从 SQLite 补充获取
    if (tabKey && tabKey !== 'all') {
        try {
            const res = await fetch(`/api/history?limit=500&consensus=${tabKey}`);
            const data = await res.json();
            if (data && data.success && data.records) {
                kpiModalRecordsMap[tabKey] = data.records;
                renderKpiModalTable();
            }
        } catch (e) {
            console.error('Fetch modal KPI records error:', e);
        }
    }
}

// 关闭模态框
function closeKpiDetailModal() {
    const modal = document.getElementById('kpiDetailModal');
    if (modal) modal.style.display = 'none';
}

// 再次打开当前锁定的选项卡弹窗
function reopenCurrentKpiModal() {
    openKpiDetailModal(currentFilter);
}

// 弹窗内部关键词快速搜索过滤
function filterKpiModalRecords(val) {
    currentKpiModalSearch = val ? val.trim().toLowerCase() : '';
    renderKpiModalTable();
}

// 渲染模态框中的详细文件列表
function renderKpiModalTable() {
    const cfg = KPI_TAB_CONFIG[currentKpiModalTab] || KPI_TAB_CONFIG['all'];
    const modalTitle = document.getElementById('kpiModalTitle');
    const modalIcon = document.getElementById('kpiModalIcon');
    const modalDesc = document.getElementById('kpiModalDesc');
    const modalTableBody = document.getElementById('kpiModalTableBody');
    const currentCountEl = document.getElementById('kpiModalCurrentCount');
    const totalCountEl = document.getElementById('kpiModalTotalCount');

    if (modalTitle) modalTitle.innerText = `${cfg.name} · 详细文件信息清单`;
    if (modalIcon) modalIcon.innerText = cfg.icon;

    // 1. 获取当前分类的基础样本集 (优先使用从后端 SQLite 按需加载的完整分类记录)
    const baseRecords = kpiModalRecordsMap[currentKpiModalTab] || historyRecords.filter(cfg.filterCheck);
    if (totalCountEl) totalCountEl.innerText = baseRecords.length;

    // 2. 搜索过滤
    const q = currentKpiModalSearch;
    const qNoHyphen = q.replace(/[-\s_]/g, '');
    const filteredRecords = baseRecords.filter(r => {
        if (!q) return true;
        const fn = (r.filename || '').toLowerCase();
        const ts = (r.timestamp || '').toLowerCase();
        const level = (r.consensus_level || '').toLowerCase();
        const title = (r.consensus_title || '').toLowerCase();
        const rev = r.review || {};
        const revStatus = (rev.status_text || '').toLowerCase();
        const reviewer = (rev.reviewer || '').toLowerCase();

        return fn.includes(q) || (qNoHyphen && fn.replace(/[-\s_]/g, '').includes(qNoHyphen)) ||
               ts.includes(q) || (qNoHyphen && ts.replace(/[-\s_]/g, '').includes(qNoHyphen)) ||
               level.includes(q) || title.includes(q) ||
               revStatus.includes(q) || reviewer.includes(q);
    });

    if (currentCountEl) currentCountEl.innerText = filteredRecords.length;
    if (modalDesc) modalDesc.innerText = `${cfg.desc} (该分类共有 ${baseRecords.length} 份音频样本)`;

    if (filteredRecords.length === 0) {
        modalTableBody.innerHTML = `<tr><td colspan="12" style="text-align:center;padding:36px;color:var(--text-muted)">
            ${baseRecords.length === 0 ? '该分类下暂无已诊断的文件记录' : '未找到匹配搜索关键词的音频文件'}
        </td></tr>`;
        return;
    }

    modalTableBody.innerHTML = filteredRecords.map((r, i) => {
        const itemKey = `${r.filename}_${r.timestamp}`;
        const isCurrentActive = isPlaybackMode && (itemKey === currentPlaybackKey);
        const rowClass = isCurrentActive ? 'class="active-replay-row"' : '';
        const rev = r.review || { status: 'UNREVIEWED', status_text: '待复核', badge_class: 'badge-unreviewed' };
        const revTip = rev.reviewer ? `复核人: ${rev.reviewer} (${rev.reviewed_at || ''})` : '待复核';

        return `
        <tr ${rowClass}>
            <td>${i + 1}</td>
            <td style="font-family:var(--font-mono);color:var(--text-muted);white-space:nowrap">${r.timestamp ? r.timestamp.slice(11) : '--'}</td>
            <td style="font-family:var(--font-mono);font-weight:700" title="${r.filename}">${r.filename}</td>
            <td><span class="badge ${r.consensus_badge}">${r.alarm_votes}/3 票</span></td>
            <td style="font-family:var(--font-mono);font-weight:700;color:${(r.mean_confidence_pct || 0) >= 90 ? '#ef4444' : '#f97316'}">${(r.mean_confidence_pct || 0).toFixed(1)}%</td>
            <td style="font-family:var(--font-mono)">${r.scores && r.scores['03K'] !== undefined ? r.scores['03K'].toFixed(1) : '0.0'}%</td>
            <td style="font-family:var(--font-mono)">${r.scores && r.scores['03U'] !== undefined ? r.scores['03U'].toFixed(1) : '0.0'}%</td>
            <td style="font-family:var(--font-mono)">${r.scores && r.scores['03R'] !== undefined ? r.scores['03R'].toFixed(1) : '0.0'}%</td>
            <td style="font-family:var(--font-mono);white-space:nowrap">${r.peak_amplitude !== undefined ? r.peak_amplitude.toFixed(3) : '--'} / ${r.rms_amplitude !== undefined ? r.rms_amplitude.toFixed(4) : '--'}</td>
            <td><span class="badge ${r.consensus_badge}">${r.consensus_level}</span></td>
            <td>
                <span class="badge ${rev.badge_class || 'badge-unreviewed'}" title="${revTip}">
                    ${rev.status_text || '待复核'}
                </span>
            </td>
            <td style="white-space:nowrap; text-align:center;">
                <button class="btn btn-outline" style="padding:2px 7px;font-size:11px;" onclick="playbackFromModal('${r.filename}', '${r.timestamp}')" title="载入顶部看板回放并试听原声">
                    ⏮ 回放
                </button>
                <button class="btn btn-outline" style="padding:2px 7px;font-size:11px;" onclick="openSpectrogramFromModal('${r.plot_file || ''}')" title="查看高清时频谱诊断图谱">
                    📈 图谱
                </button>
                <button class="btn btn-outline" style="padding:2px 7px;font-size:11px;" onclick="openReviewFromModal('${r.filename}', '${r.timestamp}')" title="进行人工专家复核">
                    ✍️ 复核
                </button>
            </td>
        </tr>
        `;
    }).join('');
}

// 从弹窗直接回放到顶部监控看板
function playbackFromModal(filename, timestamp) {
    const rec = historyRecords.find(r => r.filename === filename && (!timestamp || r.timestamp === timestamp));
    if (rec) {
        closeKpiDetailModal();
        const idx = currentFilteredRecords.findIndex(r => r.filename === filename && (!timestamp || r.timestamp === timestamp));
        if (idx >= 0) {
            playbackRecord(idx);
        } else {
            isPlaybackMode = true;
            currentPlaybackKey = `${rec.filename}_${rec.timestamp}`;
            const noticeBar = document.getElementById('playbackNotice');
            if (noticeBar) {
                noticeBar.style.display = 'flex';
                document.getElementById('playbackFileName').innerText = rec.filename;
                document.getElementById('playbackTime').innerText = rec.timestamp;
            }
            updateHeroView(rec);
            renderHistoryTable();
            const banner = document.getElementById('decisionBanner');
            if (banner) banner.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
    }
}

// 从弹窗快速查看时频谱图
function openSpectrogramFromModal(plotFile) {
    if (!plotFile) {
        alert('该记录尚未生成诊断图谱');
        return;
    }
    const freshPlotUrl = `/records/${encodeURIComponent(plotFile)}?t=${Date.now()}`;
    const modalImg = document.getElementById('modalImg');
    const modalBox = document.getElementById('imageModal');
    if (modalImg && modalBox) {
        modalImg.src = freshPlotUrl;
        modalBox.style.display = 'flex';
    }
}

// 从弹窗触发人工复核
function openReviewFromModal(filename, timestamp) {
    const rec = historyRecords.find(r => r.filename === filename && (!timestamp || r.timestamp === timestamp));
    if (rec) {
        currentReviewRecord = rec;
        document.getElementById('revModalFilename').innerText = rec.filename;
        document.getElementById('revModalTime').innerText = rec.timestamp || '--';
        document.getElementById('revModalConsensus').innerText = `${rec.consensus_level} (${rec.alarm_votes}/3 票)`;

        const rev = rec.review || {};
        const curStatus = rev.status || 'CONFIRMED_COLLISION';
        selectReviewOption(curStatus);

        const localReviewer = localStorage.getItem('monitor_reviewer_name') || '现场工程师';
        document.getElementById('revModalReviewer').value = rev.reviewer || localReviewer;
        document.getElementById('revModalNotes').value = rev.notes || '';

        const modal = document.getElementById('reviewModal');
        if (modal) modal.style.display = 'flex';
    }
}

// 从模态框一键滚动定位至主页面详细表格
function scrollFromModalToMainTable() {
    closeKpiDetailModal();
    const sec = document.getElementById('historySection');
    if (sec) {
        sec.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
}

function setHistoryFilter(f, btn) {
    currentFilter = f;
    currentConsensusFilter = 'ALL';
    currentHistoryPage = 1;
    const thSel = document.getElementById('thConsensusSelect');
    if (thSel) thSel.value = 'ALL';
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    if (btn) btn.classList.add('active');

    // 联动同步顶部选项卡高亮样式
    document.querySelectorAll('.kpi-card').forEach(c => c.classList.remove('active-tab'));
    const matchingCard = document.getElementById(`kpiCard-${f}`);
    if (matchingCard) matchingCard.classList.add('active-tab');

    // 联动同步主表格横幅提示
    const banner = document.getElementById('kpiTabActiveBanner');
    if (banner) {
        if (f === 'all') {
            banner.style.display = 'none';
        } else if (KPI_TAB_CONFIG[f]) {
            const cfg = KPI_TAB_CONFIG[f];
            banner.style.display = 'flex';
            document.getElementById('tabBannerName').innerText = `${cfg.icon} ${cfg.name}`;
            document.getElementById('tabBannerCount').innerText = `查询中...`;
        } else {
            banner.style.display = 'none';
        }
    }

    fetchHistory(1);
}

function handleSelectConsensusFilter(val) {
    currentConsensusFilter = val || 'ALL';
    currentHistoryPage = 1;
    const thSel = document.getElementById('thConsensusSelect');
    if (thSel) thSel.value = currentConsensusFilter;
    if (currentConsensusFilter !== 'ALL') {
        currentFilter = 'all';
        document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    } else {
        const allBtn = document.querySelector('.filter-btn[onclick*="\'all\'"]');
        if (allBtn) allBtn.classList.add('active');
    }
    fetchHistory(1);
}

let searchDebounceTimer = null;
function handleSearchInput(val) {
    currentSearchQuery = val ? val.trim().toLowerCase() : '';
    currentHistoryPage = 1;
    const clearBtn = document.getElementById('btnSearchClear');
    if (clearBtn) {
        clearBtn.style.display = currentSearchQuery ? 'block' : 'none';
    }
    clearTimeout(searchDebounceTimer);
    searchDebounceTimer = setTimeout(() => {
        fetchHistory(1);
    }, 300);
}

function clearSearchInput() {
    currentSearchQuery = '';
    currentHistoryPage = 1;
    const input = document.getElementById('historySearchInput');
    if (input) input.value = '';
    const clearBtn = document.getElementById('btnSearchClear');
    if (clearBtn) clearBtn.style.display = 'none';
    fetchHistory(1);
}

// 手动选择现场音频投递直接分析
async function handleManualUpload(input) {
    if (!input.files || input.files.length === 0) return;
    const file = input.files[0];
    const formData = new FormData();
    formData.append('file', file);

    try {
        exitPlaybackMode();
        const res = await fetch('/api/upload', {
            method: 'POST',
            body: formData
        });
        const data = await res.json();
        if (data.success) {
            lastSeenFilename = data.result.filename;
            updateHeroView(data.result);
            fetchHistory();
        } else {
            alert('音频上传分析失败: ' + (data.error || '未知错误'));
        }
    } catch (e) {
        alert('网络请求异常: ' + e);
    } finally {
        input.value = '';
    }
}

function openImageModal() {
    const src = document.getElementById('diagnosticPlotImg').src;
    if (src) {
        document.getElementById('modalImg').src = src;
        document.getElementById('imageModal').style.display = 'flex';
    }
}

function closeModal() {
    document.getElementById('imageModal').style.display = 'none';
}

function exportHistoryCSV() {
    if (historyRecords.length === 0) {
        alert('暂无诊断记录可导出！');
        return;
    }
    let csv = '\uFEFF时间戳,文件名,3模型票数,平均置信度%,03K置信%,03U置信%,03R置信%,峰值幅值,RMS有效值,仲裁结论,是否确诊告警,人工复核结论,复核人员,复核时间,专家勘验评语\n';
    historyRecords.forEach(r => {
        const k_score = r.scores && r.scores['03K'] !== undefined ? r.scores['03K'] : 0.0;
        const u_score = r.scores && r.scores['03U'] !== undefined ? r.scores['03U'] : 0.0;
        const r_score = r.scores && r.scores['03R'] !== undefined ? r.scores['03R'] : 0.0;
        const rev = r.review || {};
        const revStatus = rev.status_text || '待复核';
        const reviewer = rev.reviewer || '';
        const revTime = rev.reviewed_at || '';
        const revNotes = (rev.notes || '').replace(/"/g, '""');

        csv += `"${r.timestamp}","${r.filename}",${r.alarm_votes},${(r.mean_confidence_pct || 0).toFixed(2)},${k_score},${u_score},${r_score},${r.peak_amplitude},${r.rms_amplitude},"${r.consensus_level}",${r.is_alarm ? 1 : 0},"${revStatus}","${reviewer}","${revTime}","${revNotes}"\n`;
    });
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `3模型实时监控与人工复核台账_${new Date().toISOString().slice(0, 10)}.csv`;
    link.click();
}

// ---------------------------------------------------------------------------
// 人工复核交互控制器 (Manual Review Modal Controller)
// ---------------------------------------------------------------------------

function openHeroReviewModal() {
    if (!currentHeroData || !currentHeroData.filename) {
        alert('当前看板尚未接收到任何音频数据！');
        return;
    }
    openReviewModalByRecord(currentHeroData);
}

function openReviewModalByIndex(index) {
    const record = currentFilteredRecords[index];
    if (!record) return;
    openReviewModalByRecord(record);
}

function openReviewModalByRecord(record) {
    currentReviewRecord = record;

    document.getElementById('revModalFilename').innerText = record.filename || '--';
    document.getElementById('revModalTime').innerText = record.timestamp || '--';
    document.getElementById('revModalConsensus').innerText = `${record.consensus_level || 'NORMAL_CLEAN_NOISE'} (${record.alarm_votes || 0}/3票)`;

    // 默认恢复之前的复核状态或预设为确诊碰撞/正常
    const currentStatus = (record.review && record.review.status && record.review.status !== 'UNREVIEWED')
        ? record.review.status
        : (record.is_alarm ? 'CONFIRMED_COLLISION' : 'CONFIRMED_NORMAL');

    selectReviewOption(currentStatus);

    // 自动填充审核人姓名（优先取之前填过的，或取 localStorage 记忆）
    const savedReviewer = localStorage.getItem('monitor_reviewer_name') || '现场质检员';
    const existingReviewer = (record.review && record.review.reviewer) ? record.review.reviewer : savedReviewer;
    document.getElementById('revModalReviewer').value = existingReviewer;

    // 填充历史备注
    document.getElementById('revModalNotes').value = (record.review && record.review.notes) ? record.review.notes : '';

    // 显示弹窗
    const modal = document.getElementById('reviewModal');
    if (modal) {
        modal.style.display = 'flex';
    }
}

function selectReviewOption(status) {
    selectedReviewStatus = status;
    const cards = document.querySelectorAll('.review-option-card');
    cards.forEach(c => {
        if (c.getAttribute('onclick') && c.getAttribute('onclick').includes(status)) {
            c.classList.add('selected');
        } else {
            c.classList.remove('selected');
        }
    });
}

function closeReviewModal() {
    const modal = document.getElementById('reviewModal');
    if (modal) {
        modal.style.display = 'none';
    }
    currentReviewRecord = null;
}

async function submitReviewAction() {
    if (!currentReviewRecord) return;

    const reviewer = document.getElementById('revModalReviewer').value.trim() || '现场质检员';
    const notes = document.getElementById('revModalNotes').value.trim();
    const btn = document.getElementById('btnSubmitReview');

    btn.disabled = true;
    btn.innerText = '⏳ 正在提交复核...';

    try {
        const payload = {
            filename: currentReviewRecord.filename,
            timestamp: currentReviewRecord.timestamp,
            status: selectedReviewStatus,
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
            // 保存质检员姓名至本地缓存
            localStorage.setItem('monitor_reviewer_name', reviewer);

            // 更新本地数据集 (兼容 data.review 与 data.record.review)
            const updatedReview = data.review || (data.record ? data.record.review : null) || {
                status: selectedReviewStatus,
                status_text: '',
                badge_class: 'badge-unreviewed',
                reviewer: reviewer,
                reviewed_at: '',
                notes: notes
            };
            for (let r of currentFilteredRecords) {
                if (r.filename === currentReviewRecord.filename && (!currentReviewRecord.timestamp || r.timestamp === currentReviewRecord.timestamp)) {
                    r.review = updatedReview;
                }
            }
            for (let r of historyRecords) {
                if (r.filename === currentReviewRecord.filename && (!currentReviewRecord.timestamp || r.timestamp === currentReviewRecord.timestamp)) {
                    r.review = updatedReview;
                }
            }

            // 如果当前看板就是该音频，同步更新看板徽标
            if (currentHeroData && currentHeroData.filename === currentReviewRecord.filename) {
                currentHeroData.review = updatedReview;
                const heroBadge = document.getElementById('heroReviewBadge');
                if (heroBadge) {
                    heroBadge.className = `badge ${updatedReview.badge_class || 'badge-unreviewed'}`;
                    heroBadge.innerText = updatedReview.status_text || '待复核';
                    heroBadge.title = `复核人: ${updatedReview.reviewer || ''} (${updatedReview.reviewed_at || ''})${updatedReview.notes ? ' - ' + updatedReview.notes : ''}`;
                }
            }

            // 重新刷新表格
            renderHistoryTable();
            closeReviewModal();
        } else {
            alert('提交复核失败: ' + (data.error || '未知错误'));
        }
    } catch (e) {
        alert('提交复核网络异常: ' + e);
    } finally {
        btn.disabled = false;
        btn.innerText = '💾 确认提交复核结论';
    }
}
