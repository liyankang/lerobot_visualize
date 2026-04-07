// ═══════════════════════ LeRobot 数据集编辑器 ═══════════════════════

const S = {
    dataset: null, episodes: [], curEp: null, curEpData: null,
    selEpisodes: new Set(), selFrames: new Set(),
    jointNames: [], jointGroups: {}, activeJoints: new Set(),
    chart: null,            // 合并后的单一图表
    chartMode: 'both',      // 'both' | 'state' | 'action'
    videoPlaying: false, videoFrame: 0, videoTotalFrames: 0, videoTimer: null,
    chartClickState: 0,     // 0=空闲, 1=等待结束帧
    bridgePreview: [],      // 桥接帧预览 (分析模态框打开时高亮)
    uiLocked: false,        // 保存期间锁住页面交互
    saveProgressTimer: null,
};

const COLORS = [
    '#e6194b','#3cb44b','#4363d8','#f58231','#911eb4',
    '#42d4f4','#f032e6','#bfef45','#fabed4','#469990',
    '#dcbeff','#9A6324','#fffac8','#800000','#aaffc3',
    '#808000','#ffd8b1','#000075','#a9a9a9','#ff6961',
    '#77dd77','#6b5b95','#feb236','#d64161','#ff7b25','#00bcd4',
];

// ═══════════════════════ API ═══════════════════════

async function api(u, o={}) {
    try { const r=await fetch(u,o); const d=await r.json(); if(!r.ok) throw new Error(d.error||`HTTP ${r.status}`); return d; }
    catch(e) { toast(e.message,'error'); throw e; }
}
const post = (u,b) => api(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});

// ═══════════════════════ 核心操作 ═══════════════════════

async function loadDataset() {
    if (S.uiLocked) return;
    const path = $('ds-path').value.trim();
    if (!path) return toast('请输入数据集路径','error');
    toast('正在加载...','info');
    const d = await post('/api/load',{path});
    S.dataset=d.summary; S.episodes=d.episodes;
    S.jointNames=d.joint_names; S.jointGroups=d.joint_groups;
    S.selEpisodes.clear(); S.selFrames.clear();
    S.curEp=null; S.curEpData=null;
    S.activeJoints = new Set(d.joint_names.slice(0,7));
    renderSummary(); renderEpList(); renderJointSel(); hideDetail();
    $('save-path').value = path+'_edited';
    toast(`已加载: ${d.summary.total_episodes} ep, ${d.summary.total_frames} 帧`,'success');
}

async function viewEpisode(idx) {
    if (S.uiLocked) return;
    videoPause();
    toast('加载 Episode...','info');
    const d = await api(`/api/episode/${idx}`);
    S.curEp=idx; S.curEpData=d; S.selFrames.clear(); S.chartClickState=0;
    renderEpDetail(d); renderEpList();
    toast(`Ep ${idx}: ${d.frames.length} 帧`,'success');
}

async function doDeleteEp() {
    if (S.uiLocked) return;
    const idx = Array.from(S.selEpisodes);
    if (!idx.length) return toast('请先勾选要删除的 Episode','error');
    if (!confirm(`确定删除 ${idx.length} 个 Episode?\n\n${idx.join(', ')}`)) return;
    if (!confirm('⚠ 二次确认: 索引将重新编号。确认?')) return;
    const d = await post('/api/delete_episodes',{indices:idx});
    S.episodes=d.episodes; S.dataset=d.summary; S.selEpisodes.clear();
    S.curEp=null; S.curEpData=null;
    renderSummary(); renderEpList(); hideDetail();
    toast(`已删除 ${idx.length} ep, 剩余 ${d.remaining_episodes}`,'success');
}

async function doDeleteFrames() {
    if (S.uiLocked) return;
    const fr = Array.from(S.selFrames).sort((a,b)=>a-b);
    if (!fr.length) return toast('请先选择要删除的片段','error');
    if (S.curEp===null) return;

    toast('正在分析平滑性...','info');
    try {
        const activeIdx = Array.from(S.activeJoints)
            .map(j => S.jointNames.indexOf(j)).filter(i => i >= 0);
        const analysis = await post('/api/analyze_deletion', {
            episode_index: S.curEp, frame_indices: fr,
            active_joint_indices: activeIdx.length ? activeIdx : null
        });
        if (analysis.smooth) {
            await executeDelete(fr);
        } else {
            showAnalysisModal(analysis, fr);
        }
    } catch(e) {
        await executeDelete(fr);
    }
}

