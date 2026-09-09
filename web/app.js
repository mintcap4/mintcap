/* mintcap 웹 프런트 -- analysis 테이블을 그리기만 한다.
   외부 차트 라이브러리를 쓰지 않는다: 장치가 인터넷 없이도 떠야 하고,
   필요한 도형이 선·점·사각형뿐이라 인라인 SVG 로 충분하다. */

const REGIMES = ["clean", "matter", "human", "mixed"];
const REGIME_KO = { clean: "청정", matter: "물질", human: "인체", mixed: "복합" };
const REGIME_DESC = {
  clean: "저CO₂ · 저VOC — 무조치",
  matter: "저CO₂ · 고VOC — 공기청정기",
  human: "고CO₂ · 저VOC — 환풍기",
  mixed: "고CO₂ · 고VOC — 둘 다",
};
// app.css 의 --regime-* 와 같은 값. dataviz 검증 통과(dark, 인접 쌍).
const REGIME_COLOR = {
  clean: "#199e70", matter: "#d95926", human: "#3987e5", mixed: "#e66767",
};
const SEQ = ["#184f95", "#1c5cab", "#256abf", "#2a78d6", "#3987e5", "#5598e7", "#6da7ec", "#86b6ef"];

// 시계열에서 그릴 변수 -- 축이 하나여야 하므로 변수마다 차트를 따로 만든다.
const SERIES = [
  { key: "co2", label: "CO₂", unit: "ppm", color: "#3987e5" },
  { key: "voc", label: "VOC", unit: "index", color: "#d95926" },
  { key: "pm2p5", label: "PM2.5", unit: "µg/m³", color: "#199e70" },
  { key: "scd_temp", label: "온도", unit: "°C", color: "#c98500" },
];

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const api = (p) => fetch(p).then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${r.status} ${p}`))));
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const num = (v, d = 0) => (v === null || v === undefined || Number.isNaN(v) ? "—" : Number(v).toLocaleString("ko-KR", { minimumFractionDigits: d, maximumFractionDigits: d }));
const pct = (v, d = 1) => (v == null ? "—" : `${(v * 100).toFixed(d)}%`);

/** UTC 문자열을 한국 시각으로. 센서는 UTC 로 저장하고 화면에서만 KST 로 읽는다. */
function kst(ts, opts = {}) {
  if (!ts) return "—";
  const d = new Date(ts.includes("T") ? ts : ts.replace(" ", "T") + "Z");
  if (Number.isNaN(+d)) return ts;
  return d.toLocaleString("ko-KR", { timeZone: "Asia/Seoul", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", ...opts });
}
function agoMin(ts) {
  if (!ts) return null;
  const d = new Date(ts.includes("T") ? ts : ts.replace(" ", "T") + "Z");
  return Number.isNaN(+d) ? null : Math.round((Date.now() - +d) / 60000);
}

const badge = (r) => (r ? `<span class="badge ${r}">${REGIME_KO[r] || r}</span>` : '<span class="muted">—</span>');

/* ============================ SVG 도우미 ============================ */
const SVGNS = "http://www.w3.org/2000/svg";
const el = (n, attrs = {}, txt) => {
  const e = document.createElementNS(SVGNS, n);
  for (const [k, v] of Object.entries(attrs)) if (v != null) e.setAttribute(k, v);
  if (txt != null) e.textContent = txt;
  return e;
};
const scale = (d0, d1, r0, r1) => (v) => (d1 === d0 ? (r0 + r1) / 2 : r0 + ((v - d0) / (d1 - d0)) * (r1 - r0));

/** 눈금값 -- 사람이 읽기 좋은 간격으로 약 n개. */
function ticks(lo, hi, n = 5) {
  if (!(hi > lo)) return [lo];
  const raw = (hi - lo) / n;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].find((m) => raw <= m * mag) * mag;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(10));
  return out;
}

/* ============================ 선 차트 ============================
   변수 하나당 차트 하나(축이 둘이면 안 되므로). 크로스헤어 + 툴팁 기본 탑재. */
function lineChart(rows, s, { w = 520, h = 190 } = {}) {
  const M = { t: 12, r: 14, b: 24, l: 46 };
  const pts = rows.map((r) => ({ t: new Date(r.ts.replace(" ", "T") + "Z"), v: r[s.key] }))
                  .filter((p) => p.v != null && !Number.isNaN(+p.t));

  const fig = document.createElement("figure");
  fig.className = "card chart-wrap";
  fig.innerHTML = `<figcaption style="margin:0 0 8px;color:var(--label-primary);font-weight:500">
      ${s.label} <span class="muted" style="font-weight:400">${s.unit}</span></figcaption>`;

  if (pts.length < 2) {
    fig.insertAdjacentHTML("beforeend", `<div class="muted" style="padding:24px 0">데이터 없음</div>`);
    return fig;
  }

  const [t0, t1] = [pts[0].t, pts[pts.length - 1].t];
  let [lo, hi] = [Math.min(...pts.map((p) => p.v)), Math.max(...pts.map((p) => p.v))];
  const pad = (hi - lo) * 0.12 || Math.abs(hi) * 0.1 || 1;
  [lo, hi] = [lo - pad, hi + pad];

  const x = scale(+t0, +t1, M.l, w - M.r);
  const y = scale(lo, hi, h - M.b, M.t);
  const svg = el("svg", { viewBox: `0 0 ${w} ${h}`, role: "img", "aria-label": `${s.label} 시계열` });

  const g = el("g", { class: "grid" });
  for (const v of ticks(lo, hi, 4)) {
    g.append(el("line", { x1: M.l, x2: w - M.r, y1: y(v), y2: y(v) }));
    g.append(el("text", { x: M.l - 8, y: y(v) + 4, "text-anchor": "end" }, num(v, hi - lo < 5 ? 1 : 0)));
  }
  svg.append(g);

  const ax = el("g", { class: "axis" });
  ax.append(el("line", { x1: M.l, x2: w - M.r, y1: h - M.b, y2: h - M.b }));
  for (const d of [t0, new Date((+t0 + +t1) / 2), t1]) {
    ax.append(el("text", {
      x: x(+d), y: h - M.b + 16,
      "text-anchor": d === t0 ? "start" : d === t1 ? "end" : "middle",
    }, kst(d.toISOString(), { month: undefined, day: undefined })));
  }
  svg.append(ax);

  svg.append(el("path", {
    class: "series", stroke: s.color,
    d: pts.map((p, i) => `${i ? "L" : "M"}${x(+p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(""),
  }));

  // 크로스헤어
  const cross = el("line", { stroke: "var(--label-tertiary)", "stroke-width": 1, y1: M.t, y2: h - M.b, opacity: 0 });
  const dot = el("circle", { r: 4, fill: s.color, class: "mark", opacity: 0 });
  svg.append(cross, dot);
  fig.append(svg);

  const tip = document.createElement("div");
  tip.className = "tooltip";
  tip.hidden = true;
  fig.append(tip);

  // 히트 영역은 마크보다 크게 -- 차트 전체를 받는다
  svg.addEventListener("pointermove", (ev) => {
    const box = svg.getBoundingClientRect();
    const px = ((ev.clientX - box.left) / box.width) * w;
    if (px < M.l || px > w - M.r) return;
    const target = t0.getTime() + ((px - M.l) / (w - M.r - M.l)) * (+t1 - +t0);
    const p = pts.reduce((a, b) => (Math.abs(+b.t - target) < Math.abs(+a.t - target) ? b : a));
    cross.setAttribute("x1", x(+p.t)); cross.setAttribute("x2", x(+p.t)); cross.setAttribute("opacity", 1);
    dot.setAttribute("cx", x(+p.t)); dot.setAttribute("cy", y(p.v)); dot.setAttribute("opacity", 1);
    tip.hidden = false;
    tip.innerHTML = `<div class="t-title">${kst(p.t.toISOString())}</div>
      <div class="t-row"><span style="--c:${s.color}"><i></i>${s.label}</span>
      <b>${num(p.v, 1)} ${s.unit}</b></div>`;
    const left = Math.min((x(+p.t) / w) * box.width + 12, box.width - tip.offsetWidth - 8);
    tip.style.left = `${Math.max(4, left)}px`;
    tip.style.top = `${(y(p.v) / h) * box.height - 8}px`;
  });
  svg.addEventListener("pointerleave", () => {
    tip.hidden = true; cross.setAttribute("opacity", 0); dot.setAttribute("opacity", 0);
  });
  return fig;
}

