'use strict';
const $ = id => document.getElementById(id);
const state = {token: '', jobs: [], archives: [], mode: 'public', editId: null, folder: '', parent: '', logId: null, logKind: 'streams', polling: false};
const ACTIVE = new Set(['starting', 'streaming', 'retrying', 'stopping']);
const LABELS = {scheduled:'Chờ lịch',queued:'Chờ máy phát',starting:'Đang kết nối',streaming:'Đang phát',retrying:'Kết nối lại',stopping:'Đang dừng',stopped:'Đã dừng',completed:'Hoàn tất',failed:'Lỗi',interrupted:'Bị ngắt',missed:'Lỡ lịch'};
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const date = timestamp => timestamp ? new Intl.DateTimeFormat('vi-VN',{timeZone:'Asia/Ho_Chi_Minh',day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(timestamp*1000)) : '—';
const duration = value => { const seconds = Math.max(0,Math.floor(value || 0)); return [Math.floor(seconds/3600),Math.floor(seconds/60)%60,seconds%60].map(n=>String(n).padStart(2,'0')).join(':'); };
const size = bytes => bytes > 1024**3 ? `${(bytes/1024**3).toFixed(2)} GB` : `${(bytes/1024**2).toFixed(1)} MB`;
const localInput = timestamp => { const d = new Date(timestamp*1000+7*3600000); return d.toISOString().slice(0,19); };

function toast(message, type='success') {
  const item = document.createElement('div'); item.className=`toast ${type}`; item.textContent=message;
  $('toast-region').append(item);
  while($('toast-region').children.length>3) $('toast-region').firstElementChild.remove();
  setTimeout(()=>item.remove(),7000);
}
async function api(path, options={}) {
  const response = await fetch(path,{...options,headers:{'X-Local-Token':state.token,...(options.body?{'Content-Type':'application/json'}:{}),...options.headers}});
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || 'Không thực hiện được thao tác.');
  return data;
}
function setMode(mode) {
  state.mode=mode;
  for (const name of ['public','continuous']) { const button=$(`mode-${name}`); button.classList.toggle('selected',mode===name); button.setAttribute('aria-selected',String(mode===name)); }
  $('duration').disabled=mode==='continuous'; $('duration').required=mode==='public';
  $('duration-help').textContent=mode==='continuous'?'Phát lặp liên tục cho đến khi bạn bấm Dừng.':'Video tự lặp đến hết thời lượng đã đặt.';
}
function resetForm() {
  state.editId=null; $('stream-form').reset(); $('stream-key').required=true;
  $('youtube-video-id').required=false;
  $('stream-key').placeholder='Dán stream key từ nền tảng livestream';
  $('start-at').value=localInput(Date.now()/1000+300); setMode('public'); encoderChanged();
  $('composer-title').textContent='Thiết lập livestream'; $('save-stream').textContent='＋  Thêm lịch phát';
  $('cancel-edit').hidden=true; $('form-note').textContent='Lịch và stream key được lưu trên máy của bạn.';
  $('source-info').textContent='Chọn file trong thư mục có sẵn. Video được đọc trực tiếp trên máy này.';
}
function encoderChanged() {
  const copy=$('encoder').value==='copy'; $('bitrate').disabled=copy; $('resolution').disabled=copy;
  $('bitrate-help').textContent=copy?'Copy giữ bitrate và độ phân giải của video gốc.':'Chọn theo tốc độ upload và cấu hình máy.';
}
function payload() {
  const parts=$('duration').value.split(':').map(Number);
  if (state.mode==='public' && (parts.length!==3 || !parts.every(Number.isFinite) || parts[1]>59 || parts[2]>59)) throw new Error('Nhập thời lượng theo HH:MM:SS.');
  return {name:$('stream-name').value.trim(),stream_key:$('stream-key').value.trim(),source_path:$('source-path').value.trim(),
    server_url:$('server-url').value.trim(),mode:state.mode,start_at:$('start-at').value+'+07:00',
    duration:state.mode==='public'?parts[0]*3600+parts[1]*60+parts[2]:14400,
    bitrate:Number($('bitrate').value),encoder:$('encoder').value,resolution:$('resolution').value,auto_retry:$('auto-retry').checked,
    youtube_video_id:$('youtube-video-id').value.trim(),trim_after_live:$('trim-after-live').checked,publish_after_trim:$('publish-after-trim').checked};
}
async function save(liveNow=false) {
  if (!$('stream-form').reportValidity()) return false;
  const buttons=[$('save-stream'),$('live-now')]; buttons.forEach(button=>button.disabled=true);
  try {
    const data=payload();
    if (liveNow) data.start_at=localInput(Date.now()/1000)+'+07:00';
    const endpoint=(state.editId?`/api/streams/${state.editId}`:'/api/streams')+(liveNow?'?start_now=true':'');
    const result=await api(endpoint,{method:state.editId?'PUT':'POST',body:JSON.stringify(data)});
    if(result.error) toast('Lịch đã lưu; chưa thể chạy: '+result.error,'error');
    else if (liveNow) toast('Đã lưu và khởi động máy phát. Xem trạng thái trong danh sách.');
    else toast(state.editId?'Đã cập nhật lịch phát.':'Đã thêm lịch phát trên máy này.');
    resetForm(); await refresh(); return true;
  } catch(error) { toast(error.message,'error'); await refresh(); return false; }
  finally { buttons.forEach(button=>button.disabled=false); }
}
function iconButton(action,id,label,symbol,style='',disabled=false) {
  return `<button type="button" class="icon-button ${style}" data-action="${action}" data-id="${escapeHTML(id)}" title="${label}" aria-label="${label}" ${disabled?'disabled':''}>${symbol}</button>`;
}
function render() {
  const search=$('search').value.toLowerCase().trim(), status=$('filter-status').value, mode=$('filter-mode').value;
  const jobs=state.jobs.filter(job=>(!status||job.status===status)&&(!mode||job.mode===mode)&&(!search||`${job.name} ${job.id} ${job.source_path}`.toLowerCase().includes(search)));
  $('total-count').textContent=state.jobs.length; $('schedule-count').textContent=state.jobs.length;
  $('live-count').textContent=state.jobs.filter(job=>job.status==='streaming').length;
  $('scheduled-count').textContent=state.jobs.filter(job=>['scheduled','queued'].includes(job.status)).length;
  $('done-count').textContent=state.jobs.filter(job=>job.status==='completed').length;
  $('results-count').textContent=`Hiển thị ${jobs.length} / ${state.jobs.length} lịch phát`;
  $('empty-state').hidden=jobs.length>0;
  $('empty-state').querySelector('h3').textContent=state.jobs.length?'Không tìm thấy lịch phù hợp':'Sẵn sàng cho buổi live đầu tiên';
  $('clear-done').disabled=!state.jobs.some(job=>['completed','missed'].includes(job.status));
  const focus=document.activeElement.closest('tbody')?{id:document.activeElement.dataset.id,action:document.activeElement.dataset.action}:null;
  $('streams-body').innerHTML=jobs.map(job=>{
    const metrics=job.metrics||{}, active=ACTIVE.has(job.status), elapsed=metrics.elapsed||0;
    const percent=job.mode==='continuous'?0:Math.min(100,elapsed/job.duration*100);
    const budget=job.encoder==='copy'?'Gốc · Copy':`${job.bitrate.toLocaleString('vi-VN')} kbps`;
    const actions=(active?iconButton('stop',job.id,'Dừng phát','■','stop',job.status==='stopping'):iconButton('start',job.id,'Chạy ngay','▶','start'))+
      iconButton('logs',job.id,'Xem log','≡')+iconButton('edit',job.id,'Sửa lịch','✎','',active)+iconButton('delete',job.id,'Xóa lịch','⌫','delete',active);
    return `<tr><td>#${escapeHTML(job.id)}</td><td><span class="stream-title">${escapeHTML(job.name)}</span><span class="stream-file" title="${escapeHTML(job.source_path)}">▱ ${escapeHTML(job.media.name)}</span><span class="stream-meta">${job.media.width} × ${job.media.height} · ${duration(job.media.duration)} · ${size(job.media.size)}</span></td><td><span class="key-chip">${escapeHTML(job.key_hint)}</span><div class="live-date">${date(job.start_at)}</div><span class="mode-pill ${job.mode==='continuous'?'continuous':''}">${job.mode==='continuous'?'24/7 · LIÊN TỤC':duration(job.duration)}</span></td><td><div class="metrics-line">BITRATE &nbsp; <strong>${budget}</strong></div><div class="metrics-line">${duration(elapsed)} ${job.mode==='public'?'/ '+duration(job.duration):'đã phát'}</div><div class="progress-track"><span style="width:${percent}%"></span></div><div class="metrics-line">${Number(metrics.fps||0).toFixed(0)} fps · ${Number(metrics.speed||0).toFixed(2)}x${metrics.bytes_sent?' · '+size(metrics.bytes_sent):''}</div></td><td><span class="status-chip ${escapeHTML(job.status)}">${LABELS[job.status]||escapeHTML(job.status)}</span>${job.last_error?`<span class="error-note">${escapeHTML(job.last_error)}</span>`:''}</td><td><div class="actions">${actions}</div></td></tr>`;
  }).join('');
  if (focus) $('streams-body').querySelector(`[data-id="${focus.id}"][data-action="${focus.action}"]`)?.focus({preventScroll:true});
}
async function refresh() {
  // A restarted server has a new local token. Read the public health endpoint
  // before protected APIs so the old page can reload and obtain that token.
  if(state.restarting) {
    const health=await api('/api/health');
    if(health.commit && health.commit!==state.installedCommit) {location.reload();return;}
  }
  const [jobs,archives,update]=await Promise.all([api('/api/streams'),api('/api/archives'),api('/api/update')]);
  state.jobs=jobs; render(); renderArchives(archives); renderUpdate(update);
}
function confirmAction(title,message,action,label='Xác nhận') {
  $('confirm-title').textContent=title; $('confirm-message').textContent=message; $('confirm-action').textContent=label;
  $('confirm-action').onclick=async()=>{ $('confirm-action').disabled=true; try { const result=await action(); if(result!==false) $('confirm-dialog').close(); await refresh(); } catch(error) {toast(error.message,'error');} finally {$('confirm-action').disabled=false;} };
  $('confirm-dialog').showModal();
}
function editJob(job) {
  state.editId=job.id; setMode(job.mode); $('stream-name').value=job.name; $('source-path').value=job.source_path;
  $('stream-key').value=''; $('stream-key').required=false; $('stream-key').placeholder=`Để trống để giữ key đã lưu (${job.key_hint})`;
  $('start-at').value=localInput(job.start_at); $('duration').value=duration(job.duration); $('bitrate').value=job.bitrate;
  $('server-url').value=job.server_url; $('encoder').value=job.encoder; $('resolution').value=job.resolution;
  $('auto-retry').checked=job.auto_retry; encoderChanged();
  $('youtube-video-id').value=job.youtube_video_id||''; $('trim-after-live').checked=!!job.trim_after_live;
  $('publish-after-trim').checked=job.publish_after_trim!==false;
  $('youtube-video-id').required=!!job.trim_after_live;
  if(job.trim_after_live) document.querySelector('.archive-options').open=true;
  $('composer-title').textContent='Sửa lịch livestream'; $('save-stream').textContent='Lưu thay đổi'; $('cancel-edit').hidden=false;
  $('form-note').textContent=`Đang sửa lịch #${job.id}. Lưu thay đổi sẽ đặt lại lịch chờ phát.`;
  showMedia(job.media); document.querySelector('.composer').scrollIntoView({behavior:'smooth',block:'start'});
}
function showMedia(media) { $('source-info').textContent=`${media.width} × ${media.height} · ${duration(media.duration)} · ${size(media.size)} · ${media.video_codec.toUpperCase()}${media.has_audio?' + '+media.audio_codec.toUpperCase():' · Video không có audio; máy sẽ thêm âm thanh im lặng.'}`; }
async function probeSource() {
  const path=$('source-path').value.trim(); if(!path) return;
  $('source-info').textContent='Đang kiểm tra video…';
  try { const media=await api('/api/probe?path='+encodeURIComponent(path)); if($('source-path').value.trim()===path) showMedia(media); }
  catch(error) { if($('source-path').value.trim()===path) $('source-info').textContent=error.message; }
}
async function openFolder(path='') {
  $('folder-content').textContent='Đang đọc thư mục…';
  try {
    const folder=await api('/api/files?path='+encodeURIComponent(path)); state.folder=folder.path; state.parent=folder.parent;
    $('folder-path').value=folder.path; $('folder-up').disabled=folder.parent===null;
    const items=[...folder.directories.map(item=>({...item,type:'folder'})),...folder.videos.map(item=>({...item,type:'video'}))];
    $('folder-content').replaceChildren();
    for(const item of items) {
      const button=document.createElement('button'); button.className='file-item'; button.type='button';
      button.innerHTML=`<span class="file-icon">${item.type==='folder'?'▱':'▻'}</span><span><strong>${escapeHTML(item.name)}</strong><small>${item.type==='folder'?'Thư mục':size(item.size)+' · File trên máy'}</small></span><span class="file-arrow">${item.type==='folder'?'›':'＋'}</span>`;
      button.onclick=()=>{if(item.type==='folder') openFolder(item.path); else {$('source-path').value=item.path; $('file-dialog').close(); probeSource();}};
      $('folder-content').append(button);
    }
    if(!items.length) $('folder-content').textContent='Thư mục này chưa có video hỗ trợ. Chọn thư mục khác hoặc thêm file MP4, MKV, MOV, AVI, WEBM.';
    $('folder-note').textContent=`${folder.directories.length} thư mục · ${folder.videos.length} video`;
  } catch(error) {$('folder-content').textContent=error.message;}
}
async function refreshLogs() {
  if(!state.logId||!$('logs-dialog').open) return;
  try {const lines=await api(`/api/${state.logKind}/${state.logId}/logs`); $('logs-content').textContent=lines.length?lines.map(line=>`[${date(line.timestamp)}] ${line.message}`).join('\n'):'Chưa có hoạt động.';}
  catch(error){$('logs-content').textContent=error.message;}
}
function exportCsv() {
  const sanitize=value=>{let text=String(value??''); if(/^[=+\-@\t\r]/.test(text)) text="'"+text; return '"'+text.replace(/"/g,'""')+'"';};
  const rows=[['ID','Tên luồng','Video local','Thời gian UTC+7','Kiểu live','Thời lượng','Bitrate kbps','Trạng thái'],...state.jobs.map(job=>[job.id,job.name,job.source_path,date(job.start_at),job.mode,job.mode==='continuous'?'24/7':duration(job.duration),job.bitrate,LABELS[job.status]])];
  const url=URL.createObjectURL(new Blob(['\ufeff'+rows.map(row=>row.map(sanitize).join(',')).join('\r\n')],{type:'text/csv;charset=utf-8'}));
  const anchor=document.createElement('a'); anchor.href=url; anchor.download='lich-livestream.csv'; anchor.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
}
document.addEventListener('DOMContentLoaded',async()=>{
  resetForm();
  $('stream-form').onsubmit=event=>{event.preventDefault();save(false);};
  setupArchives();
  setupUpdates();
  $('trim-after-live').onchange=()=>$('youtube-video-id').required=$('trim-after-live').checked;
  $('live-now').onclick=()=>{if($('stream-form').reportValidity()) confirmAction('Bắt đầu phát ngay?', 'Máy sẽ gửi video tới máy chủ và stream key bạn đã nhập ngay bây giờ.',()=>save(true),'Bắt đầu phát');};
  $('mode-public').onclick=()=>setMode('public'); $('mode-continuous').onclick=()=>setMode('continuous');
  $('toggle-key').onclick=()=>$('stream-key').type=$('stream-key').type==='password'?'text':'password';
  $('encoder').onchange=encoderChanged; $('cancel-edit').onclick=resetForm; $('source-path').onchange=probeSource;
  $('browse').onclick=()=>{$('file-dialog').showModal();openFolder(state.folder);};
  $('folder-open').onclick=()=>openFolder($('folder-path').value.trim());
  $('folder-path').onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();openFolder(event.target.value.trim());}};
  $('folder-up').onclick=()=>openFolder(state.parent||'');
  document.querySelectorAll('[data-close]').forEach(button=>button.onclick=()=>$(button.dataset.close).close());
  $('search').oninput=render; $('filter-status').onchange=render; $('filter-mode').onchange=render;
  $('refresh').onclick=()=>refresh().then(()=>toast('Đã cập nhật danh sách.')).catch(error=>toast(error.message,'error'));
  $('export').onclick=exportCsv;
  $('clear-done').onclick=()=>confirmAction('Xóa lịch đã hoàn tất?', 'Gỡ các lịch hoàn tất và lỡ lịch khỏi danh sách trên máy.',async()=>{const result=await api('/api/completed',{method:'DELETE'});toast(`Đã xóa ${result.removed} lịch.`);},'Xóa khỏi danh sách');
  $('streams-body').onclick=async event=>{
    const button=event.target.closest('[data-action]'); if(!button)return;
    const job=state.jobs.find(item=>item.id===button.dataset.id); if(!job)return;
    const action=button.dataset.action;
    if(action==='edit')return editJob(job);
    if(action==='logs'){state.logId=job.id;state.logKind='streams';$('logs-title').textContent=`Log · ${job.name}`;$('logs-dialog').showModal();return refreshLogs();}
    if(action==='delete')return confirmAction('Xóa lịch livestream?',`Gỡ “${job.name}” và log của lịch này khỏi danh sách. File video nguồn được giữ nguyên.`,async()=>{await api(`/api/streams/${job.id}`,{method:'DELETE'});toast('Đã xóa lịch phát.');},'Xóa lịch');
    if(action==='start')return confirmAction('Chạy luồng ngay?',`Bắt đầu gửi video cho “${job.name}” ngay bây giờ.`,async()=>{await api(`/api/streams/${job.id}/start`,{method:'POST'});toast('Đã khởi động máy phát.');},'Chạy ngay');
    try{button.disabled=true;await api(`/api/streams/${job.id}/stop`,{method:'POST'});toast('Đang dừng luồng này.');await refresh();}catch(error){toast(error.message,'error');}finally{button.disabled=false;}
  };
  const updateClock=()=>$('clock').textContent=new Intl.DateTimeFormat('vi-VN',{timeZone:'Asia/Ho_Chi_Minh',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date()); updateClock();setInterval(updateClock,1000);
  try {const info=await api('/api/system');state.token=info.token;$('capacity').textContent=`Tối đa ${info.max_streams} luồng đồng thời`;$('tool-status').textContent=info.tools.ffmpeg&&info.tools.ffprobe?'FFmpeg & FFprobe sẵn sàng':'Cần cài FFmpeg / FFprobe';if(!info.tools.ffmpeg||!info.tools.ffprobe)toast('Chưa tìm thấy FFmpeg hoặc FFprobe. Xem hướng dẫn trong README.','error');await refresh();}
  catch(error){toast(error.message,'error');}
  let offline=false;
  setInterval(async()=>{if(state.polling)return;state.polling=true;try{await refresh();await refreshLogs();if(offline)toast('Đã kết nối lại máy phát.');offline=false;}catch(error){if(!offline)toast('Mất kết nối web local. Kiểm tra cửa sổ máy phát.','error');offline=true;}finally{state.polling=false;}},2000);
});

const ARCHIVE_LABELS={queued:'Chờ kiểm tra',waiting_live:'Chờ live kết thúc',processing:'YouTube đang xử lý',preparing:'Chuẩn bị cắt',submitting:'Đang gửi lệnh cắt',verifying:'Đang xác minh',publishing:'Đang công khai',completed:'Hoàn tất',needs_login:'Cần đăng nhập',needs_review:'Cần kiểm tra',failed:'Lỗi',cancelled:'Đã dừng'};
const ARCHIVE_PAUSED=new Set(['needs_login','needs_review','failed','cancelled']);
function renderArchives(data) {
  state.archives=data.tasks; $('studio-status').textContent=data.browser_message;
  const session=data.session||{};
  $('studio-connect').disabled=!data.login_pending; $('studio-cancel-login').hidden=!data.login_pending;
  $('studio-session').textContent=session.channel_id?`Kênh: ${session.channel_id} · ${session.connected?'Phiên đã lưu · Chạy nền':'Cần kiểm tra / đăng nhập lại'}${session.verified_at?' · Xác minh: '+date(session.verified_at):''}`:'Đăng nhập Google, chọn kênh rồi bấm “Đăng nhập xong — chạy nền”. Tool sẽ lưu phiên trên máy và tự xử lý các bản lưu đã chọn.';
  $('archives-empty').hidden=data.tasks.length>0;
  $('archives-body').innerHTML=data.tasks.map(task=>`<tr><td><a href="https://studio.youtube.com/video/${escapeHTML(task.video_id)}/copyright" target="_blank" rel="noreferrer">${escapeHTML(task.video_id)} ↗</a><span class="stream-meta">${task.publish_after?'Hết claim → Công khai':'Giữ chế độ hiển thị'} · ${Number(task.cuts)} lượt cắt</span></td><td><span class="status-chip ${task.status==='completed'?'completed':ARCHIVE_PAUSED.has(task.status)?'failed':'starting'}">${ARCHIVE_LABELS[task.status]||escapeHTML(task.status)}</span></td><td class="archive-message">${escapeHTML(task.message)}<span class="stream-meta">${date(task.updated_at)}</span></td><td><div class="actions">${iconButton('archive-logs',task.id,'Xem log','≡')}${task.status==='completed'?iconButton('archive-retry',task.id,'Kiểm tra lại','↻'):ARCHIVE_PAUSED.has(task.status)?iconButton('archive-retry',task.id,'Tiếp tục','▶'):iconButton('archive-cancel',task.id,'Dừng tự động','■','stop')}</div></td></tr>`).join('');
}

function renderUpdate(data) {
  const info=data.installed, busy=['checking','downloading','installing'].includes(data.status);
  state.installedCommit=info.commit;
  $('update-version').textContent=`${info.version} · ${info.commit?info.commit.slice(0,8):'chưa có commit'} · ${info.packaged?'Windows EXE':'Bản mã nguồn'}`;
  $('update-message').textContent=data.message+(data.status==='downloading'?` ${size(data.downloaded)} / ${size(data.total)}`:'');
  $('update-check').disabled=busy; $('update-download').disabled=busy||data.status!=='available'||!info.packaged;
  $('update-install').disabled=busy||!data.can_install;
  $('update-progress').hidden=data.status!=='downloading'; $('update-progress').max=data.total||1; $('update-progress').value=data.downloaded||0;
  $('update-release').href=data.release_url;
}
function setupUpdates() {
  $('app-stop').onclick=()=>confirmAction('Thoát Local Live Studio?', 'Các luồng đang phát và tác vụ nền sẽ dừng. Dữ liệu đã lưu được giữ lại.',async()=>{
    await api('/api/app/stop',{method:'POST'});toast('Đang thoát chương trình.');
  },'Thoát tool');
  for(const action of ['check','download']) $('update-'+action).onclick=async()=>{
    try {renderUpdate(await api('/api/update/'+action,{method:'POST'}));}catch(error){toast(error.message,'error');}
  };
  $('update-install').onclick=()=>confirmAction('Cài bản mới và khởi động lại?', 'Tool sẽ thay EXE bằng bản đã kiểm tra SHA-256, giữ dữ liệu và khởi động lại. Các luồng live cần dừng trước khi cài.',async()=>{
    await api('/api/update/install',{method:'POST'});state.restarting=true;toast('Đang cập nhật. Giữ trang này mở để kết nối lại.');
  },'Cài bản mới');
}
function setupArchives() {
  $('studio-open').onclick=async()=>{
    try {await api('/api/studio/open',{method:'POST'}); toast('Đang mở cửa sổ Studio riêng. Đăng nhập và chọn kênh tại đó.'); await refresh();}
    catch(error){toast(error.message,'error');}
  };
  $('studio-connect').onclick=async()=>{
    try {await api('/api/studio/connect',{method:'POST'}); toast('Đang kiểm tra phiên trước khi chuyển sang chạy nền.'); await refresh();}
    catch(error){toast(error.message,'error');}
  };
  $('studio-cancel-login').onclick=async()=>{
    try {await api('/api/studio/cancel-login',{method:'POST'}); await refresh();}
    catch(error){toast(error.message,'error');}
  };
  $('archive-form').onsubmit=async event=>{
    event.preventDefault(); if(!$('archive-form').reportValidity()) return;
    $('archive-add').disabled=true;
    try {const task=await api('/api/archives',{method:'POST',body:JSON.stringify({video:$('archive-video').value.trim(),publish_after:$('archive-publish').checked,accept_permanent_cut:$('archive-consent').checked})}); toast(task.existing?'Video đã có trong danh sách với thiết lập đã lưu. Dùng Tiếp tục / Kiểm tra lại ở dòng đó.':'Đã đưa bản lưu vào danh sách kiểm tra.'); $('archive-video').value=''; $('archive-consent').checked=false; await refresh();}
    catch(error){toast(error.message,'error');}finally{$('archive-add').disabled=false;}
  };
  $('archives-body').onclick=async event=>{
    const button=event.target.closest('[data-action]'); if(!button)return;
    const task=state.archives.find(item=>item.id===button.dataset.id); if(!task)return;
    const action=button.dataset.action;
    if(action==='archive-logs'){state.logId=task.id;state.logKind='archives';$('logs-title').textContent=`Log bản lưu · ${task.video_id}`;$('logs-dialog').showModal();return refreshLogs();}
    try {button.disabled=true;await api(`/api/archives/${task.id}/${action==='archive-retry'?'retry':'cancel'}`,{method:'POST'});await refresh();}
    catch(error){toast(error.message,'error');}finally{button.disabled=false;}
  };
}