async function executeDelete(framesToDelete, keepFrames=[]) {
    if (S.uiLocked) return;
    const actual = keepFrames.length > 0
        ? framesToDelete.filter(f => !keepFrames.includes(f))
        : framesToDelete;
    if (!actual.length) { toast('没有需要删除的帧','info'); return; }

    const rng = compressRanges(actual.sort((a,b)=>a-b));
    const rs = rng.map(([s,e])=>s===e?`帧${s}`:`帧${s}-${e}`).join(', ');
    let msg = `从 Ep ${S.curEp} 删除 ${actual.length} 帧?\n\n${rs}`;
    if (keepFrames.length > 0) msg += `\n\n(保留桥接帧: ${keepFrames.join(', ')})`;
    if (!confirm(msg)) return;

    videoPause();
    const d = await post('/api/delete_frames',{episode_index:S.curEp,frame_indices:actual});
    S.episodes=d.episodes; S.dataset=d.summary; S.selFrames.clear(); S.chartClickState=0;
    if (d.episode_data) { S.curEpData=d.episode_data; renderEpDetail(d.episode_data); }
    else { S.curEp=null; S.curEpData=null; hideDetail(); }
    renderSummary(); renderEpList();
    toast(`已删除 ${actual.length} 帧, 剩余 ${d.remaining_frames}`,'success');
}

function showAnalysisModal(analysis, originalFrames) {
    const rec = analysis.recommendation || {};
    const alt = analysis.alternative;

    S.bridgePreview = rec.frames || [];
    refreshChart();

    let jHTML = '';
    for (const j of (analysis.junctions || [])) {
        const joints = (j.problematic_joints || [])
            .map(ji => S.jointNames[ji] || `关节${ji}`).slice(0, 5);
        jHTML += `<div class="aj-item">
            <span class="aj-range">帧 ${j.left_frame} ↔ 帧 ${j.right_frame}</span>
            <span class="aj-info">跨越 ${j.deleted_count} 帧 · 加速度超标 ${j.max_accel_ratio}×</span>
            ${joints.length ? `<span class="aj-joints">受影响: ${joints.join(', ')}</span>` : ''}
        </div>`;
    }

    let rHTML = '';
    if (rec.method === 'bridge' && rec.frames && rec.frames.length) {
        rHTML += `<div class="arec">
            <div class="arec-title">方案一: 保留桥接帧 <span class="arec-tag-pri">推荐</span></div>
            <p>通过递归二分法找到的最优桥接帧，保留这些真实帧使轨迹平滑过渡</p>
            <div class="arec-frames">${rec.frames.map(f=>`<span class="af-tag af-bridge">帧 ${f}</span>`).join('')}</div>
            <button class="bp" onclick="applyRecommendation(${JSON.stringify(rec.frames)},${JSON.stringify(originalFrames)})">采用此方案</button>
        </div>`;
        if (alt && alt.frames && alt.frames.length) {
            rHTML += `<div class="arec">
                <div class="arec-title">方案二: 滤波插值匹配</div>
                <p>通过滤波生成平滑参考轨迹，匹配最接近理想值的真实帧</p>
                <div class="arec-frames">${alt.frames.map(f=>`<span class="af-tag af-filter">帧 ${f}</span>`).join('')}</div>
                <button class="bp" onclick="applyRecommendation(${JSON.stringify(alt.frames)},${JSON.stringify(originalFrames)})">采用此方案</button>
            </div>`;
        }
    } else if (rec.method === 'filter' && rec.frames && rec.frames.length) {
        rHTML += `<div class="arec">
            <div class="arec-title">方案: 滤波插值匹配</div>
            <p>未找到理想桥接帧，通过滤波生成平滑参考轨迹，匹配最接近理想值的真实帧</p>
            <div class="arec-frames">${rec.frames.map(f=>`<span class="af-tag af-filter">帧 ${f}</span>`).join('')}</div>
            <button class="bp" onclick="applyRecommendation(${JSON.stringify(rec.frames)},${JSON.stringify(originalFrames)})">采用此方案</button>
        </div>`;
    } else {
        rHTML += `<div class="arec arec-warn"><p>未找到合适的过渡帧，建议减小删除范围或直接强制删除</p></div>`;
    }

    const html = `<div class="modal-overlay" id="analysis-modal" onclick="if(event.target===this)closeAnalysisModal()">
        <div class="modal-card">
            <div class="modal-hdr"><h3>平滑性分析结果</h3><button class="modal-close" onclick="closeAnalysisModal()">×</button></div>
            <div class="modal-body">
                <div class="aj-section"><h4>检测到不连续拼接点</h4>${jHTML}</div>
                <div class="arec-section"><h4>建议方案</h4>${rHTML}</div>
            </div>
            <div class="modal-footer">
                <button class="bd" onclick="forceDeleteAll(${JSON.stringify(originalFrames)})">强制全部删除</button>
                <button class="bg" onclick="closeAnalysisModal()">取消</button>
            </div>
        </div>
    </div>`;
    document.body.insertAdjacentHTML('beforeend', html);
}

