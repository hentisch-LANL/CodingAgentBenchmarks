// Renders a horizontal bar chart of agent pass rates with 95% CI error bars.
// Inline SVG only; no external libraries. Styling stays deliberately plain.
(function () {
    const SVG_NS = "http://www.w3.org/2000/svg";

    let agents = null;

    function loadData() {
        if (agents !== null) return agents;
        const script = document.getElementById("agent-data");
        if (!script) {
            agents = [];
            return agents;
        }
        try {
            agents = JSON.parse(script.textContent) || [];
        } catch (err) {
            agents = [];
        }
        return agents;
    }

    function el(name, attrs, text) {
        const node = document.createElementNS(SVG_NS, name);
        if (attrs) {
            for (const [k, v] of Object.entries(attrs)) {
                node.setAttribute(k, v);
            }
        }
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function orderAgents(data, order) {
        const copy = data.slice();
        if (order === "name") {
            copy.sort((a, b) => a.name.localeCompare(b.name));
        } else {
            copy.sort((a, b) => b.pass_rate - a.pass_rate);
        }
        return copy;
    }

    // Linear interpolation in RGB space from red (worst) to green (best).
    // `t` is 0 (least successful) .. 1 (most successful).
    const RED = [192, 0, 0]; // matches --fail
    const GREEN = [7, 131, 0]; // matches --pass
    function heatColor(t) {
        const clamped = Math.max(0, Math.min(1, t));
        const mix = (a, b) => Math.round(a + (b - a) * clamped);
        const r = mix(RED[0], GREEN[0]);
        const g = mix(RED[1], GREEN[1]);
        const b = mix(RED[2], GREEN[2]);
        return `rgb(${r}, ${g}, ${b})`;
    }

    function render() {
        const container = document.getElementById("plot-container");
        if (!container) return;
        const data = loadData();

        container.textContent = "";
        if (data.length === 0) {
            container.appendChild(
                Object.assign(document.createElement("p"), {
                    className: "plot-empty",
                    textContent: "No agent data to plot.",
                })
            );
            return;
        }

        const orderSelect = document.getElementById("plot-order");
        const order = orderSelect ? orderSelect.value : "pass_rate";
        const rows = orderAgents(data, order);

        // Heatmap range: worst point estimate -> red, best -> green.
        const passRates = rows.map((a) => a.pass_rate);
        const minRate = Math.min(...passRates);
        const maxRate = Math.max(...passRates);
        const rateSpan = maxRate - minRate;
        const colorFor = (value) => heatColor(rateSpan === 0 ? 1 : (value - minRate) / rateSpan);

        // Fixed layout geometry (label gutter width is measured below).
        const topPad = 28;
        const bottomPad = 60; // room for axis ticks, tick labels, and axis title
        const rowHeight = 40; // room for value label above each logo
        const capHeight = 10; // vertical size of the CI end caps
        const markerRadius = 3.5; // fallback dot radius when an agent has no logos
        const valueLabelGap = 6; // gap between the logo top and its value label above it

        // Logo geometry (logos now mark the point estimates).
        const logoSize = 16;
        const logoGap = 3;
        const gutterStart = 4;
        const labelTextGap = 6; // (retained for reference; text starts at gutterStart)
        const gutterEndPad = 20; // breathing room between longest label and the plot area

        // Vertical placement within each row band. The mark (logo + CI line)
        // sits low in the band so that the majority of the vertical whitespace
        // separates a row's value label from the row ABOVE it, not below.
        // headroom (large) : label : gap : logo : floor (small).
        const rowFloor = 4; // small gap beneath the logo

        // Widest logo cluster (e.g. hybrid = 2 logos) centered on a point can
        // overflow the axis at 0%/100%; pad by half a max cluster each side.
        const maxLogoCount = rows.reduce(
            (max, agent) => Math.max(max, Array.isArray(agent.logos) ? agent.logos.length : 0),
            0
        );
        const maxClusterWidth = maxLogoCount
            ? maxLogoCount * logoSize + (maxLogoCount - 1) * logoGap
            : markerRadius * 2;
        const edgeOverhang = Math.ceil(maxClusterWidth / 2) + 2;
        const rightPad = edgeOverhang; // room for a mark/CI cap sitting at 100%
        // Inner horizontal padding so marks/caps at 0% and 100% aren't clipped.
        const plotPadX = edgeOverhang;
        const axisWidth = 640;
        const plotWidth = axisWidth + plotPadX * 2;
        const height = topPad + rows.length * rowHeight + bottomPad;

        const svg = el("svg", {
            role: "img",
            "aria-label": "Agent pass rates with 95% confidence intervals",
            class: "plot-svg",
        });
        // Attach early so text can be measured with getComputedTextLength().
        container.appendChild(svg);

        // ---- Pass 0: with logos now marking the point estimates, the left
        // label is just the agent text, all rows starting at the same x. ----
        const textStartX = gutterStart;

        // ---- Pass 1: draw per-row text labels and measure widths. ----
        const rowLabels = rows.map((agent, i) => {
            // Mark sits low in the row band: most whitespace lands above the
            // value label (separating it from the row above), little below.
            const cy = topPad + i * rowHeight + rowHeight - rowFloor - logoSize / 2;
            const label = agent.label
                ? agent.label
                : (agent.models && agent.models !== "n/a"
                    ? `${agent.harness} with ${agent.models}`
                    : agent.harness);

            const textX = textStartX;
            const labelText = el(
                "text",
                { x: textX, y: cy + 4, class: "plot-row-label", "text-anchor": "start" },
                label
            );
            svg.appendChild(labelText);

            let textLen = 0;
            try {
                textLen = labelText.getComputedTextLength();
            } catch (err) {
                textLen = label.length * 6.2; // fallback estimate
            }
            return { cy, textX, textLen };
        });

        // Widest label determines the gutter width so nothing overlaps the plot.
        const maxLabelRight = rowLabels.reduce(
            (max, r) => Math.max(max, r.textX + r.textLen),
            gutterStart
        );
        const labelWidth = Math.ceil(maxLabelRight + gutterEndPad);

        const width = labelWidth + plotWidth + rightPad;
        svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
        svg.setAttribute("width", width);
        svg.setAttribute("height", height);

        // Map a percentage to an x coordinate inside the padded plot area.
        const xFor = (pct) => labelWidth + plotPadX + (pct / 100) * axisWidth;

        const axisY = topPad + rows.length * rowHeight; // baseline for the x-axis

        // Gridlines at 0/25/50/75/100%.
        for (let pct = 0; pct <= 100; pct += 25) {
            const x = xFor(pct);
            svg.appendChild(
                el("line", {
                    x1: x,
                    y1: topPad,
                    x2: x,
                    y2: axisY,
                    class: "plot-gridline",
                })
            );
        }

        // X-axis: baseline, tick marks, tick labels, and an axis title.
        svg.appendChild(
            el("line", {
                x1: xFor(0),
                y1: axisY,
                x2: xFor(100),
                y2: axisY,
                class: "plot-axis-line",
            })
        );
        for (let pct = 0; pct <= 100; pct += 25) {
            const x = xFor(pct);
            svg.appendChild(
                el("line", { x1: x, y1: axisY, x2: x, y2: axisY + 5, class: "plot-axis-tick" })
            );
            svg.appendChild(
                el(
                    "text",
                    { x: x, y: axisY + 18, class: "plot-axis-label", "text-anchor": "middle" },
                    `${pct}%`
                )
            );
        }
        // Axis title, centered under the tick labels.
        svg.appendChild(
            el(
                "text",
                {
                    x: xFor(50),
                    y: axisY + 40,
                    class: "plot-axis-title",
                    "text-anchor": "middle",
                },
                "Pass Rate (% of benchmarks passed)"
            )
        );

        // ---- Pass 2: draw CI, points, logos, and value labels per row. ----
        rows.forEach((agent, i) => {
            const cy = rowLabels[i].cy;
            const value = agent.pass_rate;
            const color = colorFor(value);
            const cx = xFor(value);

            const logos = Array.isArray(agent.logos) ? agent.logos : [];

            // Lay out the logo cluster so its combined *visual* span is centered
            // on the point estimate. Each logo's `cx` is the alpha-weighted
            // centroid fraction; ink lands where we want it, not the bbox.
            const placed = [];
            if (logos.length) {
                const clusterWidth = logos.length * logoSize + (logos.length - 1) * logoGap;
                // First pass positions boxes left-to-right from an origin of 0,
                // then we shift so the visual centroid of the cluster hits cx.
                let boxX = 0;
                let inkWeightX = 0;
                logos.forEach((logo) => {
                    const centroid = boxX + (logo.cx != null ? logo.cx : 0.5) * logoSize;
                    inkWeightX += centroid;
                    placed.push({ logo, boxX });
                    boxX += logoSize + logoGap;
                });
                const clusterInkCenter = inkWeightX / logos.length;
                const shift = cx - clusterInkCenter;
                placed.forEach((p) => (p.boxX += shift));
                // Record cluster bounds for line interruption below.
                placed._left = shift; // leftmost box origin
                placed._right = shift + clusterWidth; // rightmost box end
            }

            // Confidence interval line, interrupted where logos sit so the line
            // is never drawn *under* a logo — the logo takes over. The cut edges
            // follow each logo's visible (alpha) extent, not its bounding box.
            const hasCI = agent.ci_low !== null && agent.ci_high !== null;
            if (hasCI) {
                const xLow = xFor(agent.ci_low);
                const xHigh = xFor(agent.ci_high);
                const capTop = cy - capHeight / 2;
                const capBottom = cy + capHeight / 2;

                // Compute the visible span the logo cluster occupies on the line.
                let cutLeft = null;
                let cutRight = null;
                if (placed.length) {
                    const first = placed[0];
                    const last = placed[placed.length - 1];
                    const firstInsetL = first.logo.inset_left != null ? first.logo.inset_left : 0;
                    const lastInsetR = last.logo.inset_right != null ? last.logo.inset_right : 0;
                    cutLeft = first.boxX + firstInsetL * logoSize;
                    cutRight = last.boxX + logoSize - lastInsetR * logoSize;
                }

                if (cutLeft === null) {
                    // No logos: single continuous line.
                    svg.appendChild(el("line", { x1: xLow, y1: cy, x2: xHigh, y2: cy, class: "plot-ci-line", stroke: color }));
                } else {
                    // Left segment: from xLow up to the logo's visible left edge.
                    if (cutLeft > xLow) {
                        svg.appendChild(el("line", { x1: xLow, y1: cy, x2: Math.min(cutLeft, xHigh), y2: cy, class: "plot-ci-line", stroke: color }));
                    }
                    // Right segment: from the logo's visible right edge to xHigh.
                    if (cutRight < xHigh) {
                        svg.appendChild(el("line", { x1: Math.max(cutRight, xLow), y1: cy, x2: xHigh, y2: cy, class: "plot-ci-line", stroke: color }));
                    }
                }
                svg.appendChild(el("line", { x1: xLow, y1: capTop, x2: xLow, y2: capBottom, class: "plot-ci-cap", stroke: color }));
                svg.appendChild(el("line", { x1: xHigh, y1: capTop, x2: xHigh, y2: capBottom, class: "plot-ci-cap", stroke: color }));
            }

            // No logos: fall back to a plain colored dot so the point still shows.
            if (!placed.length) {
                svg.appendChild(
                    el("circle", { cx: cx, cy: cy, r: markerRadius, class: "plot-point", fill: color })
                );
            }

            // Logos mark the point on top of the line, centered by visual weight.
            placed.forEach((p) => {
                const img = el("image", {
                    x: p.boxX,
                    y: cy - logoSize / 2,
                    width: logoSize,
                    height: logoSize,
                    class: "plot-point-logo",
                    preserveAspectRatio: "xMidYMid meet",
                });
                img.setAttribute("href", p.logo.src);
                img.setAttributeNS("http://www.w3.org/1999/xlink", "xlink:href", p.logo.src);
                svg.appendChild(img);
            });

            // Value label above the mark. The gap above the label (separating it
            // from the row above) is intentionally larger than the gap below it.
            svg.appendChild(
                el(
                    "text",
                    {
                        x: cx,
                        y: cy - logoSize / 2 - valueLabelGap,
                        class: "plot-value-label",
                        "text-anchor": "middle",
                        fill: color,
                    },
                    `${Math.round(value)}%`
                )
            );
        });
    }

    // Re-render when the order control changes.
    document.addEventListener("DOMContentLoaded", () => {
        const orderSelect = document.getElementById("plot-order");
        if (orderSelect) orderSelect.addEventListener("change", render);
    });

    // Exposed so viz.js can trigger a render when the plot view is shown.
    window.renderPlot = render;

    // Exposed so recompute.js can hand the plot a freshly recomputed dataset
    // (pass_rate / ci_low / ci_high reflecting only the selected task groups).
    window.setPlotData = function (arr) {
        agents = Array.isArray(arr) ? arr : [];
    };

    // Exposed so recompute.js can read the originally embedded dataset once.
    window.getPlotData = loadData;
})();