/* ============================ 레짐 평면 (산점도) ============================
   레짐은 이 평면 위의 위치로 정의되므로 색으로 구분하지 않는다 -- 4색 산점도는
   어느 두 점이든 나란히 놓일 수 있어 색각 검증(--pairs all)을 통과하지 못한다.
   대신 앵커선과 분면 이름으로 읽고, 현재 노드만 점으로 찍는다. */
function planeChart(nodes) {
  const w = 520, h = 380, M = { t: 16, r: 16, b: 40, l: 52 };
  const XMAX = Math.max(5, ...nodes.map((n) => n.x_co2 ?? 0)) * 1.1;
  const YMAX = Math.max(3, ...nodes.map((n) => n.x_voc ?? 0)) * 1.15;
  const x = scale(0, XMAX, M.l, w - M.r), y = scale(0, YMAX, h - M.b, M.t);
  const svg = el("svg", { viewBox: `0 0 ${w} ${h}`, role: "img", "aria-label": "레짐 평면" });

  const g = el("g", { class: "grid" });
  for (const v of ticks(0, XMAX, 5)) {
    g.append(el("line", { x1: x(v), x2: x(v), y1: M.t, y2: h - M.b }));
    g.append(el("text", { x: x(v), y: h - M.b + 16, "text-anchor": "middle" }, num(v, 1)));
  }
  for (const v of ticks(0, YMAX, 4)) {
    g.append(el("line", { x1: M.l, x2: w - M.r, y1: y(v), y2: y(v) }));
    g.append(el("text", { x: M.l - 8, y: y(v) + 4, "text-anchor": "end" }, num(v, 1)));
  }
  svg.append(g);

  // 앵커 -- 이름을 정하는 기준선
  const ax = 700 / 400, ay = 120 / 100;
  const anc = el("g", { class: "anchor" });
  anc.append(el("line", { x1: x(ax), x2: x(ax), y1: M.t, y2: h - M.b }));
  anc.append(el("line", { x1: M.l, x2: w - M.r, y1: y(ay), y2: y(ay) }));
  svg.append(anc);

  // 분면 이름 -- 색이 아니라 자리로 읽게
  const q = [
    ["clean", (M.l + x(ax)) / 2, (y(ay) + h - M.b) / 2],
    ["matter", (M.l + x(ax)) / 2, (M.t + y(ay)) / 2],
    ["human", (x(ax) + w - M.r) / 2, (y(ay) + h - M.b) / 2],
    ["mixed", (x(ax) + w - M.r) / 2, (M.t + y(ay)) / 2],
  ];
  for (const [name, cx, cy] of q) {
    svg.append(el("text", { x: cx, y: cy, "text-anchor": "middle", class: "quad-label" },
      `${REGIME_KO[name]} ${name}`));
  }

  svg.append(el("text", { x: (M.l + w - M.r) / 2, y: h - 6, "text-anchor": "middle" },
    "x_co2  =  CO₂ / 400"));
  svg.append(el("text", { x: 12, y: (M.t + h - M.b) / 2, "text-anchor": "middle",
    transform: `rotate(-90 12 ${(M.t + h - M.b) / 2})` }, "x_voc  =  VOC / 100"));

  const wrap = document.createElement("div");
  wrap.className = "chart-wrap";
  wrap.append(svg);
  const tip = document.createElement("div");
  tip.className = "tooltip"; tip.hidden = true;
  wrap.append(tip);

  const drawn = nodes.filter((n) => n.x_co2 != null && n.x_voc != null)
                     .map((n) => ({ n, cx: x(n.x_co2), cy: y(n.x_voc) }));

  for (const { n, cx, cy } of drawn) {
    const c = el("circle", {
      cx, cy, r: 7, fill: REGIME_COLOR[n.regime] || "var(--label-tertiary)",
      class: "mark", style: "cursor:pointer",
    });
    c.addEventListener("pointerenter", () => {
      tip.hidden = false;
      tip.innerHTML = `<div class="t-title">${esc(n.label || n.scope)}</div>
        <div class="t-row"><span>레짐</span><b>${REGIME_KO[n.regime] || "—"}</b></div>
        <div class="t-row"><span>CO₂</span><b>${num(n.co2)} ppm</b></div>
        <div class="t-row"><span>VOC</span><b>${num(n.voc, 1)}</b></div>
        <div class="t-row"><span>확신도</span><b>${n.p_max == null ? "—" : n.p_max.toFixed(2)}</b></div>`;
      const box = svg.getBoundingClientRect();
      tip.style.left = `${Math.min((cx / w) * box.width + 14, box.width - 150)}px`;
      tip.style.top = `${(cy / h) * box.height - 10}px`;
    });
    c.addEventListener("pointerleave", () => { tip.hidden = true; });
    svg.append(c);
  }

  // 라벨 충돌 회피 -- 교실이 다 비슷한 상태면 점이 한곳에 몰려 이름이 서로 겹친다.
  // 위에서부터 훑으며 최소 간격을 못 지키면 아래로 밀고, 점과 멀어지면 잇는 선을 그린다.
  const GAP = 13;
  let lastY = -Infinity;
  for (const d of [...drawn].sort((a, b) => a.cy - b.cy || a.cx - b.cx)) {
    const ly = Math.max(d.cy + 4, lastY + GAP);
    lastY = ly;
    const lx = d.cx + 12;
    if (Math.abs(ly - (d.cy + 4)) > 3) {
      svg.append(el("line", {
        x1: d.cx + 8, y1: d.cy, x2: lx - 2, y2: ly - 4,
        stroke: "var(--label-quaternary)", "stroke-width": 1,
      }));
    }
    svg.append(el("text", { x: lx, y: ly, style: "font-size:10px" },
      (d.n.label || d.n.scope).replace(/^node_/, "")));
  }
  return wrap;
}