function closeAnalysisModal() {
    S.bridgePreview = []; refreshChart();
    const m = document.getElementById('analysis-modal');
    if (m) m.remove();
}

async function applyRecommendation(keepFrames, allFrames) {
    closeAnalysisModal();
    await executeDelete(allFrames, keepFrames);
}

async function forceDeleteAll(frames) {
    closeAnalysisModal();
    await executeDelete(frames);
}

async function doSave() {
    if (S.uiLocked) return;
    const p = $('save-path').value.trim();
    if (!p) return toast('请输入保存路径','error');
    if (!confirm(`保存到:\n${p}\n\n已有路径将被覆盖!`)) return;
    lockUI('正在保存数据集', '正在写入 Parquet、导出视频并重算统计信息，请稍候...');
    startSaveProgressPolling();
    toast('正在保存 (含统计重算)...','info');
    try {
        const d = await post('/api/save',{output_path:p});
        updateLoadingProgress({
            title: '保存完成',
            detail: `数据集已保存到: ${d.path}`,
            percent: 100,
        });
        toast(`保存成功: ${d.path}`,'success');
    } finally {
        stopSaveProgressPolling();
        unlockUI();
    }
}

// ═══════════════════════ 视频同步 ═══════════════════════

const getVids = () => Array.from(document.querySelectorAll('#vw video'));
function videoToggle() { S.videoPlaying ? videoPause() : videoPlay(); }

function videoPlay() {
    if (S.videoPlaying || S.videoTotalFrames<=0) return;
    S.videoPlaying=true; $('btn-vp').textContent='⏸';
    const iv = 1000/(S.dataset?S.dataset.fps:30);
    S.videoTimer = setInterval(()=>{
        if (S.videoFrame>=S.videoTotalFrames-1) { videoPause(); return; }
        S.videoFrame++; videoSync();
    }, iv);
}

function videoPause() {
    S.videoPlaying=false;
    if (S.videoTimer) { clearInterval(S.videoTimer); S.videoTimer=null; }
    const b=$('btn-vp'); if(b) b.textContent='▶';
}

function videoStep(d) {
    videoPause();
    if (d===-Infinity) S.videoFrame=0;
    else if (d===Infinity) S.videoFrame=Math.max(0,S.videoTotalFrames-1);
    else S.videoFrame=Math.max(0,Math.min(S.videoTotalFrames-1,S.videoFrame+d));
    videoSync();
}

function videoSync() {
    const fps=S.dataset?S.dataset.fps:30;
    let t;
    if (S.curEpData && S.curEpData.frames && S.videoFrame < S.curEpData.frames.length) {
        t = S.curEpData.frames[S.videoFrame].timestamp;
    } else {
        t = S.videoFrame / fps;
    }
    for (const v of getVids()) v.currentTime=t;
    const sl=$('vs'); if(sl) sl.value=S.videoFrame;
    const lb=$('vfl'); if(lb) lb.textContent=`${S.videoFrame} / ${Math.max(0,S.videoTotalFrames-1)}`;
    refreshChart();
    renderFrameValues(S.videoFrame);
}

function onSliderInput(v) { videoPause(); S.videoFrame=parseInt(v)||0; videoSync(); }

// ═══════════════════════ 渲染 ═══════════════════════

