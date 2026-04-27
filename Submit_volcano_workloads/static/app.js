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
    granBtn1: $("granBtn1"),
    granBtn25: $("granBtn25"),
  };

  const selectedNpuGrans = new Set([1]);

  function syncGranButtons() {
    [els.granBtn1, els.granBtn25].forEach((btn) => {
      if (!btn) return;
      const v = Number(btn.getAttribute("data-gran"), 10);
      const on = selectedNpuGrans.has(v);
      btn.classList.toggle("is-on", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  function wireGranToggles() {
    [els.granBtn1, els.granBtn25].forEach((btn) => {
      if (!btn) return;
      btn.addEventListener("click", () => {
        const v = Number(btn.getAttribute("data-gran"), 10);
        if (selectedNpuGrans.has(v)) {
          if (selectedNpuGrans.size <= 1) return;
          selectedNpuGrans.delete(v);
        } else {
          selectedNpuGrans.add(v);
        }
        syncGranButtons();
      });
    });
    syncGranButtons();
  }

  wireGranToggles();

  let statusTimer = null;
  let runPollTimer = null;
  /** Go simulator /stepResult reachable (from /api/health). */
  let simulatorReachable = false;
  /** POST /api/runs accepted and polling until succeeded/failed. */
  let runInProgress = false;

  function syncStartButton() {
    els.btnRun.disabled = !simulatorReachable || runInProgress;
  }

  function wireFilePicker(input, button, nameEl, emptyText, multiple) {
    function update() {
      const files = input.files;
      if (!files || !files.length) {
        nameEl.textContent = emptyText;
        return;
      }
      if (multiple) {
        const names = Array.from(files)
          .map((f) => f.name)
          .join(", ");
        const summary =
          files.length === 1
            ? names
            : files.length + " files: " + names;
        nameEl.textContent =
          summary.length > 72 ? summary.slice(0, 69) + "…" : summary;
      } else {
        nameEl.textContent = files[0].name;
      }
    }
    button.addEventListener("click", () => input.click());
    input.addEventListener("change", update);
    update();
  }

  wireFilePicker(
    els.fileCluster,
    document.getElementById("btnPickCluster"),
    document.getElementById("nameCluster"),
    "No file chosen",
    false
  );
  wireFilePicker(
    els.fileWorkload,
    document.getElementById("btnPickWorkload"),
    document.getElementById("nameWorkload"),
    "No file chosen",
    false
  );
  wireFilePicker(
    els.filePlugins,
    document.getElementById("btnPickPlugins"),
    document.getElementById("namePlugins"),
    "No files chosen",
    true
  );

  async function fetchHealth() {
    try {
      const r = await fetch("/api/health");
      const j = await r.json();
      const ok = j.simulator_reachable === true;
      simulatorReachable = ok;
      els.statusDot.classList.toggle("ok", ok);
      els.statusDot.classList.toggle("bad", !ok);
      if (ok) {
        els.statusText.textContent = "Simulator: OK";
      } else {
        const detail = j.simulator_detail ? " (" + j.simulator_detail + ")" : "";
        els.statusText.textContent = "Simulator: not OK";
      }
    } catch (e) {
      simulatorReachable = false;
      els.statusDot.classList.remove("ok");
      els.statusDot.classList.add("bad");
      els.statusText.textContent = "Web API unreachable";
    }
    syncStartButton();
  }

  function granularitiesFromChart(chart) {
    const raw = chart.granularities;
    if (raw && raw.length) {
      return [...new Set(raw.map(Number))]
        .filter((n) => !Number.isNaN(n))
        .sort((a, b) => a - b);
    }
    const s = new Set();
    (chart.points || []).forEach((p) => {
      if (p.npu_granularity_percent == null || p.npu_granularity_percent === "") return;
      const n = Number(p.npu_granularity_percent);
      if (!Number.isNaN(n)) s.add(n);
    });
    const arr = [...s].sort((a, b) => a - b);
    return arr.length ? arr : [1];
  }

  function findPointRow(points, algoId, sc, gran) {
    return points.find(
      (p) =>
        p.algorithm_id === algoId &&
        Number(p.scale) === Number(sc) &&
        Number(p.npu_granularity_percent) === Number(gran)
    );
  }

  /** Flexnpu-memory line: 1% dashed, 25% dotted; other gran matches 1%. */
  function memoryLineStyleAttr(gran) {
    const g = Number(gran);
    if (g === 25) {
      return ' stroke-dasharray="2 5" stroke-linecap="round" stroke-opacity="0.78"';
    }
    return ' stroke-dasharray="8 5" stroke-linecap="round" stroke-opacity="0.78"';
  }

  function buildChartSvg(chart) {
    const algorithms = chart.algorithms || [];
    const points = chart.points || [];
    const scales = [...new Set((chart.scales || []).map(Number))].sort((a, b) => a - b);
    if (!scales.length) {
      return '<p class="card-desc">No data yet. Run a simulation.</p>';
    }

    const granularities = granularitiesFromChart(chart);
    const dashForGranIndex = (gi) => {
      const d = ["", "6 4", "3 3", "8 3 2 3"];
      return d[gi % d.length] || "";
    };

    const PLOT_W = 880;
    const H = 520;
    const padL = 72;
    const padR = 72;
    const padT = 48;
    const padB = 120;
    const innerW = PLOT_W - padL - padR;
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

    const barW = 12;
    const seriesCount = Math.max(1, algorithms.length * Math.max(1, granularities.length));
    const offsetStep = barW + 3;
    const offsetStart = (-((seriesCount - 1) * offsetStep) / 2);

    let paths = "";
    let bars = "";
    let legend = "";
    const legendStepX = 220;
    const legendHitW = Math.max(160, legendStepX - 12);

    algorithms.forEach((algo, ai) => {
      const color = algo.color || "#64748b";
      granularities.forEach((gran, gi) => {
        const seriesIdx = ai * granularities.length + gi;
        const dash = dashForGranIndex(gi);
        const dashAttr = dash ? ` stroke-dasharray="${dash}"` : "";
        const memLineAttr = memoryLineStyleAttr(gran);

        const pts = scales
          .map((sc) => {
            const row = findPointRow(points, algo.id, sc, gran);
            return row
              ? {
                  s: sc,
                  alloc: Number(row.allocation_rate_avg) || 0,
                  mem: Number(row.allocation_memory_rate_avg) || 0,
                  pods: Number(row.running_pods) || 0,
                }
              : null;
          })
          .filter(Boolean);

        let pathInner = "";
        if (pts.length) {
          const firstM = pts[0];
          const restM = pts
            .slice(1)
            .map((p) => `${xOf(p.s).toFixed(1)},${yAlloc(p.mem).toFixed(1)}`)
            .join(" L ");
          const d0m = `M ${xOf(firstM.s).toFixed(1)},${yAlloc(firstM.mem).toFixed(1)}`;
          const dm = restM ? `${d0m} L ${restM}` : d0m;
          pathInner += `<path d="${dm}" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round"${memLineAttr} />`;

          const first = pts[0];
          const rest = pts
            .slice(1)
            .map((p) => `${xOf(p.s).toFixed(1)},${yAlloc(p.alloc).toFixed(1)}`)
            .join(" L ");
          const d0 = `M ${xOf(first.s).toFixed(1)},${yAlloc(first.alloc).toFixed(1)}`;
          const d = rest ? `${d0} L ${rest}` : d0;
          pathInner += `<path d="${d}" fill="none" stroke="${color}" stroke-width="2.5" stroke-linecap="round"${dashAttr} />`;
        }
        paths += `<g class="chart-series" data-series="${seriesIdx}">${pathInner}</g>`;

        let barsInner = "";
        scales.forEach((sc) => {
          const row = findPointRow(points, algo.id, sc, gran);
          if (!row) return;
          const pods = Number(row.running_pods) || 0;
          const bx = xOf(sc) + offsetStart + seriesIdx * offsetStep;
          const h = barH(pods);
          const y0 = padT + innerH;
          barsInner += `<g transform="translate(${bx.toFixed(1)},${y0})">
          <rect x="0" y="${(-h).toFixed(1)}" width="${barW}" height="${h.toFixed(1)}" fill="${color}" opacity="0.2" rx="2" />
          <rect x="0" y="${(-h).toFixed(1)}" width="${barW}" height="3" fill="${color}" opacity="0.5" rx="1" />
        </g>`;
        });
        bars += `<g class="chart-series" data-series="${seriesIdx}">${barsInner}</g>`;

        const legLabel = escapeXml(algo.name) + " · " + gran + "%";
        const lx = 12 + seriesIdx * legendStepX;
        legend += `<g class="legend-item" data-series="${seriesIdx}" transform="translate(${lx},0)">
        <rect x="0" y="-14" width="10" height="10" rx="2" fill="${color}" />
        <text x="16" y="-5" class="lg">${legLabel}</text>
        <line x1="0" y1="10" x2="18" y2="10" stroke="${color}" stroke-width="2"${dashAttr} />
        <text x="22" y="13" class="lg-sm">Flexnpu-core</text>
        <line x1="0" y1="24" x2="18" y2="24" stroke="${color}" stroke-width="2"${memLineAttr} />
        <text x="22" y="27" class="lg-sm">Flexnpu-memory</text>
        <text x="0" y="42" class="lg-sm">Core / memory alloc %</text>
        <rect class="legend-hit" data-series="${seriesIdx}" x="0" y="-16" width="${legendHitW}" height="22" fill="transparent" pointer-events="all" />
      </g>`;
      });
    });

    let grid = "";
    [0, 25, 50, 75, 100].forEach((tick) => {
      const y = yAlloc(tick);
      grid += `<line x1="${padL}" y1="${y.toFixed(1)}" x2="${PLOT_W - padR}" y2="${y.toFixed(1)}" stroke="#f1f5f9" />`;
      grid += `<text x="${padL - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end" class="tick">${tick}%</text>`;
    });

    const podTicks = 5;
    for (let i = 0; i <= podTicks; i++) {
      const val = Math.round((maxPods * i) / podTicks);
      const z = yPods(val);
      grid += `<text x="${PLOT_W - padR + 10}" y="${(z + 4).toFixed(1)}" class="tick-r">${val}</text>`;
    }

    let xTicks = "";
    scales.forEach((sc) => {
      const x = xOf(sc);
      xTicks += `<line x1="${x.toFixed(1)}" y1="${H - padB}" x2="${x.toFixed(1)}" y2="${H - padB + 6}" stroke="#cbd5e1" />`;
      xTicks += `<text x="${x.toFixed(1)}" y="${H - padB + 22}" text-anchor="middle" class="tick">${sc}x</text>`;
    });

    const legendTotalW =
      seriesCount > 0 ? 12 + (seriesCount - 1) * legendStepX + legendHitW + 16 : 200;
    const svgCanvasW = Math.max(PLOT_W, legendTotalW + 48);
    const legendTranslateX = (svgCanvasW - legendTotalW) / 2;

    return `<svg viewBox="0 0 ${svgCanvasW} ${H}" width="${svgCanvasW}" height="${H}" preserveAspectRatio="xMinYMid meet" xmlns="http://www.w3.org/2000/svg" style="min-width:${svgCanvasW}px;max-width:none">
      <style>
        .axis-label { font-size: 10px; font-weight: 800; fill: #94a3b8; text-transform: uppercase; letter-spacing: 0.08em; }
        .tick { font-size: 10px; fill: #94a3b8; font-family: ui-monospace, monospace; }
        .tick-r { font-size: 10px; fill: #cbd5e1; font-family: ui-monospace, monospace; font-weight: 700; }
        .lg { font-size: 12px; font-weight: 700; fill: #334155; }
        .lg-sm { font-size: 9px; fill: #94a3b8; font-weight: 700; }
        .chart-series.is-hidden { display: none; }
        .legend-item.is-hidden { opacity: 0.38; }
        .legend-hit { cursor: pointer; transition: fill 0.15s ease, opacity 0.15s ease; }
        .legend-hit:hover { fill: rgba(148, 163, 184, 0.22); }
      </style>
      <text transform="translate(${padL - 52},${(padT + H - padB) / 2}) rotate(-90)" text-anchor="middle" class="axis-label">Flexnpu core &amp; memory alloc %</text>
      <text transform="translate(${PLOT_W - padR + 52},${(padT + H - padB) / 2}) rotate(90)" text-anchor="middle" class="axis-label">Running pods</text>
      <text x="${(padL + PLOT_W - padR) / 2}" y="${H - 70}" text-anchor="middle" class="axis-label">Workload scale</text>
      ${grid}
      ${xTicks}
      ${bars}
      ${paths}
      <g transform="translate(${legendTranslateX}, ${H - 52})">${legend}</g>
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
    const gList = granularitiesFromChart(chart);
    const points = chart.points || [];
    const theadEl = els.fragTable.querySelector("thead");
    const tbody = els.fragTable.querySelector("tbody");
    theadEl.innerHTML = "";
    tbody.innerHTML = "";

    const colCount = 2 + scales.length * 4;
    const placeholderColSpan = Math.max(3, colCount);

    if (!scales.length || !algorithms.length) {
      const tr = document.createElement("tr");
      const th = document.createElement("th");
      th.colSpan = placeholderColSpan;
      th.className = "frag-matrix-placeholder";
      th.textContent = "No data yet. Run a simulation.";
      tr.appendChild(th);
      theadEl.appendChild(tr);
      return;
    }

    const trTop = document.createElement("tr");
    const thCorner = document.createElement("th");
    thCorner.colSpan = 2;
    thCorner.className = "frag-matrix-corner";
    trTop.appendChild(thCorner);
    scales.forEach((s) => {
      const th = document.createElement("th");
      th.colSpan = 4;
      th.className = "frag-matrix-scale-band";
      th.textContent = "Workload scale " + s + "x";
      trTop.appendChild(th);
    });
    theadEl.appendChild(trTop);

    const trSub = document.createElement("tr");
    const hAlgo = document.createElement("th");
    hAlgo.textContent = "Algorithm";
    hAlgo.className = "frag-matrix-subhead";
    trSub.appendChild(hAlgo);
    const hGran = document.createElement("th");
    hGran.textContent = "NPU granularity";
    hGran.className = "frag-matrix-subhead";
    trSub.appendChild(hGran);
    const metricLabels = [
      "Flexnpu-core Allocation",
      "Flexnpu-memory Allocation",
      "Running pods",
      "Fragmentation %",
    ];
    scales.forEach(() => {
      metricLabels.forEach((label) => {
        const th = document.createElement("th");
        th.textContent = label;
        th.className = "frag-matrix-subhead frag-matrix-metric-head";
        trSub.appendChild(th);
      });
    });
    theadEl.appendChild(trSub);

    algorithms.forEach((algo) => {
      gList.forEach((gran, gi) => {
        const tr = document.createElement("tr");
        if (gi === 0) {
          const nameCell = document.createElement("td");
          nameCell.rowSpan = gList.length;
          nameCell.className = "frag-matrix-algo";
          nameCell.innerHTML = `<div class="algo-cell algo-cell-matrix"><span class="algo-swatch" style="background:${algo.color}"></span>${escapeXml(algo.name)}</div>`;
          tr.appendChild(nameCell);
        }
        const granCell = document.createElement("td");
        granCell.className = "frag-matrix-gran";
        granCell.textContent = gran + "%";
        tr.appendChild(granCell);

        scales.forEach((sc) => {
          const row = findPointRow(points, algo.id, sc, gran);
          const allocTd = document.createElement("td");
          allocTd.className = "frag-matrix-num";
          allocTd.textContent =
            row && row.allocation_rate_avg != null
              ? Number(row.allocation_rate_avg).toFixed(2) + "%"
              : "—";
          tr.appendChild(allocTd);

          const memAllocTd = document.createElement("td");
          memAllocTd.className = "frag-matrix-num";
          memAllocTd.textContent =
            row && row.allocation_memory_rate_avg != null
              ? Number(row.allocation_memory_rate_avg).toFixed(2) + "%"
              : "—";
          tr.appendChild(memAllocTd);

          const podsTd = document.createElement("td");
          podsTd.className = "frag-matrix-num";
          podsTd.textContent =
            row && row.running_pods != null ? String(Math.round(Number(row.running_pods) || 0)) : "—";
          tr.appendChild(podsTd);

          const fragTd = document.createElement("td");
          fragTd.className = "frag-matrix-num";
          const fv =
            row && row.fragmentation_rate != null ? Number(row.fragmentation_rate).toFixed(2) : null;
          fragTd.textContent = fv === null ? "—" : fv + "%";
          tr.appendChild(fragTd);
        });

        tbody.appendChild(tr);
      });
    });
  }

  function wireChartLegendInteraction(mount) {
    const svg = mount && mount.querySelector && mount.querySelector("svg");
    if (!svg) return;
    svg.addEventListener("click", function chartLegendClick(ev) {
      const hit = ev.target.closest(".legend-hit");
      if (!hit || !svg.contains(hit)) return;
      const id = hit.getAttribute("data-series");
      if (id == null || id === "") return;
      ev.preventDefault();
      svg.querySelectorAll(`g.chart-series[data-series="${id}"]`).forEach((g) => {
        g.classList.toggle("is-hidden");
      });
      const leg = svg.querySelector(`g.legend-item[data-series="${id}"]`);
      if (leg) leg.classList.toggle("is-hidden");
    });
  }

  function renderChart(chart) {
    els.chartMount.innerHTML = buildChartSvg(chart);
    wireChartLegendInteraction(els.chartMount);
    buildFragTable(chart);
  }

  async function pollRunStatus() {
    try {
      const r = await fetch("/api/status");
      const j = await r.json();
      // Support both { state: {...} } and legacy flat JSON
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
        runInProgress = false;
        els.btnExport.disabled = false;
        syncStartButton();
      }
      if (st.status === "failed") {
        clearInterval(runPollTimer);
        runPollTimer = null;
        runInProgress = false;
        els.runMsg.textContent = "Error: " + (st.error || "unknown");
        syncStartButton();
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
      alert("Please select cluster, workload, and at least one plugins file.");
      return;
    }
    if (!simulatorReachable) {
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
    fd.append(
      "npu_granularity_percents",
      Array.from(selectedNpuGrans)
        .sort((a, b) => a - b)
        .join(",")
    );
    for (let i = 0; i < fps.length; i++) fd.append("plugins", fps[i]);

    try {
      const r = await fetch("/api/runs", { method: "POST", body: fd });
      if (r.status === 409) {
        els.runMsg.textContent = "A simulation is already running. Please wait.";
        syncStartButton();
        return;
      }
      if (!r.ok) {
        const t = await r.text();
        els.runMsg.textContent = "Failed: " + t;
        syncStartButton();
        return;
      }
      runInProgress = true;
      syncStartButton();
      if (runPollTimer) clearInterval(runPollTimer);
      runPollTimer = setInterval(pollRunStatus, 400);
      pollRunStatus();
    } catch (e) {
      els.runMsg.textContent = String(e);
      runInProgress = false;
      syncStartButton();
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

  renderChart({ algorithms: [], scales: [], granularities: [], points: [] });
  fetchHealth();
  statusTimer = setInterval(fetchHealth, 4000);
})();
