// Stats charts (BUILD_PLAN §1.5). Data comes from the JSON block the page renders; no
// inline script, so the CSP stays script-src 'self'. Colours follow the palette.
(function () {
  "use strict";

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function alpha(color, a) {
    // "#rrggbb" -> rgba(); anything else is returned as is.
    if (!/^#[0-9a-f]{6}$/i.test(color)) return color;
    const n = parseInt(color.slice(1), 16);
    return "rgba(" + (n >> 16) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + a + ")";
  }

  function short(iso) {
    const [, m, d] = iso.split("-");
    return Number(m) + "/" + Number(d);
  }

  function aligned(labels, rows, key) {
    const byDay = new Map(rows.map((r) => [r.d, r[key]]));
    return labels.map((d) => (byDay.has(d) ? byDay.get(d) : null));
  }

  function draw() {
    const node = document.getElementById("stats-data");
    if (!node || typeof Chart === "undefined") return;
    const data = JSON.parse(node.textContent);
    const text = css("--color-text") || "#e8edf2";
    const muted = css("--color-muted") || "#93a1b0";
    const line = css("--color-line") || "#2c353f";
    const accent = css("--color-accent") || "#f2b544";
    const over = css("--color-over") || "#2fbf71";
    const under = css("--color-under") || "#4c8dff";
    Chart.defaults.color = muted;
    Chart.defaults.borderColor = line;
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    Chart.defaults.animation = false;

    const scaleX = { ticks: { callback: function (v) { return short(this.getLabelForValue(v)); },
                              maxRotation: 0, autoSkipPadding: 12 }, grid: { display: false } };

    const weight = document.getElementById("weight-chart");
    if (weight) {
      const labels = data.labels;
      const scale = aligned(labels, data.weigh_ins.filter((w) => w.s !== "manual"), "v");
      const manual = aligned(labels, data.weigh_ins.filter((w) => w.s === "manual"), "v");
      const rows = data.trend.concat(data.projection);
      const trendY = aligned(labels, data.trend, "y");
      const projY = aligned(labels, data.trend.slice(-1).concat(data.projection), "y");
      const lo = aligned(labels, rows, "lo");
      const hi = aligned(labels, rows, "hi");
      new Chart(weight, {
        type: "line",
        data: {
          labels: labels,
          datasets: [
            { label: "+1σ", data: hi, borderWidth: 0, pointRadius: 0, fill: "+1",
              backgroundColor: alpha(accent, 0.14), spanGaps: true },
            { label: "-1σ", data: lo, borderWidth: 0, pointRadius: 0, fill: false, spanGaps: true },
            { label: "Trend", data: trendY, borderColor: accent, borderWidth: 2, pointRadius: 0,
              pointStyle: "line", spanGaps: true },
            { label: "Projection", data: projY, borderColor: accent, borderWidth: 2,
              borderDash: [6, 4], pointRadius: 0, pointStyle: "line", spanGaps: true },
            { label: "Scale", data: scale, showLine: false, pointRadius: 4,
              pointBackgroundColor: text, pointBorderColor: text },
            { label: "Manual", data: manual, showLine: false, pointRadius: 4,
              pointBackgroundColor: "transparent", pointBorderColor: text, pointBorderWidth: 2 },
          ],
        },
        options: {
          maintainAspectRatio: false,
          interaction: { mode: "index", intersect: false },
          plugins: {
            legend: { labels: { filter: (item) => !item.text.includes("σ"), usePointStyle: true, boxWidth: 8 } },
            tooltip: { filter: (item) => item.raw !== null && !item.dataset.label.includes("σ") },
          },
          scales: { x: scaleX, y: { title: { display: true, text: data.unit } } },
        },
      });
    }

    document.querySelectorAll("canvas.metric-chart").forEach((canvas) => {
      const m = data.metrics[Number(canvas.dataset.index)];
      if (!m) return;
      new Chart(canvas, {
        data: {
          labels: m.labels,
          datasets: [
            { type: "bar", label: m.label, data: m.values, backgroundColor: alpha(under, 0.55),
              borderRadius: 3 },
            { type: "line", label: "7-day average", data: m.avg7, borderColor: over,
              borderWidth: 2, pointRadius: 0, spanGaps: true },
          ],
        },
        options: {
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: { x: scaleX, y: { beginAtZero: true,
                                     ticks: { precision: m.metric === "workouts" ? 0 : undefined } } },
        },
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", draw);
  } else {
    draw();
  }
})();