function renderSummary() {
    const s=S.dataset; if(!s) return;
    $('si').innerHTML =
        `<span>FPS:<b>${s.fps}</b></span>`+
        `<span>机器人:<b>${s.robot_type}</b></span>`+
        `<span>Episodes:<b>${s.total_episodes}</b></span>`+
        `<span>帧:<b>${s.total_frames}</b></span>`+
        `<span>摄像头:${s.cameras.length?s.cameras.join(','):'无'}</span>`+
        (s.modified?' <span class="bw">已修改</span>':'');
}

function renderEpList() {
    const el=$('ep-list');
    if (!S.episodes.length) { el.innerHTML='<div class="mt">暂无</div>'; return; }
    let h='';
    for (const ep of S.episodes) {
        const i=ep.episode_index, ck=S.selEpisodes.has(i)?'checked':'', act=S.curEp===i?'act':'';
        h+=`<div class="epi ${act}"><input type="checkbox" class="eck" ${ck} onclick="event.stopPropagation();toggleEpSel(${i},this.checked)"><span style="flex:1" onclick="viewEpisode(${i})">Ep ${i} <small>(${ep.length}帧)</small></span></div>`;
    }
    el.innerHTML=h; syncSelAll();
}

function renderJointSel() {
    const el=$('jb'); let h='';
    for (const [grp,joints] of Object.entries(S.jointGroups)) {
        const ac=joints.filter(j=>S.activeJoints.has(j)).length, all=ac===joints.length;
        h+='<div class="jg">';
        h+=`<label class="jgl"><input type="checkbox" ${all?'checked':''} data-grp="${grp}" onchange="toggleJtGrp('${grp}',this.checked)"> ${grp}</label>`;
        for (const nm of joints) {
            const gi=S.jointNames.indexOf(nm), ck=S.activeJoints.has(nm)?'checked':'', c=COLORS[gi%COLORS.length];
            h+=`<label class="jl" style="border-left:2px solid ${c};padding-left:3px;"><input type="checkbox" value="${nm}" ${ck} onchange="toggleJt('${nm}',this.checked)"> ${nm}</label>`;
        }
        h+='</div>';
    }
    el.innerHTML=h;
    for (const [grp,joints] of Object.entries(S.jointGroups)) {
        const ac=joints.filter(j=>S.activeJoints.has(j)).length;
        if (ac>0 && ac<joints.length) { const cb=el.querySelector(`input[data-grp="${grp}"]`); if(cb) cb.indeterminate=true; }
    }
}

function renderEpDetail(data) {
    $('no-sel').style.display='none';
    const det=$('ep-det'); det.classList.add('show');
    $('ep-t').textContent=`Episode ${data.episode_index} — ${data.frames.length} 帧`;
    S.videoFrame=0; S.videoTotalFrames=data.frames.length; S.videoPlaying=false;
    const sl=$('vs'); sl.max=Math.max(0,data.frames.length-1); sl.value=0;
    $('vfl').textContent=`0 / ${Math.max(0,data.frames.length-1)}`;
    $('btn-vp').textContent='▶';
    renderVideos(data.videos);
    updateChart(data.frames);
    renderFrameInfo(data.frames);
    renderFrameValues(0);
}

function hideDetail() {
    $('no-sel').style.display='flex';
    $('ep-det').classList.remove('show');
    videoPause();
}

function renderVideos(videos) {
    const el=$('vw');
    if (!videos||!Object.keys(videos).length) { el.innerHTML='<div class="mt">无视频</div>'; return; }
    let h='';
    for (const [cam,url] of Object.entries(videos))
        h+=`<div class="vi"><h4>${cam}</h4><video preload="auto" muted width="240"><source src="${url}" type="video/mp4"></video></div>`;
    el.innerHTML=h;
}

function renderFrameInfo(frames) {
    $('fr-total').textContent=frames.length;
    $('fr-sel-cnt').textContent=S.selFrames.size;
    $('fr-from').value=''; $('fr-to').value='';
    $('fr-from').max=frames.length-1; $('fr-to').max=frames.length-1;
    renderSelRanges();
}

function renderSelRanges() {
    const el=$('sr');
    if (!S.selFrames.size) {
        el.innerHTML='<span class="ht">在图表上点两次选择片段，或手动输入范围</span>';
        $('fr-sel-cnt').textContent='0'; refreshChart(); return;
    }
    const sorted=Array.from(S.selFrames).sort((a,b)=>a-b), rng=compressRanges(sorted);
    let h='';
    for (const [s,e] of rng) {
        const l=s===e?`帧${s}`:`帧${s}-${e}(${e-s+1})`;
        h+=`<span class="rt">${l}<button onclick="rmRange(${s},${e})">×</button></span>`;
    }
    el.innerHTML=h; $('fr-sel-cnt').textContent=S.selFrames.size; refreshChart();
}