/* ============================ 전이행렬 히트맵 ============================ */
function heatmap(regimes, matrix) {
  if (!regimes?.length) return `<div class="muted">일간 배치를 아직 돌리지 않았습니다.</div>`;
  const cell = (v) => {
    const i = Math.min(SEQ.length - 1, Math.max(0, Math.round(v * (SEQ.length - 1))));
    return SEQ[i];
  };
  const head = regimes.map((r) => `<th>${REGIME_KO[r]}</th>`).join("");
  const body = regimes.map((r, i) => `<tr><th style="text-align:left">${REGIME_KO[r]}</th>${
    regimes.map((_, j) => {
      const v = matrix[i]?.[j] ?? 0;
      return `<td class="cell" style="background:${cell(v)};opacity:${0.25 + 0.75 * v}"
                  title="${REGIME_KO[r]} → ${REGIME_KO[regimes[j]]}: ${pct(v, 1)}">${
        v >= 0.001 ? (v * 100).toFixed(1) : "·"}</td>`;
    }).join("")}</tr>`).join("");
  return `<div class="scroll-x"><table class="heat">
    <caption style="caption-side:top;text-align:left;color:var(--label-secondary);
             font-size:var(--t-caption1);padding-bottom:8px">행에서 → 열로 (%) · 5분 뒤</caption>
    <thead><tr><th></th>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

/* ============================ 막대 (체류) ============================ */
function dwellBars(med, counts) {
  const items = REGIMES.filter((r) => med[r] != null);
  if (!items.length) return `<div class="muted">데이터 없음</div>`;
  const max = Math.max(...items.map((r) => med[r]));
  return `<h3 style="margin:0 0 4px;font-size:var(--t-headline)">중앙 체류 시간</h3>
    <p class="sub" style="margin:0 0 12px">같은 레짐이 이어진 구간의 길이 중앙값.</p>
    ${items.map((r) => `
      <div style="margin-bottom:10px">
        <div style="display:flex;justify-content:space-between;font-size:var(--t-caption1);margin-bottom:4px">
          <span>${badge(r)}</span>
          <span><b>${num(med[r])}</b>분 <span class="muted">· ${num(counts[r])}구간</span></span>
        </div>
        <div style="height:8px;background:var(--fill-quaternary);border-radius:4px;overflow:hidden">
          <div style="width:${(med[r] / max) * 100}%;height:100%;background:${REGIME_COLOR[r]};border-radius:4px"></div>
        </div>
      </div>`).join("")}`;
}

/* ============================ 탭 ============================ */
const loaded = new Set();
function showTab(name) {
  $$(".tab").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.panel === name)));
  $$(".panel").forEach((p) => { p.hidden = p.id !== `panel-${name}`; });
  location.hash = name;
  if (!loaded.has(name)) { loaded.add(name); RENDER[name]().catch(showError); }
}
function showError(e) {
  console.error(e);
  const p = $(".panel:not([hidden])");
  if (p) p.insertAdjacentHTML("afterbegin",
    `<div class="note critical"><span>⚠️</span><div><strong>불러오기 실패.</strong> ${esc(e.message)}</div></div>`);
}

/* ============================ 렌더 ============================ */
const RENDER = {
  async monitor() {
    const [sum, regimes, nodes] = await Promise.all([api("/api/summary"), api("/api/regime"), api("/api/nodes")]);

    const share = sum.regime_share || {};
    $("#summary-tiles").innerHTML = [
      ["노드", num(sum.nodes), sum.last_bucket ? `마지막 ${kst(sum.last_bucket)}` : ""],
      ["CO₂ 중앙값", `${num(sum.co2_median)} <span class="muted" style="font-size:var(--t-footnote)">ppm</span>`, "노드별 최근값의 중앙값"],
      ["VOC 중앙값", num(sum.voc_median, 1), "index"],
      ["QC 탈락", num((sum.nodes_failing_qc || []).length),
       (sum.nodes_failing_qc || []).map((n) => n.replace(/^node_/, "")).join(", ") || "없음"],
    ].map(([k, v, n]) => `<div class="card tile"><div class="k">${k}</div>
        <div class="v">${v}</div><div class="n">${esc(n)}</div></div>`).join("");

    // 점유율 밴드 -- 인접 조각 사이 2px 표면 간격
    $("#share-band").innerHTML = REGIMES.filter((r) => share[r] > 0)
      .map((r) => `<i style="width:${share[r] * 100}%;background:${REGIME_COLOR[r]};
                   box-shadow:inset -2px 0 0 var(--bg-elevated)" title="${REGIME_KO[r]} ${pct(share[r])}"></i>`).join("");
    $("#share-legend").innerHTML = REGIMES.map((r) =>
      `<span style="--c:${REGIME_COLOR[r]}"><i></i>${REGIME_KO[r]} ${pct(share[r] || 0)}</span>`).join("");
    $("#share-table").innerHTML = `<table><thead><tr><th>레짐</th><th>뜻</th><th>비율</th></tr></thead>
      <tbody>${REGIMES.map((r) => `<tr><td>${badge(r)}</td>
        <td style="text-align:left;color:var(--label-secondary)">${REGIME_DESC[r]}</td>
        <td>${pct(share[r] || 0)}</td></tr>`).join("")}</tbody></table>`;

    $("#node-cards").innerHTML = regimes.map((n) => {
      const late = agoMin(n.bucket);
      return `<div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
          <b>${esc(n.label || n.scope)}</b>${badge(n.regime)}
        </div>
        <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-top:12px;
                    font-variant-numeric:tabular-nums">
          <div><div class="k muted" style="font-size:var(--t-caption2)">CO₂</div>
               <div style="font-size:var(--t-title3)">${num(n.co2)}<span class="muted" style="font-size:var(--t-caption1)"> ppm</span></div></div>
          <div><div class="k muted" style="font-size:var(--t-caption2)">VOC</div>
               <div style="font-size:var(--t-title3)">${num(n.voc, 1)}</div></div>
          <div><div class="k muted" style="font-size:var(--t-caption2)">PM2.5</div>
               <div>${num(n.pm2p5, 1)}<span class="muted"> µg/m³</span></div></div>
          <div><div class="k muted" style="font-size:var(--t-caption2)">온 · 습도</div>
               <div>${num(n.temp, 1)}°C · ${num(n.hum, 0)}%</div></div>
        </div>
        <div class="n muted" style="margin-top:12px;font-size:var(--t-caption1)">
          체류 ${n.dwell_min == null ? "—" : `${num(n.dwell_min)}분${n.dwell_censored ? "+" : ""}`}
          · 확신도 ${n.p_max == null ? "—" : n.p_max.toFixed(2)}
          ${late != null && late > 15 ? ` · <span style="color:var(--status-warning)">${num(late)}분 전 수신</span>` : ""}
        </div></div>`;
    }).join("") || `<div class="muted">노드 없음</div>`;

    const sel = $("#ts-node");
    sel.innerHTML = nodes.map((n) => `<option value="${esc(n.node)}">${esc(n.label)}</option>`).join("");
    $("#ts-legend").innerHTML = SERIES.map((s) =>
      `<span style="--c:${s.color}"><i></i>${s.label}</span>`).join("");
    const drawTs = async () => {
      const box = $("#ts-charts");
      box.innerHTML = `<div class="muted">불러오는 중…</div>`;
      const d = await api(`/api/timeseries?node=${encodeURIComponent(sel.value)}&hours=${$("#ts-hours").value}&cols=${SERIES.map((s) => s.key).join(",")}`);
      box.replaceChildren(...SERIES.map((s) => lineChart(d.rows, s)));
    };
    sel.onchange = $("#ts-hours").onchange = () => drawTs().catch(showError);
    if (nodes.length) await drawTs();

    const occ = await api("/api/occ_co2");
    const o = occ[0];
    $("#occ-co2").innerHTML = !o || !o.n
      ? `<div class="note warn"><span>⚠️</span><div><strong>조인 불가.</strong>
          ${esc(o?.note || "occupancy 데이터가 없습니다")}
          ${o?.vision_nodes?.length ? `<div class="mono" style="margin-top:6px">
            환경 ${o.env_nodes.map((n) => n.replace(/^node_/, "")).join(" ")}<br>
            비전 ${o.vision_nodes.map((n) => n.replace(/^node_/, "")).join(" ")}</div>` : ""}</div></div>`
      : `<div class="card">${occ.map((r) => `<div>${esc(r.scope)} — ${num(r.n)}쌍 ·
          Spearman ρ ${r.spearman?.toFixed(2)}</div>`).join("")}</div>`;
  },

  async diagnose() {
    const [regimes, tr] = await Promise.all([api("/api/regime"), api("/api/transition")]);

    $("#plane-chart").replaceChildren(planeChart(regimes));
    $("#regime-table").innerHTML = `
      <thead><tr><th>노드</th><th>레짐</th><th>확신도</th><th>체류</th><th>CO₂</th><th>VOC</th></tr></thead>
      <tbody>${regimes.map((n) => `<tr>
        <td>${esc((n.label || n.scope).replace(/^node_/, ""))}</td>
        <td style="text-align:right">${badge(n.regime)}</td>
        <td>${n.p_max == null ? "—" : n.p_max.toFixed(2)}</td>
        <td>${n.dwell_min == null ? "—" : `${num(n.dwell_min)}분${n.dwell_censored ? "+" : ""}`}</td>
        <td>${num(n.co2)}</td><td>${num(n.voc, 1)}</td></tr>`).join("")}</tbody>`;

    $("#transition-heat").innerHTML = heatmap(tr.regimes, tr.matrix)
      + `<p class="sub" style="margin-top:12px">유효 쌍 ${num(tr.valid_pairs)} ·
         gap 으로 제외 ${num(tr.gap_pairs)}${tr.win_start ? ` · ${kst(tr.win_start)} ~ ${kst(tr.win_end)}` : ""}</p>`;
    $("#dwell-chart").innerHTML = dwellBars(tr.dwell_median_min || {}, tr.dwell_n || {});

    const bands = await api("/api/band");
    $("#band-list").innerHTML = bands.length ? bands.map((b) => {
      const by = new Map((b.slots || []).map((s) => [s.min, s.regime]));
      let html = "";
      for (let m = 0; m < 1440; m += 5) {
        const r = by.get(m);
        html += `<i style="width:${100 / 288}%;background:${r ? REGIME_COLOR[r] : "transparent"}"
                  title="${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")} ${r ? REGIME_KO[r] : "데이터 없음"}"></i>`;
      }
      return `<div style="margin-bottom:16px">
        <div style="display:flex;justify-content:space-between;font-size:var(--t-caption1);margin-bottom:4px">
          <b>${esc((b.label || b.scope).replace(/^node_/, ""))}</b>
          <span class="muted">00 · 06 · 12 · 18 · 24시 (UTC)</span></div>
        <div class="band">${html}</div></div>`;
    }).join("") + `<div class="legend" style="margin-top:8px">${REGIMES.map((r) =>
      `<span style="--c:${REGIME_COLOR[r]}"><i></i>${REGIME_KO[r]}</span>`).join("")}</div>`
      : `<div class="muted">일간 배치를 아직 돌리지 않았습니다.</div>`;
  },

  async control() {
    const [acts, fcs] = await Promise.all([api("/api/action"), api("/api/forecast")]);

    const alerts = fcs.filter((f) => f.alert_co2 || f.alert_voc);
    $("#alerts").innerHTML = alerts.length ? alerts.map((f) => `
      <div class="note critical" style="margin-bottom:8px"><span>🔴</span><div>
        <strong>${esc(f.label || f.scope)}</strong> —
        ${f.alert_co2 ? `CO₂ ${num(f.co2_now)} → <b>${num(f.co2_pred)} ppm</b> (임계 1000)` : ""}
        ${f.alert_co2 && f.alert_voc ? " · " : ""}
        ${f.alert_voc ? `VOC ${num(f.voc_now, 1)} → <b>${num(f.voc_pred, 1)}</b> (임계 200)` : ""}
        <div class="muted" style="margin-top:4px">${f.horizon_min}분 뒤 예측. 지금 환기하면 넘지 않습니다.</div>
      </div></div>`).join("")
      : `<div class="note"><span>✅</span><div>30분 안에 임계를 넘을 것으로 예측되는 노드가 없습니다.</div></div>`;

    $("#action-cards").innerHTML = acts.map((a) => `<div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <b>${esc(a.label || a.scope)}</b>
        ${badge((a.devices || [])[0]?.regime)}
      </div>
      ${(a.devices || []).map((d) => `
        <div style="display:flex;justify-content:space-between;align-items:baseline;
                    margin-top:12px;padding-top:12px;border-top:1px solid var(--separator)">
          <span>${d.device === "fan" ? "환풍기" : "공기청정기"}</span>
          <b style="color:${d.state ? "var(--status-good)" : "var(--label-tertiary)"}">
            ${d.state ? "ON" : "OFF"}</b>
        </div>
        <div class="muted mono" style="font-size:var(--t-caption2)">${esc(d.rule)}${
          d.value == null ? "" : ` · 현재 ${num(d.value, 1)}`}${
          d.hold_until ? ` · ${kst(d.hold_until)}까지 유지` : ""}</div>`).join("")}
      </div>`).join("") || `<div class="muted">배치를 아직 돌리지 않았습니다.</div>`;

    $("#forecast-table").innerHTML = `
      <thead><tr><th>노드</th><th>CO₂ 지금</th><th>30분 뒤</th><th>10분 기울기</th>
        <th>VOC 지금</th><th>30분 뒤</th><th>경보</th></tr></thead>
      <tbody>${fcs.map((f) => `<tr>
        <td>${esc((f.label || f.scope).replace(/^node_/, ""))}</td>
        <td>${num(f.co2_now)}</td>
        <td${f.alert_co2 ? ' style="color:var(--status-critical);font-weight:600"' : ""}>${num(f.co2_pred)}</td>
        <td>${f.d_co2_10m == null ? "—" : (f.d_co2_10m > 0 ? "+" : "") + num(f.d_co2_10m)}</td>
        <td>${num(f.voc_now, 1)}</td>
        <td${f.alert_voc ? ' style="color:var(--status-critical);font-weight:600"' : ""}>${num(f.voc_pred, 1)}</td>
        <td>${f.alert_co2 || f.alert_voc ? "⚠️" : "—"}</td></tr>`).join("")}</tbody>`;
  },

  async admin() {
    const [h, qcs, models] = await Promise.all([api("/api/health"), api("/api/qc"), api("/api/models")]);

    $("#admin-tiles").innerHTML = [
      ["DB 상태", h.ok ? "정상" : "오류", esc(h.db || "")],
      ["마지막 수집", kst(h.last_reading), h.stale ? "⚠️ 15분 이상 끊김" : "정상"],
      ["마지막 배치", kst(h.last_run), `analysis ${num(h.analysis_rows)}행`],
      ["readings", num(h.readings), "누적 행"],
    ].map(([k, v, n]) => `<div class="card tile"><div class="k">${k}</div>
      <div class="v" style="font-size:var(--t-title3)">${v}</div><div class="n">${n}</div></div>`).join("");

    const rows = qcs.flatMap((q) => (q.days || []).map((d) => ({ node: q.label || q.scope, ...d })))
                    .sort((a, b) => (b.date > a.date ? 1 : -1));
    $("#qc-table").innerHTML = `
      <thead><tr><th>날짜</th><th>노드</th><th>행</th><th>CO₂ 유효</th><th>VOC 유효</th>
        <th>통과</th><th style="text-align:left">사유</th></tr></thead>
      <tbody>${rows.map((r) => `<tr>
        <td>${esc(r.date)}</td><td>${esc(String(r.node).replace(/^node_/, ""))}</td>
        <td>${num(r.rows)}</td><td>${num(r.valid_co2_pct, 1)}%</td><td>${num(r.valid_voc_pct, 1)}%</td>
        <td style="color:${r.passed ? "var(--status-good)" : "var(--status-critical)"}">${r.passed ? "통과" : "탈락"}</td>
        <td style="text-align:left;color:var(--label-secondary)">${esc(r.reason || "")}</td></tr>`).join("")
        || `<tr><td colspan="7" class="muted">배치를 아직 돌리지 않았습니다.</td></tr>`}</tbody>`;

    $("#model-cards").innerHTML = models.map((m) => {
      if (m.kind === "gmm") {
        return `<div class="card">
          <h3 style="margin:0 0 4px;font-size:var(--t-headline)">레짐 모델 (GMM)</h3>
          <p class="sub" style="margin:0 0 12px">${esc(m.model_ver)} · ${kst(m.trained_at)} 학습 ·
             표본 ${num(m.n_samples)} · k=${m.k} · BIC ${num(m.bic)}</p>
          <div class="scroll-x"><table>
            <thead><tr><th>성분</th><th>CO₂</th><th>VOC</th><th>비중</th><th>레짐</th></tr></thead>
            <tbody>${(m.clusters || []).map((c) => `<tr>
              <td>c${c.cluster}</td><td>${num(c.ppm)} ppm</td><td>${num(c.voc)}</td>
              <td>${pct(c.weight)}</td><td style="text-align:right">${badge(c.regime)}</td></tr>`).join("")}</tbody>
          </table></div>
          <p class="sub" style="margin-top:12px">
            k=6 으로 밀도를 잡고 중심의 분면으로 이름 붙여 4개 레짐으로 병합합니다.
            앵커 규칙과 ${pct(m.agreement_with_anchor_rule)} 일치 — 나머지는 GMM 이
            결합분포를 보고 경계 근처를 다르게 판정한 것입니다.</p>
        </div>`;
      }
      const s = m.scores || {};
      return `<div class="card">
        <h3 style="margin:0 0 4px;font-size:var(--t-headline)">예측 모델 (능형회귀)</h3>
        <p class="sub" style="margin:0 0 12px">${esc(m.model_ver)} · ${kst(m.trained_at)} 학습 ·
           ${m.horizon_min}분 앞 · 학습 ${num(m.n_train)} / 검증 ${num(m.n_test)} · ${esc(m.split || "")}</p>
        <table><thead><tr><th>타깃</th><th>MAE</th><th>R²</th><th>지속 기준선</th><th>개선</th></tr></thead>
          <tbody>${Object.entries(s).map(([k, v]) => `<tr>
            <td>${k.includes("co2") ? "CO₂" : "VOC"}</td>
            <td>${num(v.model?.mae, 1)}</td><td>${v.model?.r2?.toFixed(3)}</td>
            <td>${num(v.persistence?.mae, 1)}</td>
            <td style="color:${v.mae_gain_pct > 10 ? "var(--status-good)" : "var(--status-warning)"}">
              ${v.mae_gain_pct > 0 ? "+" : ""}${v.mae_gain_pct}%</td></tr>`).join("")}</tbody></table>
        <div class="note warn" style="margin-top:12px"><span>⚠️</span><div>
          R² 는 CO₂ 가 자기상관이 강해 높게 나옵니다. 실제로 볼 값은
          <strong>지속 기준선("30분 뒤에도 지금과 같다") 대비 개선폭</strong>이고,
          지금은 한 자릿수입니다. 선제 경보의 참고 지표로만 쓰세요.</div></div>
      </div>`;
    }).join("") || `<div class="muted">주간 배치를 아직 돌리지 않았습니다.</div>`;
  },
};

/* ============================ 시작 ============================ */
async function boot() {
  try {
    const h = await api("/api/health");
    const stale = h.stale;
    $("#health-dot").className = `dot ${!h.ok ? "error" : stale ? "warn" : "ok"}`;
    $("#health-text").textContent = !h.ok ? "DB 오류"
      : `마지막 수집 ${kst(h.last_reading)}${stale ? " · 끊김" : ""} · 배치 ${kst(h.last_run)}`;
  } catch (e) {
    $("#health-dot").className = "dot error";
    $("#health-text").textContent = "서버 응답 없음";
  }
  $$(".tab").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.panel)));
  showTab(["monitor", "diagnose", "control", "admin"].includes(location.hash.slice(1))
    ? location.hash.slice(1) : "monitor");
}
boot();
