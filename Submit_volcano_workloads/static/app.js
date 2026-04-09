(function () {
  const $ = (id) => document.getElementById(id);

  const els = {
    statusDot: $("statusDot"),
    statusText: $("statusText"),
    btnExport: $("btnExport"),
    btnRun: $("btnRun"),
    progressFill: $("progressFill"),
    progressLabel: $("progressLabel"),
    runMsg: $("runMsg"),
    fileCluster: $("fileCluster"),
    fileWorkload: $("fileWorkload"),
    filePlugins: $("filePlugins"),
    scaleInput: $("scaleInput"),
    chartMount: $("chartMount"),
    fragTable: $("fragTable"),
  };

  let statusTimer = null;
  let runPollTimer = null;

  async function fetchHealth() {
    try {
      const r = await fetch("/api/health");
      const j = await r.json();
      const ok = j.simulator_reachable === true;
      els.statusDot.classList.toggle("ok", ok);
      els.statusDot.classList.toggle("bad", !ok);
      els.statusText.textContent = ok
        ? "Simulator: OK"
        : "Simulator: " + (j.simulator_detail || "unreachable");
    } catch (e) {
      els.statusDot.classList.remove("ok");
      els.statusDot.classList.add("bad");
      els.statusText.textContent = "API unreachable";
    }
  }

  function buildChartSvg(chart) {
    const algorithms = chart.algorithms || [];
    const points = chart.points || [];
    const scales = [...new Set((chart.scales || []).map(Number))].sort((a, b) => a - b);
    if (!scales.length) {
      return '<p class="card-desc">No data yet. Run a simulation.</p>';
    }

    const W = 880;
    const H = 520;
    const padL = 72;
    const padR = 72;
    const padT = 48;
    const padB = 120;
    const innerW = W - padL - padR;
    const innerH = H - padT - padB;
    const sMin = scales[0];
    const sMax = scales[scales.length - 1] || sMin;
    const sSpan = Math.max(1e-6, sMax - sMin);

    const xOf = (s) => padL + ((Number(s) - sMin) / sSpan) * innerW;
    const yAlloc = (v) => padT + innerH - (Math.min(100, Math.max(0, v)) / 100) * innerH;

    let maxPods = 1;
    points.forEach((p) => {
      maxPods = Math.max(maxPods, Number(p.running_pods) || 0);
    });
    const yPods = (n) => padT + innerH - (Math.min(maxPods, Math.max(0, n)) / maxPods) * innerH;
    const barH = (n) => (Math.min(maxPods, Math.max(0, n)) / maxPods) * innerH;

    const barW = 14;
    const algoCount = Math.max(1, algorithms.length);
    const offsetStep = barW + 4;
    const offsetStart = (-((algoCount - 1) * offsetStep) / 2);

    let paths = "";
    let bars = "";
    let legend = "";

    algorithms.forEach((algo, ai) => {
      const color = algo.color || "#64748b";
      const pts = scales
        .map((sc) => {
          const row = points.find(
            (p) => p.algorithm_id === algo.id && Number(p.scale) === Number(sc)
          );
          return row
            ? { s: sc, alloc: Number(row.allocation_rate_avg) || 0, pods: Number(row.running_pods) || 0 }
            : null;
        })
        .filter(Boolean);

      if (pts.length) {
        const first = pts[0];
        const rest = pts
          .slice(1)
          .map((p) => `${xOf(p.s).toFixed(1)},${yAlloc(p.alloc).toFixed(1)}`)
          .join(" L ");
        const d0 = `M ${xOf(first.s).toFixed(1)},${yAlloc(first.alloc).toFixed(1)}`;
        const d = rest ? `${d0} L ${rest}` : d0;
        paths += `<path d="${d}" fill="none" stroke="${color}" stroke-width="2.5" stroke-linecap="round" />`;
      }

      scales.forEach((sc) => {
        const row = points.find(
          (p) => p.algorithm_id === algo.id && Number(p.scale) === Number(sc)
        );
        if (!row) return;
        const pods = Number(row.running_pods) || 0;
        const bx = xOf(sc) + offsetStart + ai * offsetStep;
        const h = barH(pods);
        const y0 = padT + innerH;
        bars += `<g transform="translate(${bx.toFixed(1)},${y0})">
          <rect x="0" y="${(-h).toFixed(1)}" width="${barW}" height="${h.toFixed(1)}" fill="${color}" opacity="0.2" rx="2" />
          <rect x="0" y="${(-h).toFixed(1)}" width="${barW}" height="3" fill="${color}" opacity="0.5" rx="1" />
        </g>`;
      });

      legend += `<g transform="translate(${20 + ai * 200},0)">
        <rect x="0" y="-14" width="10" height="10" rx="2" fill="${color}" />
        <text x="16" y="-5" class="lg">${escapeXml(algo.name)}</text>
        <line x1="0" y1="8" x2="18" y2="8" stroke="${color}" stroke-width="2" />
        <text x="22" y="12" class="lg-sm">avg node alloc %</text>
      </g>`;
    });

    let grid = "";
    [0, 25, 50, 75, 100].forEach((tick) => {
      const y = yAlloc(tick);
      grid += `<line x1="${padL}" y1="${y.toFixed(1)}" x2="${W - padR}" y2="${y.toFixed(1)}" stroke="#f1f5f9" />`;
      grid += `<text x="${padL - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end" class="tick">${tick}%</text>`;
    });

    const podTicks = 5;
    for (let i = 0; i <= podTicks; i++) {
      const val = Math.round((maxPods * i) / podTicks);
      const z = yPods(val);
      grid += `<text x="${W - padR + 10}" y="${(z + 4).toFixed(1)}" class="tick-r">${val}</text>`;
    }

    let xTicks = "";
    scales.forEach((sc) => {
      const x = xOf(sc);
      xTicks += `<line x1="${x.toFixed(1)}" y1="${H - padB}" x2="${x.toFixed(1)}" y2="${H - padB + 6}" stroke="#cbd5e1" />`;
      xTicks += `<text x="${x.toFixed(1)}" y="${H - padB + 22}" text-anchor="middle" class="tick">${sc}x</text>`;
    });

    return `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">
      <style>
        .axis-label { font-size: 10px; font-weight: 800; fill: #94a3b8; text-transform: uppercase; letter-spacing: 0.08em; }
        .tick { font-size: 10px; fill: #94a3b8; font-family: ui-monospace, monospace; }
        .tick-r { font-size: 10px; fill: #cbd5e1; font-family: ui-monospace, monospace; font-weight: 700; }
        .lg { font-size: 12px; font-weight: 700; fill: #334155; }
        .lg-sm { font-size: 9px; fill: #94a3b8; font-weight: 700; }
      </style>
      <text transform="translate(${padL - 52},${(padT + H - padB) / 2}) rotate(-90)" text-anchor="middle" class="axis-label">Allocation rate %</text>
      <text transform="translate(${W - padR + 52},${(padT + H - padB) / 2}) rotate(90)" text-anchor="middle" class="axis-label">Running pods</text>
      <text x="${(padL + W - padR) / 2}" y="${H - 24}" text-anchor="middle" class="axis-label">Workload scale</text>
      ${grid}
      ${xTicks}
      ${bars}
      ${paths}
      <g transform="translate(${padL}, ${H - 52})">${legend}</g>
    </svg>`;
  }

  function escapeXml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function buildFragTable(chart) {
    const algorithms = chart.algorithms || [];
    const scales = [...new Set((chart.scales || []).map(Number))].sort((a, b) => a - b);
    const points = chart.points || [];
    const thead = els.fragTable.querySelector("thead tr");
    const tbody = els.fragTable.querySelector("tbody");
    thead.innerHTML = '<th>Algorithm</th>' + scales.map((s) => `<th>${s}x</th>`).join("");
    tbody.innerHTML = "";
    algorithms.forEach((algo) => {
      const tr = document.createElement("tr");
      const nameCell = document.createElement("td");
      nameCell.innerHTML = `<div class="algo-cell"><span class="algo-swatch" style="background:${algo.color}"></span>${escapeXml(algo.name)}</div>`;
      tr.appendChild(nameCell);
      scales.forEach((sc) => {
        const td = document.createElement("td");
        const row = points.find(
          (p) => p.algorithm_id === algo.id && Number(p.scale) === Number(sc)
        );
        const v = row ? Number(row.fragmentation_rate).toFixed(2) : null;
        td.textContent = v === null ? "—" : v + "%";
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
  }

  function renderChart(chart) {
    els.chartMount.innerHTML = buildChartSvg(chart);
    buildFragTable(chart);
  }

  async function pollRunStatus() {
    try {
      const r = await fetch("/api/status");
      const j = await r.json();
      // 兼容 { state: {...} } 与历史扁平结构
      const st = j.state != null ? j.state : j;
      const pct = Math.min(100, Math.max(0, Number(st.progress_percent) || 0));
      els.progressFill.style.width = pct + "%";
      els.progressLabel.textContent = st.status === "succeeded" ? "✓" : Math.round(pct) + "%";
      els.progressFill.classList.toggle("done", st.status === "succeeded");
      els.runMsg.textContent = st.message || st.current_step_label || "";
      if (st.chart) renderChart(st.chart);
      if (st.status === "succeeded") {
        clearInterval(runPollTimer);
        runPollTimer = null;
        els.btnRun.disabled = false;
        els.btnExport.disabled = false;
      }
      if (st.status === "failed") {
        clearInterval(runPollTimer);
        runPollTimer = null;
        els.btnRun.disabled = false;
        els.runMsg.textContent = "Error: " + (st.error || "unknown");
      }
    } catch (e) {
      els.runMsg.textContent = "Status poll failed";
    }
  }

  els.btnRun.addEventListener("click", async () => {
    const fc = els.fileCluster.files[0];
    const fw = els.fileWorkload.files[0];
    const fps = els.filePlugins.files;
    if (!fc || !fw || !fps.length) {
      alert("请选择 cluster、workload 与至少一个 plugins 文件。");
      return;
    }
    els.btnRun.disabled = true;
    els.btnExport.disabled = true;
    els.progressFill.style.width = "0%";
    els.progressFill.classList.remove("done");
    els.progressLabel.textContent = "0%";
    els.runMsg.textContent = "Submitting…";

    const fd = new FormData();
    fd.append("cluster", fc);
    fd.append("workload", fw);
    fd.append("workload_scales", els.scaleInput.value.trim() || "1.0");
    for (let i = 0; i < fps.length; i++) fd.append("plugins", fps[i]);

    try {
      const r = await fetch("/api/runs", { method: "POST", body: fd });
      if (r.status === 409) {
        els.runMsg.textContent = "已有任务在运行，请稍后。";
        els.btnRun.disabled = false;
        return;
      }
      if (!r.ok) {
        const t = await r.text();
        els.runMsg.textContent = "Failed: " + t;
        els.btnRun.disabled = false;
        return;
      }
      if (runPollTimer) clearInterval(runPollTimer);
      runPollTimer = setInterval(pollRunStatus, 400);
      pollRunStatus();
    } catch (e) {
      els.runMsg.textContent = String(e);
      els.btnRun.disabled = false;
    }
  });

  els.btnExport.addEventListener("click", async () => {
    try {
      const r = await fetch("/api/runs/latest/export");
      if (!r.ok) {
        const t = await r.text();
        alert(t);
        return;
      }
      const blob = await r.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "volcano_sim_export.zip";
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) {
      alert(String(e));
    }
  });

  renderChart({ algorithms: [], scales: [], points: [] });
  fetchHealth();
  statusTimer = setInterval(fetchHealth, 4000);
})();