// ═══════════════════════ 当前帧关节数据 ═══════════════════════

function renderFrameValues(fi) {
    const lbl=$('cfl'), grid=$('fdg');
    if (!lbl||!grid) return;
    if (!S.curEpData||fi<0||fi>=S.curEpData.frames.length) {
        lbl.textContent='—'; grid.innerHTML='<div class="mt">无数据</div>'; return;
    }
    const f=S.curEpData.frames[fi], st=f.state||[], ac=f.action||[];
    const fps=S.dataset?S.dataset.fps:30;
    lbl.textContent=`帧 ${fi} | ${(fi/fps).toFixed(3)}s`;
    let h='';
    for (const [grp,joints] of Object.entries(S.jointGroups)) {
        h+='<div>';
        h+=`<div class="fgt">${grp}</div><table class="ft"><tr><th>关节</th><th>State</th><th>Action</th></tr>`;
        for (const nm of joints) {
            const ji=S.jointNames.indexOf(nm), c=COLORS[ji%COLORS.length];
            const sv=(ji>=0&&ji<st.length)?st[ji].toFixed(4):'—';
            const av=(ji>=0&&ji<ac.length)?ac[ji].toFixed(4):'—';
            h+=`<tr><td class="fjn" style="color:${c}">${nm}</td><td>${sv}</td><td>${av}</td></tr>`;
        }
        h+='</table></div>';
    }
    grid.innerHTML=h;
}

// ═══════════════════════ 合并图表 ═══════════════════════

const chartOverlay = {
    id:'chartOverlay',
    beforeDraw(chart) {
        const {ctx,chartArea,scales:{x}}=chart; if(!chartArea) return;
        const {top,bottom,left,right}=chartArea; ctx.save();
        // 选中片段高亮
        if (S.selFrames.size>0) {
            const rng=compressRanges(Array.from(S.selFrames).sort((a,b)=>a-b));
            ctx.fillStyle='rgba(231,76,60,0.12)'; ctx.strokeStyle='rgba(231,76,60,0.35)'; ctx.lineWidth=1;
            for (const [s,e] of rng) {
                const p1=x.getPixelForValue(s-0.4), p2=x.getPixelForValue(e+0.4);
                ctx.fillRect(p1,top,p2-p1,bottom-top); ctx.strokeRect(p1,top,p2-p1,bottom-top);
            }
        }
        // 桥接帧预览 (绿色虚线)
        if (S.bridgePreview && S.bridgePreview.length>0) {
            ctx.strokeStyle='rgba(39,174,96,0.85)'; ctx.lineWidth=2.5; ctx.setLineDash([4,3]);
            for (const f of S.bridgePreview) {
                const px=x.getPixelForValue(f);
                if (px>=left&&px<=right) {
                    ctx.beginPath(); ctx.moveTo(px,top); ctx.lineTo(px,bottom); ctx.stroke();
                }
            }
            ctx.setLineDash([]);
        }
        // 播放位置指示线
        if (S.videoTotalFrames>0&&S.curEpData) {
            const px=x.getPixelForValue(S.videoFrame);
            if (px>=left&&px<=right) {
                ctx.strokeStyle='#e74c3c'; ctx.lineWidth=2; ctx.setLineDash([5,3]);
                ctx.beginPath(); ctx.moveTo(px,top); ctx.lineTo(px,bottom); ctx.stroke(); ctx.setLineDash([]);
            }
        }
        ctx.restore();
    }
};
Chart.register(chartOverlay);

function updateChart(frames) {
    if (!frames||!frames.length) return;
    const labels=frames.map(f=>f.frame_index);
    const joints=Array.from(S.activeJoints);
    buildChart(labels,frames,joints);
}

