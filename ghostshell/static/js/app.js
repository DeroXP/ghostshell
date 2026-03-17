(function () {
  const DASHBOARD_REFRESH_MS = 15000;
  const LOG_MAX_LINES = 500;

  document.addEventListener('DOMContentLoaded', () => {
    initWindowControls();
    initMatrixRain();
    initLogStream();
    initPageSpecificFeatures();
  });

  function initWindowControls() {
    const minBtn = document.getElementById('btn-min');
    const closeBtn = document.getElementById('btn-close');
    if (minBtn) {
      minBtn.addEventListener('click', async () => {
        try { await fetch('/api/window/minimize', { method: 'POST' }); } catch {}
      });
    }
    if (closeBtn) {
      closeBtn.addEventListener('click', async () => {
        try { await fetch('/api/window/close', { method: 'POST' }); } catch {}
      });
    }
  }

  function initLogStream() {
    const logTarget = document.getElementById('log') || document.querySelector('[data-log-target]');
    if (!logTarget || typeof EventSource === 'undefined') return;

    const source = new EventSource('/api/logs/stream');
    const lines = [];
    source.onmessage = (event) => {
      if (!event.data) return; // heartbeat
      lines.push(event.data);
      if (lines.length > LOG_MAX_LINES) lines.splice(0, lines.length - LOG_MAX_LINES);
      logTarget.textContent = lines.join('\n');
      logTarget.scrollTop = logTarget.scrollHeight;
    };
    source.onerror = () => {
      source.close();
      setTimeout(initLogStream, 3000);
    };
  }

  function initPageSpecificFeatures() {
    const elevateBtn = document.getElementById('btn-elevate');
    if (elevateBtn) initElevateFlow(elevateBtn);
    // Dashboard fetch on index page
    if (document.getElementById('app')) {
      fetchDashboard();
      setInterval(fetchDashboard, DASHBOARD_REFRESH_MS);
    }
  }

  function initElevateFlow(button) {
    const status = document.getElementById('elevate-status');
    const setStatus = (msg, isError) => {
      if (!status) return;
      status.textContent = msg;
      status.style.color = isError ? '#f66' : '#0f0';
    };
    button.addEventListener('click', async () => {
      setStatus('Requesting elevation... Accept the UAC prompt.', false);
      try {
        const res = await fetch('/api/elevate', { method: 'POST' });
        const data = await res.json();
        if (data.relaunching) setStatus('Relaunching...', false);
        else if (data.error) setStatus('Error: ' + data.error, true);
      } catch (e) {
        setStatus('Error: ' + (e && e.message ? e.message : e), true);
      }
    });
  }

  async function fetchDashboard() {
    try {
      const res = await fetch('/api/dashboard', { cache: 'no-store' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      renderDashboard(data);
    } catch (e) {
      // optional: surface error
    }
  }

  function renderDashboard(d) {
    const osEl = document.getElementById('os-version');
    const cpuEl = document.getElementById('cpu-model');
    const gpuEl = document.getElementById('gpu-models');
    const ramEl = document.getElementById('ram-total');
    const storEl = document.getElementById('storage-primary');

    if (osEl && d.os) osEl.textContent = `${d.os.system || 'Windows'} ${d.os.edition || ''} (Build ${d.os.build || ''})`;
    if (cpuEl && d.cpu) cpuEl.textContent = `${d.cpu.model || ''} (${d.cpu.logical_cores || '?'} threads)`;
    if (gpuEl && Array.isArray(d.gpu)) gpuEl.textContent = d.gpu.map(g => g.model).filter(Boolean).join(', ');
    if (ramEl && d.ram) ramEl.textContent = `${d.ram.total_gb || '?'} GB`;
    if (storEl && Array.isArray(d.storage) && d.storage.length) {
      const s = d.storage[0];
      storEl.textContent = `${s.name || ''} ${s.type || ''} ${s.total_gb || '?'} GB`;
    }
  }

  function initMatrixRain() {
    const canvas = document.getElementById('matrix') || document.querySelector('canvas[data-matrix]');
    if (!canvas || !(canvas instanceof HTMLCanvasElement)) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const chars = 'ｱｲｳｴｵｶｷｸｹｺ0123456789ABCDEFGHJKLMNPQRSTUVWXYZ';
    const fontSize = 16;
    const resize = () => { canvas.width = window.innerWidth; canvas.height = window.innerHeight; };
    window.addEventListener('resize', resize); resize();
    let columns = Math.floor(canvas.width / fontSize);
    const drops = Array(columns).fill(1);

    (function draw(){
      ctx.fillStyle = 'rgba(10,10,15,0.15)';
      ctx.fillRect(0,0,canvas.width,canvas.height);
      ctx.fillStyle = '#00ffd5';
      ctx.font = fontSize + 'px JetBrainsMono, monospace';
      for (let i=0;i<drops.length;i++){
        const text = chars.charAt(Math.floor(Math.random()*chars.length));
        const x = i*fontSize;
        const y = drops[i]*fontSize;
        ctx.fillText(text, x, y);
        if (y > canvas.height && Math.random() > 0.975) drops[i]=0; else drops[i]++;
      }
      requestAnimationFrame(draw);
    })();
  }
})();