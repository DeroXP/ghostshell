(() => {
  'use strict';

  const REFRESH_MS = 15000;
  const state = {
    optimizerLoaded: false,
    optimizerTweaks: [],
    networkLoaded: false,
    networkTweaks: [],
    dnsPresets: null,
    gpuLoaded: false,
    vaultUnlocked: false,
    vaultLoadedOnce: false,
  };

  document.addEventListener('DOMContentLoaded', () => {
    initWindowControls();
    initMatrixRain();
    initLogStream();
    initNavigation();
    initDashboard();
    initQuickActions();
    initProfiles();
    initDebloat();
    initOptimizer();
    initNetwork();
    initGPU();
    initPrivacy();
    initHWID();
    initVault();
  });

  // --- Helpers ---
  function qs(sel, root=document){ return root.querySelector(sel); }
  function qsa(sel, root=document){ return Array.from(root.querySelectorAll(sel)); }

  async function api(url, {method='GET', body, headers, ...rest}={}){
    const opts = {method, headers: headers?{...headers}:{}, ...rest};
    if (body !== undefined){
      opts.body = typeof body==='string' ? body : JSON.stringify(body);
      if (!opts.headers['Content-Type']) opts.headers['Content-Type'] = 'application/json';
    }
    const res = await fetch(url, opts);
    const ct = res.headers.get('content-type')||'';
    let payload = null;
    if (ct.includes('application/json')) payload = await res.json();
    else payload = await res.text();
    if (!res.ok){
      const msg = (payload && payload.error) || (payload && payload.message) || res.statusText || `HTTP ${res.status}`;
      throw new Error(msg);
    }
    return payload;
  }

  function appendLog(msg){
    const pre = qs('#log');
    if (!pre) return;
    const t = new Date().toISOString().replace('T',' ').replace('Z','');
    const lines = (pre.textContent||'').split('\n');
    lines.push(`[${t}] ${msg}`);
    if (lines.length>500) lines.splice(0, lines.length-500);
    pre.textContent = lines.join('\n');
    pre.scrollTop = pre.scrollHeight;
  }

  function setStatus(el, text, ok=true){ if (!el) return; el.textContent = text; el.style.color = ok? '#00ffd5':'#f66'; }

  // --- Navigation ---
  function initNavigation(){
    const items = qsa('.nav-item');
    const sections = qsa('.main .section');
    const show = (id)=>{
      sections.forEach(s=>{ s.hidden = (s.id !== id); });
      items.forEach(i=> i.classList.toggle('active', i.getAttribute('data-section')===id));
      // Lazy-load on section entry
      if (id==='optimize' && !state.optimizerLoaded) ensureOptimize();
      if (id==='network' && !state.networkLoaded) ensureNetwork();
      if (id==='gpu' && !state.gpuLoaded) ensureGPUInfo();
      if (id==='vault' && !state.vaultLoadedOnce) ensureVaultBasics();
    };
    items.forEach(i=> i.addEventListener('click', ()=> show(i.getAttribute('data-section'))));
    show('dashboard');
  }

  // --- Logs SSE ---
  function initLogStream(){
    const pre = qs('#log') || qs('[data-log-target]');
    if (!pre || typeof EventSource==='undefined') return;
    const src = new EventSource('/api/logs/stream');
    const buf=[];
    src.onmessage = (e)=>{ if (!e.data) return; buf.push(e.data); if (buf.length>500) buf.splice(0, buf.length-500); pre.textContent = buf.join('\n'); pre.scrollTop = pre.scrollHeight; };
    src.onerror = ()=>{ try{src.close();}catch{} setTimeout(initLogStream, 3000); };
  }

  // --- Window controls ---
  function initWindowControls(){
    const minBtn = qs('#btn-min');
    const closeBtn = qs('#btn-close');
    if (minBtn) minBtn.addEventListener('click', ()=> fetch('/api/window/minimize',{method:'POST'}).catch(()=>{}));
    if (closeBtn) closeBtn.addEventListener('click', ()=> fetch('/api/window/close',{method:'POST'}).catch(()=>{}));
  }

  // --- Matrix rain ---
  function initMatrixRain(){
    const canvas = document.getElementById('matrix');
    if (!canvas || !(canvas instanceof HTMLCanvasElement)) return;
    const ctx = canvas.getContext('2d'); if (!ctx) return;
    const chars = 'ｱｲｳｴｵｶｷｸｹｺ0123456789ABCDEFGHJKLMNPQRSTUVWXYZ';
    const fontSize = 16;
    const resize = ()=>{ canvas.width = window.innerWidth; canvas.height = window.innerHeight; };
    window.addEventListener('resize', resize); resize();
    let columns = Math.floor(canvas.width / fontSize);
    const drops = Array(columns).fill(1);
    (function draw(){
      ctx.fillStyle = 'rgba(10,10,15,0.15)'; ctx.fillRect(0,0,canvas.width,canvas.height);
      ctx.fillStyle = '#00ffd5'; ctx.font = fontSize + 'px JetBrainsMono, monospace';
      for (let i=0;i<drops.length;i++){
        const text = chars.charAt(Math.floor(Math.random()*chars.length));
        const x = i*fontSize; const y = drops[i]*fontSize; ctx.fillText(text, x, y);
        if (y > canvas.height && Math.random() > 0.975) drops[i]=0; else drops[i]++;
      }
      requestAnimationFrame(draw);
    })();
  }

  // --- Dashboard ---
  function initDashboard(){
    const tick = async ()=>{ try{ const d = await api('/api/dashboard'); renderDashboard(d);}catch{} };
    tick(); setInterval(tick, REFRESH_MS);
  }
  function renderDashboard(d){
    const osEl=qs('#os-version'), cpuEl=qs('#cpu-model'), gpuEl=qs('#gpu-models'), ramEl=qs('#ram-total'), storEl=qs('#storage-primary');
    if (osEl && d.os) osEl.textContent = `${d.os.system||'Windows'} ${d.os.edition||''} (Build ${d.os.build||''})`;
    if (cpuEl && d.cpu) cpuEl.textContent = `${d.cpu.model||''} (${d.cpu.logical_cores||'?'} threads)`;
    if (gpuEl && Array.isArray(d.gpu)) gpuEl.textContent = d.gpu.map(g=>g.model).filter(Boolean).join(', ');
    if (ramEl && d.ram) ramEl.textContent = `${d.ram.total_gb||'?'} GB`;
    if (storEl && Array.isArray(d.storage) && d.storage.length){ const s=d.storage[0]; storEl.textContent = `${s.name||''} ${s.type||''} ${s.total_gb||'?'} GB`; }
  }

  // --- Quick actions & Profiles ---
  function initQuickActions(){
    const qd=qs('#qa-debloat'); if (qd) qd.addEventListener('click', runDebloat);
    const qo=qs('#qa-optimize'); if (qo) qo.addEventListener('click', applyAllTweaks);
    const qp=qs('#qa-privacy'); if (qp) qp.addEventListener('click', hardenPrivacy);
    const qb=qs('#qa-benchmark'); if (qb) qb.addEventListener('click', async()=>{ try{ await api('/api/benchmark/run'); appendLog('Benchmark started'); }catch(e){ appendLog('Benchmark error: '+e.message); }});
  }

  function initProfiles(){
    const ex=qs('#profile-export'); const im=qs('#profile-import'); const name=qs('#profile-name'); const status=qs('#profile-status');
    if (ex) ex.addEventListener('click', async()=>{
      const n=(name&&name.value.trim())||'profile';
      try{ const r=await api('/api/profile/export',{method:'POST', body:{name:n}}); setStatus(status, r.message||'Exported'); appendLog(`Profile exported: ${n}`);}catch(e){ setStatus(status, e.message, false); appendLog('Profile export failed: '+e.message);} });
    if (im) im.addEventListener('click', async()=>{
      const n=(name&&name.value.trim())||'profile';
      try{ const r=await api('/api/profile/import',{method:'POST', body:{name_or_path:n, apply:true}}); setStatus(status, r.message||'Imported'); appendLog(`Profile imported: ${n}`);}catch(e){ setStatus(status, e.message, false); appendLog('Profile import failed: '+e.message);} });
  }

  // --- Debloat ---
  function initDebloat(){ const b=qs('#debloat-run'); if (b) b.addEventListener('click', runDebloat); }
  async function runDebloat(){ try{ await api('/api/debloat/execute',{method:'POST', body:{}}); appendLog('Debloat started'); }catch(e){ appendLog('Debloat error: '+e.message);} }

  // --- Optimizer ---
  function initOptimizer(){
    const sa=qs('#opt-select-all'), da=qs('#opt-deselect-all'), ap=qs('#opt-apply');
    if (sa) sa.addEventListener('click', ()=> setAllOptimize(true));
    if (da) da.addEventListener('click', ()=> setAllOptimize(false));
    if (ap) ap.addEventListener('click', applySelectedTweaks);
    ensureOptimize();
  }
  async function ensureOptimize(){ if (state.optimizerLoaded) return; try{ const r=await api('/api/optimizer/catalog'); state.optimizerTweaks = Array.isArray(r.tweaks)? r.tweaks:[]; renderOptimizeList(); state.optimizerLoaded=true; }catch(e){ appendLog('Optimizer catalog error: '+e.message);} }
  function renderOptimizeList(){ const box=qs('#optimize-list'); if (!box) return; box.innerHTML=''; state.optimizerTweaks.forEach(t=>{ const id=`opt_${t.key}`; const label=document.createElement('label'); label.className='checkitem'; label.innerHTML = `<input type="checkbox" data-key="${t.key}" id="${id}"> <strong>${escapeHtml(t.title||t.key)}</strong><br><small>${escapeHtml(t.description||'')}</small>`; box.appendChild(label); }); }
  function setAllOptimize(v){ qsa('#optimize-list input[type="checkbox"]').forEach(cb=> cb.checked=v); }
  async function applySelectedTweaks(){ const sel = qsa('#optimize-list input[type="checkbox"]').filter(cb=>cb.checked).map(cb=>cb.getAttribute('data-key')); const out=qs('#opt-results'); if (!sel.length){ setStatus(out,'No tweaks selected', false); return; } try{ const r=await api('/api/optimizer/apply',{method:'POST', body:{selected:sel}}); const ok=(r.results||[]).filter(x=>x[1]).length; const total=(r.results||[]).length; setStatus(out, `Applied ${ok}/${total} tweaks`); appendLog(`Optimizer applied ${ok}/${total}`);}catch(e){ setStatus(out, e.message, false); appendLog('Optimizer apply error: '+e.message);} }
  async function applyAllTweaks(){ await ensureOptimize(); qsa('#optimize-list input[type="checkbox"]').forEach(cb=> cb.checked=true); await applySelectedTweaks(); }

  // --- Network ---
  function initNetwork(){ const load=qs('#net-load'); const apply=qs('#net-apply'); const dnsApply=qs('#dns-apply'); const pingBtn=qs('#ping-go');
    if (load) load.addEventListener('click', ensureNetworkTweaks);
    if (apply) apply.addEventListener('click', applyNetworkTweaks);
    if (dnsApply) dnsApply.addEventListener('click', applyDNS);
    if (pingBtn) pingBtn.addEventListener('click', doPing);
    ensureDNSPresets();
  }
  async function ensureNetwork(){ await ensureDNSPresets(); await ensureNetworkTweaks(); }
  async function ensureDNSPresets(){ if (state.dnsPresets) return; try{ const r=await api('/api/dns/presets'); state.dnsPresets = r.presets||{}; renderDnsPresets(); }catch(e){ appendLog('DNS preset load error: '+e.message);} }
  function renderDnsPresets(){ const box=qs('#dns-presets'); if (!box) return; box.innerHTML=''; const presets=state.dnsPresets||{}; Object.keys(presets).forEach(key=>{ const ips=presets[key]||[]; const id=`dns_${key}`; const label=document.createElement('label'); label.className='radioitem'; label.innerHTML = `<input type="radio" name="dnsPreset" value="${key}" id="${id}"> <strong>${escapeHtml(key)}</strong> <small>${ips.join(', ')}</small>`; box.appendChild(label); }); box.addEventListener('change', (e)=>{ const r=e.target; if (r && r.name==='dnsPreset'){ const ips=state.dnsPresets[r.value]||[]; const p=qs('#dns-primary'), s=qs('#dns-secondary'); if (p) p.value=ips[0]||''; if (s) s.value=ips[1]||''; }}); }
  async function applyDNS(){ const sel=qs('#dns-presets input[type="radio"]:checked'); try{ let res; if (sel){ res=await api('/api/dns/apply',{method:'POST', body:{preset: sel.value}});} else { const p=qs('#dns-primary')?.value.trim(); const s=qs('#dns-secondary')?.value.trim(); res=await api('/api/dns/apply',{method:'POST', body:{primary:p, secondary:s}});} appendLog('DNS: '+(res.message||'applied')); }catch(e){ appendLog('DNS apply error: '+e.message);} }
  async function ensureNetworkTweaks(){ if (state.networkLoaded) return; try{ const r=await api('/api/network/catalog'); state.networkTweaks = Array.isArray(r.tweaks)? r.tweaks:[]; renderNetworkTweaks(); state.networkLoaded=true; }catch(e){ appendLog('Network tweaks load error: '+e.message);} }
  function renderNetworkTweaks(){ const box=qs('#network-list'); if (!box) return; box.innerHTML=''; state.networkTweaks.forEach(t=>{ const id=`net_${t.key}`; const label=document.createElement('label'); label.className='checkitem'; label.innerHTML = `<input type="checkbox" data-key="${t.key}" id="${id}"> <strong>${escapeHtml(t.title||t.key)}</strong><br><small>${escapeHtml(t.description||'')}</small>`; box.appendChild(label); }); }
  async function applyNetworkTweaks(){ const sel = qsa('#network-list input[type="checkbox"]').filter(cb=>cb.checked).map(cb=>cb.getAttribute('data-key')); if (!sel.length){ appendLog('No network tweaks selected'); return; } try{ const r=await api('/api/network/apply',{method:'POST', body:{selected:sel}}); const ok=(r.results||[]).filter(x=>x[1]).length; const total=(r.results||[]).length; appendLog(`Network tweaks: ${ok}/${total} applied`);}catch(e){ appendLog('Network apply error: '+e.message);} }
  async function doPing(){ const host=qs('#ping-host')?.value.trim()||''; const out=qs('#ping-result'); if (!host){ setStatus(out,'Host required', false); return; } try{ const r=await api('/api/network/ping',{method:'POST', body:{host}}); const msg = r && typeof r.avg==='number' ? `avg ${r.avg} ms (min ${r.min}, max ${r.max})` : 'no data'; setStatus(out, msg, true); appendLog(`Ping ${host}: ${msg}`);}catch(e){ setStatus(out, e.message, false); appendLog('Ping error: '+e.message);} }

  // --- GPU ---
  function initGPU(){ const prep=qs('#gpu-prepare'); const tw=qs('#gpu-tweaks'); if (prep) prep.addEventListener('click', gpuPrepare); if (tw) tw.addEventListener('click', gpuTweaks); ensureGPUInfo(); }
  async function ensureGPUInfo(){ if (state.gpuLoaded) return; try{ const r=await api('/api/gpu/info'); const info=r.info||{}; const m=qs('#gpu-model'), d=qs('#gpu-driver'); if (m) m.textContent = info.model||'—'; if (d) d.textContent = info.driver_version||'—'; state.gpuLoaded=true; }catch(e){ appendLog('GPU info error: '+e.message);} }
  async function gpuPrepare(){ try{ const r=await api('/api/gpu/prepare',{method:'POST', body:{}}); const notes=qs('#gpu-readme'); if (notes) notes.value = (r.readme ? `README: ${r.readme}\n` : '') + (r.message||'Prepared'); appendLog('GPU prepare: '+(r.message||'done')); }catch(e){ appendLog('GPU prepare error: '+e.message);} }
  async function gpuTweaks(){ try{ const r=await api('/api/gpu/tweaks',{method:'POST'}); appendLog('GPU tweaks: '+(r.message||'applied')); }catch(e){ appendLog('GPU tweaks error: '+e.message);} }

  // --- Privacy ---
  function initPrivacy(){ const b=qs('#privacy-harden'); if (b) b.addEventListener('click', hardenPrivacy); }
  async function hardenPrivacy(){ try{ await api('/api/privacy/harden',{method:'POST'}); appendLog('Privacy hardening started'); }catch(e){ appendLog('Privacy error: '+e.message);} }

  // --- HWID ---
  function initHWID(){ const gen=qs('#hwid-gen-guid'); const apply=qs('#hwid-apply'); const rest=qs('#hwid-restore'); const out=qs('#hwid-status');
    if (gen) gen.addEventListener('click', async()=>{ try{ const r=await api('/api/utils/guid'); const el=qs('#hwid-guid'); if (el) el.value=r.guid; setStatus(out,'GUID generated'); }catch(e){ setStatus(out,e.message,false);} });
    if (apply) apply.addEventListener('click', async()=>{ const changes={}; const g=qs('#hwid-guid')?.value.trim(); const p=qs('#hwid-prodid')?.value.trim(); const n=qs('#hwid-name')?.value.trim(); if (g) changes.machine_guid=g; if (p) changes.product_id=p; if (n) changes.computer_name=n; if (!Object.keys(changes).length){ setStatus(out,'Nothing to apply', false); return; } try{ const r=await api('/api/hwid/apply',{method:'POST', body:{changes}}); setStatus(out, r.message||'HWID applied'); appendLog('HWID apply: '+(r.message||'')); }catch(e){ setStatus(out,e.message,false); appendLog('HWID apply error: '+e.message);} });
    if (rest) rest.addEventListener('click', async()=>{ try{ const r=await api('/api/hwid/restore',{method:'POST'}); setStatus(out, r.message||'Restored'); appendLog('HWID restore: '+(r.message||'')); }catch(e){ setStatus(out,e.message,false); appendLog('HWID restore error: '+e.message);} });
  }

  // --- Vault ---
  function initVault(){ const setup=qs('#vault-setup-pin'); const unlock=qs('#vault-unlock'); const form=qs('#vault-entry-form');
    if (setup) setup.addEventListener('click', vaultSetupPin);
    if (unlock) unlock.addEventListener('click', vaultUnlock);
    if (form) form.addEventListener('submit', vaultAddEntry);
  }
  async function ensureVaultBasics(){ state.vaultLoadedOnce = true; await refreshVaultEntries(true); }
  async function vaultSetupPin(){ const pin=qs('#vault-pin')?.value.trim(); if (!pin){ appendLog('Enter a PIN'); return; } try{ const r=await api('/api/vault/setup',{method:'POST', body:{pin}}); appendLog(r.ok? 'Vault PIN set':'Vault PIN setup failed'); }catch(e){ appendLog('Vault setup error: '+e.message);} }
  async function vaultUnlock(){ const pin=qs('#vault-pin')?.value.trim(); if (!pin){ appendLog('Enter PIN to unlock'); return; } try{ const r=await api('/api/vault/unlock',{method:'POST', body:{pin}}); if (r.ok){ state.vaultUnlocked=true; qs('#vault-locked-state')?.setAttribute('hidden',''); qs('#vault-unlocked-state')?.removeAttribute('hidden'); await refreshVaultEntries(); appendLog('Vault unlocked'); } else { appendLog('Vault unlock failed: '+(r.message||'')); } }catch(e){ appendLog('Vault unlock error: '+e.message);} }
  async function vaultAddEntry(e){ e.preventDefault(); if (!state.vaultUnlocked){ appendLog('Unlock vault first'); return; } const service=qs('#vault-service')?.value.trim(); const username=qs('#vault-username')?.value.trim(); const password=qs('#vault-password')?.value.trim(); if (!service||!username||!password){ appendLog('Fill all fields'); return; } try{ await api('/api/vault/add',{method:'POST', body:{service,username,password}}); await refreshVaultEntries(); appendLog('Vault entry added'); (e.target.reset && e.target.reset()); }catch(e2){ appendLog('Vault add error: '+e2.message);} }
  async function refreshVaultEntries(silent=false){ if (!state.vaultUnlocked){ if (!silent) appendLog('Vault is locked'); return; } try{ const r=await api('/api/vault/entries'); const list=qs('#vault-list'); if (list){ list.innerHTML=''; (r.entries||[]).forEach(en=>{ const item=document.createElement('div'); item.className='vault-item'; item.textContent = `${en.service||''} — ${en.username||''}`; list.appendChild(item); }); } }catch(e){ if (!silent) appendLog('Vault list error: '+e.message);} }

  // --- Utility ---
  function escapeHtml(s){ return (s||'').replace(/[&<>"']/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c])); }

})();