function buildChart(labels,frames,joints) {
    if (S.chart) { S.chart.destroy(); S.chart=null; }
    const ds=[], mode=S.chartMode, showS=(mode==='both'||mode==='state'), showA=(mode==='both'||mode==='action');

    for (const jn of joints) {
        const ji=S.jointNames.indexOf(jn); if(ji<0) continue;
        const c=COLORS[ji%COLORS.length];
        if (showS) ds.push({
            label: mode==='both'?`${jn} (S)`:jn,
            data: frames.map(f=>{const a=f.state;return(a&&ji<a.length)?a[ji]:null;}),
            borderColor:c, borderWidth:1.5, borderDash:[], pointRadius:0, pointHitRadius:6, tension:0, fill:false,
        });
        if (showA) ds.push({
            label: mode==='both'?`${jn} (A)`:jn,
            data: frames.map(f=>{const a=f.action;return(a&&ji<a.length)?a[ji]:null;}),
            borderColor:c, borderWidth:1, borderDash:[5,3], pointRadius:0, pointHitRadius:6, tension:0, fill:false,
        });
    }

    S.chart = new Chart($('main-chart'), {
        type:'line', data:{labels,datasets:ds},
        options:{
            responsive:true, maintainAspectRatio:false, animation:false,
            interaction:{mode:'index',intersect:false},
            plugins:{
                title:{display:false},
                legend:{display:true,position:'bottom',labels:{font:{size:9},boxWidth:10,padding:6}},
                zoom:{pan:{enabled:true,mode:'x'},zoom:{wheel:{enabled:true},pinch:{enabled:true},mode:'x'}},
            },
            scales:{
                x:{title:{display:true,text:'帧',font:{size:10}},ticks:{maxTicksLimit:20,font:{size:9}}},
                y:{title:{display:true,text:'值',font:{size:10}},ticks:{font:{size:9}}},
            },
            onClick:(_ev,_el,chart)=>{
                const rect=chart.canvas.getBoundingClientRect();
                const mx=_ev.native.clientX-rect.left, fi=Math.round(chart.scales.x.getValueForPixel(mx));
                if (fi<0||fi>=S.videoTotalFrames) return;
                // 同步视频
                videoPause(); S.videoFrame=fi; videoSync();
                // 选段
                if (S.chartClickState===0) {
                    $('fr-from').value=fi; $('fr-to').value=''; S.chartClickState=1;
                    toast(`起始帧: ${fi} — 再点一次选结束帧`,'info');
                } else {
                    const from=parseInt($('fr-from').value), lo=Math.min(from,fi), hi=Math.max(from,fi);
                    $('fr-from').value=lo; $('fr-to').value=hi;
                    for(let i=lo;i<=hi;i++) S.selFrames.add(i);
                    S.chartClickState=0; renderSelRanges();
                    toast(`已添加: 帧${lo}-${hi} (${hi-lo+1}帧)`,'success');
                }
            },
        },
    });
}

function setChartMode(m) {
    S.chartMode=m;
    document.querySelectorAll('.mode-b').forEach(b=>b.classList.toggle('on',b.dataset.mode===m));
    if (S.curEpData) updateChart(S.curEpData.frames);
}

function resetZoom() { if(S.chart) S.chart.resetZoom(); }
function refreshChart() { if(S.chart) S.chart.update('none'); }

// ═══════════════════════ 选择逻辑 ═══════════════════════

function toggleEpSel(i,ck) { ck?S.selEpisodes.add(i):S.selEpisodes.delete(i); syncSelAll(); }
function toggleSelAll(ck) { ck?S.episodes.forEach(e=>S.selEpisodes.add(e.episode_index)):S.selEpisodes.clear(); renderEpList(); }
function syncSelAll() { const cb=$('sel-all'),t=S.episodes.length,s=S.selEpisodes.size; cb.checked=s===t&&t>0; cb.indeterminate=s>0&&s<t; }

function toggleJt(nm,ck) { ck?S.activeJoints.add(nm):S.activeJoints.delete(nm); renderJointSel(); if(S.curEpData)updateChart(S.curEpData.frames); }
function toggleJtGrp(g,ck) { for(const j of(S.jointGroups[g]||[])){ck?S.activeJoints.add(j):S.activeJoints.delete(j);} renderJointSel(); if(S.curEpData)updateChart(S.curEpData.frames); }

function addFrameRange() {
    if (S.uiLocked) return;
    const f=parseInt($('fr-from').value), t=parseInt($('fr-to').value);
    if (isNaN(f)||isNaN(t)||f<0||t<f) return toast('请输入有效范围','error');
    for(let i=f;i<=t;i++) S.selFrames.add(i); S.chartClickState=0; renderSelRanges();
    toast(`已添加: 帧${f}-${t} (${t-f+1}帧)`,'success');
}
function rmRange(s,e) { if (S.uiLocked) return; for(let i=s;i<=e;i++) S.selFrames.delete(i); renderSelRanges(); }
function clearFrameSel() { if (S.uiLocked) return; S.selFrames.clear(); S.chartClickState=0; $('fr-from').value=''; $('fr-to').value=''; renderSelRanges(); }

