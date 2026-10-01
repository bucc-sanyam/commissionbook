(() => {
  "use strict";
  const source = document.getElementById("dashboard-data");
  if (!source) return;
  const { chart, currency, totals } = JSON.parse(source.textContent);
  const svgNS = "http://www.w3.org/2000/svg";
  const money = (number, compact = false) => {
    const options = compact
      ? { notation: "compact", maximumFractionDigits: 1 }
      : { minimumFractionDigits: 2, maximumFractionDigits: 2 };
    return `${number < 0 ? "-" : ""}${currency}${new Intl.NumberFormat("en-IN", options).format(Math.abs(number))}`;
  };
  function svgElement(name, attrs = {}, text = null) {
    const element = document.createElementNS(svgNS, name);
    Object.entries(attrs).forEach(([key, value]) => element.setAttribute(key, String(value)));
    if (text !== null) element.textContent = text;
    return element;
  }
  function monthLabel(value, full = false) {
    if (!/^\d{4}-\d{2}$/.test(value)) return value;
    return new Intl.DateTimeFormat("en-GB", { month: "short", ...(full ? { year: "numeric" } : {}) })
      .format(new Date(`${value}-15T12:00:00`));
  }
  let metric = "commission";
  const chartBox = document.getElementById("earnings-chart");
  const tooltip = document.createElement("div");
  tooltip.className = "chart-tooltip";
  tooltip.hidden = true;
  tooltip.setAttribute("aria-hidden", "true");

  function drawEarnings() {
    if (!chart.months.length || !chartBox) return;
    const values = chart[metric].map(Number);
    const width = Math.max(chartBox.clientWidth, 270);
    const height = chartBox.clientHeight;
    const left = 57, right = 22, top = 28, bottom = 36;
    const plotWidth = width - left - right;
    const plotHeight = height - top - bottom;
    const max = Math.max(0, ...values);
    const min = Math.min(0, ...values);
    const span = max - min || 1;
    const upper = max + span * .15;
    const lower = min < 0 ? min - span * .1 : 0;
    const y = (v) => top + (upper - v) / (upper - lower) * plotHeight;
    const x = (index) => left + (index + .5) / values.length * plotWidth;
    const svg = svgElement("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": `${metric === "commission" ? "Commission" : "Client profit and loss"} by month` });
    svg.append(svgElement("title", {}, `${metric === "commission" ? "Commission" : "Client P&L"}, ${chart.months.length} months`));

    for (let index = 0; index <= 4; index++) {
      const value = lower + (upper - lower) * index / 4;
      const yy = y(value);
      svg.append(svgElement("line", { x1: left, y1: yy, x2: width - right, y2: yy, stroke: "#eaf0e3", "stroke-dasharray": "3 4", "stroke-width": 1 }));
      svg.append(svgElement("text", { x: left - 9, y: yy + 3, "text-anchor": "end", fill: "#647950", "font-size": 10, "font-family": "inherit" }, money(value, true)));
    }
    if (min < 0) svg.append(svgElement("line", { x1: left, y1: y(0), x2: width - right, y2: y(0), stroke: "#c5d4b6", "stroke-width": 1 }));
    const barWidth = Math.min(32, plotWidth / values.length * .55);
    const labelEvery = Math.max(1, Math.ceil(values.length / (width < 400 ? 4 : 8)));
    values.forEach((value, index) => {
      const xx = x(index);
      const yy = y(value);
      const bar = svgElement("rect", {
        x: xx - barWidth / 2, y: Math.min(yy, y(0)),
        width: barWidth, height: Math.max(Math.abs(y(0) - yy), 2),
        rx: Math.min(4, barWidth / 4),
        fill: value < 0 ? "#d49b86" : index === values.length - 1 ? "#3f7352" : "#b8d29b",
        tabindex: 0, role: "img", "aria-label": `${monthLabel(chart.months[index], true)}: ${money(value)}`,
      });
      bar.append(svgElement("title", {}, `${monthLabel(chart.months[index], true)}: ${money(value)}`));
      const showTip = () => {
        tooltip.textContent = `${monthLabel(chart.months[index], true)} · ${money(value)}`;
        tooltip.hidden = false;
        tooltip.style.left = `${Math.max(90, Math.min(width - 90, xx))}px`;
        tooltip.style.top = `${Math.max(28, Math.min(yy, y(0)) - 5)}px`;
      };
      bar.addEventListener("mouseenter", showTip);
      bar.addEventListener("focus", showTip);
      bar.addEventListener("mouseleave", () => { tooltip.hidden = true; });
      bar.addEventListener("blur", () => { tooltip.hidden = true; });
      svg.append(bar);
      if (index % labelEvery === 0) {
        svg.append(svgElement("text", { x: xx, y: height - 12, "text-anchor": "middle", fill: "#647950", "font-size": 10, "font-family": "inherit" }, monthLabel(chart.months[index], values.length > 12)));
      }
    });
    chartBox.replaceChildren(svg, tooltip);
  }
  document.querySelectorAll("[data-chart-metric]").forEach((button, index) => {
    if (!button.id) button.id = `chart-tab-${index}`;
    button.tabIndex = button.getAttribute("aria-selected") === "true" ? 0 : -1;
    button.addEventListener("click", () => {
      metric = button.dataset.chartMetric;
      document.querySelectorAll("[data-chart-metric]").forEach((tab) => {
        tab.setAttribute("aria-selected", String(tab === button));
        tab.tabIndex = tab === button ? 0 : -1;
      });
      chartBox.setAttribute("aria-labelledby", button.id);
      document.getElementById("chart-total").textContent = money(Number(totals[metric]));
      document.getElementById("chart-metric-label").textContent = metric === "commission" ? "commission earned" : "realised client P&L";
      document.getElementById("chart-legend-label").textContent = metric === "commission" ? "Monthly commission" : "Monthly realised P&L";
      drawEarnings();
    });
    button.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      const tabs = [...document.querySelectorAll("[data-chart-metric]")];
      const next = tabs[(index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length];
      next.focus();
      next.click();
    });
  });
  if (chart.months.length) new ResizeObserver(drawEarnings).observe(chartBox);

  const donut = document.querySelector("#client-donut svg");
  if (donut && chart.clients.length) {
    const colors = ["#4e7950", "#92b472", "#c1d3a5", "#d6b996", "#dce4d1"];
    const slices = chart.clients.map((name, index) => ({ name, value: Number(chart.client_commission[index]) }));
    const displayed = slices.slice(0, 4);
    if (slices.length > 4) displayed.push({ name: "Other clients", value: slices.slice(4).reduce((sum, slice) => sum + slice.value, 0) });
    const total = displayed.reduce((sum, slice) => sum + slice.value, 0);
    if (total <= 0) return;
    const circumference = 2 * Math.PI * 66;
    let offset = 0;
    const legend = document.getElementById("donut-legend");
    displayed.forEach((slice, index) => {
      const length = slice.value / total * circumference;
      const segment = svgElement("circle", {
        cx: 90, cy: 90, r: 66, fill: "none", stroke: colors[index], "stroke-width": 22,
        "stroke-dasharray": `${Math.max(.1, length - (displayed.length > 1 ? 5 : 0))} ${circumference}`,
        "stroke-dashoffset": -offset, transform: "rotate(-90 90 90)",
      });
      segment.append(svgElement("title", {}, `${slice.name}: ${money(slice.value)} (${Math.round(slice.value / total * 100)}%)`));
      donut.append(segment);
      offset += length;
      const item = document.createElement("div");
      item.className = "donut-legend-item";
      const dot = document.createElement("span");
      dot.className = "color-dot";
      dot.style.background = colors[index];
      const name = document.createElement("span");
      name.textContent = slice.name;
      const amount = document.createElement("strong");
      amount.textContent = money(slice.value);
      item.append(dot, name, amount);
      legend.append(item);
    });
  }
})();