// ═══════════════════════ 工具 ═══════════════════════

function $(id) { return document.getElementById(id); }
function lockUI(title='处理中', detail='请稍候...') {
    S.uiLocked = true;
    document.body.classList.add('ui-locked');
    const overlay = $('loading-overlay');
    if (!overlay) return;
    $('loading-title').textContent = title;
    $('loading-detail').textContent = detail;
    $('loading-meta').textContent = '';
    overlay.classList.add('show');
    overlay.setAttribute('aria-hidden', 'false');
}
function unlockUI() {
    S.uiLocked = false;
    document.body.classList.remove('ui-locked');
    const overlay = $('loading-overlay');
    if (!overlay) return;
    overlay.classList.remove('show');
    overlay.setAttribute('aria-hidden', 'true');
}
function updateLoadingProgress(progress) {
    if (!progress) return;
    const title = progress.title || '正在处理中';
    const detail = progress.detail || '请稍候...';
    $('loading-title').textContent = title;
    $('loading-detail').textContent = detail;
    if (typeof progress.percent === 'number') {
        $('loading-meta').textContent = `进度 ${progress.percent}%`;
    } else if (progress.current > 0 || progress.total > 0) {
        $('loading-meta').textContent = `进度 ${progress.current || 0}/${progress.total || 0}`;
    } else {
        $('loading-meta').textContent = '';
    }
}
async function pollSaveProgressOnce() {
    try {
        const r = await fetch('/api/save_progress');
        if (!r.ok) return;
        const d = await r.json();
        updateLoadingProgress(d);
    } catch(_e) {}
}
function startSaveProgressPolling() {
    stopSaveProgressPolling();
    pollSaveProgressOnce();
    S.saveProgressTimer = setInterval(pollSaveProgressOnce, 500);
}
function stopSaveProgressPolling() {
    if (S.saveProgressTimer) {
        clearInterval(S.saveProgressTimer);
        S.saveProgressTimer = null;
    }
}
function compressRanges(sorted) {
    if(!sorted.length)return[]; const r=[]; let rs=sorted[0],re=sorted[0];
    for(let i=1;i<sorted.length;i++){sorted[i]===re+1?re=sorted[i]:(r.push([rs,re]),rs=re=sorted[i]);}
    r.push([rs,re]); return r;
}
function toast(m,t='info') { const b=$('tb'),e=document.createElement('div'); e.className=`tt t${t[0]}`; e.textContent=m; b.appendChild(e); setTimeout(()=>e.remove(),3500); }

// ═══════════════════════ 初始化 ═══════════════════════

document.addEventListener('DOMContentLoaded',()=>{
    $('btn-load').addEventListener('click',loadDataset);
    $('btn-save').addEventListener('click',doSave);
    $('btn-del-ep').addEventListener('click',doDeleteEp);
    $('btn-del-fr').addEventListener('click',doDeleteFrames);
    $('btn-add-rng').addEventListener('click',addFrameRange);
    $('btn-clr-fr').addEventListener('click',clearFrameSel);
    $('sel-all').addEventListener('change',e=>toggleSelAll(e.target.checked));
    $('ds-path').addEventListener('keydown',e=>{if(e.key==='Enter')loadDataset();});
    $('fr-to').addEventListener('keydown',e=>{if(e.key==='Enter')addFrameRange();});
    document.addEventListener('keydown',e=>{
        if (S.uiLocked) {
            e.preventDefault();
            return;
        }
        if(e.target.tagName==='INPUT'||e.target.tagName==='TEXTAREA')return;
        if(!S.curEpData)return;
        switch(e.code){
            case'Space':e.preventDefault();videoToggle();break;
            case'ArrowLeft':e.preventDefault();videoStep(e.shiftKey?-10:-1);break;
            case'ArrowRight':e.preventDefault();videoStep(e.shiftKey?10:1);break;
            case'Home':e.preventDefault();videoStep(-Infinity);break;
            case'End':e.preventDefault();videoStep(Infinity);break;
        }
    });
});
