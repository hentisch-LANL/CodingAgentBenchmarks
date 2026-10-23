// recompute.js - Recompute every leaderboard statistic from the currently
// selected task groups, entirely in the browser.
//
// The "Task groups" panel (#group-selector) lets the viewer include/exclude
// whole task groups. When the selection changes we recompute each agent's
// pass rate (with a 95% Wilson score interval), completeness, and average
// cost / tokens / tok-s / walltime (with 95% normal-approximation intervals)
// using ONLY the attempts that belong to the selected groups, then rewrite the
// table cells, the embedded plot dataset, and re-apply the current sort.
//
// The server (site/build.py) renders the initial page with all groups selected
// and uses the identical formulas, so the JS-disabled view and the default JS
// view agree. This deliberately avoids bootstrap resampling: a binomial
// proportion has a closed-form interval, which is both correct near 0%/100%
// and cheap enough to run live in the browser.
(function () {
    "use strict";

    const Z95 = 1.959963984540054;

    // Decimal-safe round-half-up. Mirrors _dround in site/build.py so a mean
    // computed here matches the server's despite last-ULP summation-order
    // differences (which can otherwise flip a half-cent display tie).
    function dround(value, ndigits) {
        if (value === null || value === undefined) return value;
        const factor = Math.pow(10, ndigits == null ? 6 : ndigits);
        if (value >= 0) return Math.floor(value * factor + 0.5) / factor;
        return -Math.floor(-value * factor + 0.5) / factor;
    }

    // ---- Statistics (must match site/build.py) --------------------------

    function wilsonProportionCI(passed, total) {
        if (total <= 0) return null;
        const p = passed / total;
        const z = Z95;
        const z2 = z * z;
        const denom = 1 + z2 / total;
        const center = (p + z2 / (2 * total)) / denom;
        const half =
            (z / denom) *
            Math.sqrt((p * (1 - p)) / total + z2 / (4 * total * total));
        return [Math.max(0, center - half), Math.min(1, center + half)];
    }

    function mean(samples) {
        if (!samples.length) return null;
        let s = 0;
        for (const x of samples) s += x;
        return dround(s / samples.length, 6);
    }

    function meanCI(samples) {
        const n = samples.length;
        if (n === 0) return null;
        const m = mean(samples);
        if (n === 1) return [m, m];
        let ss = 0;
        for (const x of samples) ss += (x - m) * (x - m);
        const variance = ss / (n - 1);
        const half = Z95 * Math.sqrt(variance / n);
        return [m - half, m + half];
    }

    // ---- Formatting (must match site/build.py) --------------------------

    function formatCost(cost) {
        if (cost === null || cost === undefined) return "n/a";
        // toFixed matches Python's f"{v:.2f}" (round-half-to-even on the true
        // double) far more reliably than toLocaleString; we add thousands
        // separators manually to mirror the server's "," grouping.
        const abs = Math.abs(cost).toFixed(2);
        const withCommas = addThousands(abs);
        return cost < 0 ? `-$${withCommas}` : `$${withCommas}`;
    }

    // Add comma thousands separators to a "123456.78" style string, matching
    // Python's f"{v:,.2f}" grouping.
    function addThousands(numStr) {
        const neg = numStr.startsWith("-");
        const body = neg ? numStr.slice(1) : numStr;
        const dot = body.indexOf(".");
        const intPart = dot === -1 ? body : body.slice(0, dot);
        const fracPart = dot === -1 ? "" : body.slice(dot);
        const grouped = intPart.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
        return (neg ? "-" : "") + grouped + fracPart;
    }

    function formatDuration(seconds) {
        if (seconds === null || seconds === undefined) return "n/a";
        const total = Math.max(0, Math.round(seconds));
        const h = Math.floor(total / 3600);
        const m = Math.floor((total % 3600) / 60);
        const s = total % 60;
        const pad = (n) => String(n).padStart(2, "0");
        return `${h}:${pad(m)}:${pad(s)}`;
    }

    function formatTokens(t) {
        if (t === null || t === undefined) return "n/a";
        if (t >= 1000) return `${(t / 1000).toFixed(1)}k`;
        return `${t.toFixed(0)}`;
    }

    function formatTps(tps) {
        if (tps === null || tps === undefined) return "n/a";
        return tps.toFixed(1);
    }

    function formatPercent(v) {
        return `${v.toFixed(0)}%`;
    }

    function formatPercentageCI(interval) {
        if (!interval) return "";
        return `[${interval[0].toFixed(0)}%, ${interval[1].toFixed(0)}%]`;
    }

    function formatCIGeneric(interval, fmt) {
        if (!interval) return "";
        return `[${fmt(interval[0])}, ${fmt(interval[1])}]`;
    }

    // ---- DOM helpers ----------------------------------------------------

    // Rebuild a "stat" cell: a value with an optional CI shown on hover, exactly
    // like build.py's _stat_span. Returns the innerHTML string.
    function statSpan(valueText, tipText) {
        const value = escapeHtml(valueText);
        if (tipText) {
            return `<span class="stat" data-tip="${escapeHtml(tipText)}">${value}</span>`;
        }
        return `<span class="stat">${value}</span>`;
    }

    function escapeHtml(str) {
        return String(str)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }

    function statusClassForRate(rate, ran) {
        if (!ran) return "status-muted";
        if (rate === 100) return "status-pass";
        if (rate === 0) return "status-fail";
        return "status-muted";
    }

    function setNumericSortValue(cell, value) {
        cell.setAttribute("data-sort-value", value === null ? "" : value.toFixed(6));
    }

    // ---- Main recompute -------------------------------------------------

    document.addEventListener("DOMContentLoaded", () => {
        const selector = document.getElementById("group-selector");
        const table = document.getElementById("leaderboard-table");
        if (!selector || !table) return;

        const checkboxes = Array.from(
            selector.querySelectorAll("input.group-checkbox")
        );
        if (checkboxes.length === 0) return;

        // Original, full dataset (one entry per agent) as embedded by the server.
        const baseData = (window.getPlotData ? window.getPlotData() : []).map((a) => a);
        const byAgent = new Map();
        baseData.forEach((a) => byAgent.set(a.name, a));

        const rows = Array.from(table.querySelectorAll("tbody tr.agent-row"));

        function selectedGroups() {
            const set = new Set();
            checkboxes.forEach((cb) => {
                if (cb.checked) set.add(cb.value);
            });
            return set;
        }

        // Aggregate an agent's per-group breakdown over the selected groups.
        function aggregateAgent(agent, selected) {
            let attempts = 0;
            let passed = 0;
            let ranCount = 0;
            const costs = [];
            const durations = [];
            const tokens = [];
            const tps = [];
            const groups = agent.groups || {};
            Object.keys(groups).forEach((name) => {
                if (!selected.has(name)) return;
                const g = groups[name];
                attempts += g.attempt_count || 0;
                passed += g.passed_count || 0;
                ranCount += g.ran_count || 0;
                if (g.costs) costs.push(...g.costs);
                if (g.durations) durations.push(...g.durations);
                if (g.tokens) tokens.push(...g.tokens);
                if (g.tps) tps.push(...g.tps);
            });
            const passRate = attempts ? (passed / attempts) * 100 : 0;
            const propCI = wilsonProportionCI(passed, attempts);
            return {
                attempts,
                passed,
                ranCount,
                passRate,
                passRateCI: propCI ? [propCI[0] * 100, propCI[1] * 100] : null,
                avgCost: mean(costs),
                avgTokens: mean(tokens),
                avgTps: mean(tps),
                avgWalltime: mean(durations),
                costCI: meanCI(costs),
                tokensCI: meanCI(tokens),
                tpsCI: meanCI(tps),
                walltimeCI: meanCI(durations),
            };
        }

        // Recompute a group summary cell (pass rate over selected group only).
        function groupCellRate(agent, groupName) {
            const g = (agent.groups || {})[groupName];
            if (!g || !g.attempt_count) return { rate: 0, ran: false };
            return {
                rate: (g.passed_count / g.attempt_count) * 100,
                ran: g.ran_count > 0,
            };
        }

        function recompute() {
            const selected = selectedGroups();

            // Dim the header of any deselected group column to match its cells.
            table.querySelectorAll("th.group-header").forEach((th) => {
                const groupName = th.dataset.groupName;
                th.classList.toggle("group-deselected", !selected.has(groupName));
            });

            // First pass: compute stats and the completeness denominator
            // (max ran-count across agents over the selected groups only).
            const stats = new Map();
            let maxRan = 0;
            rows.forEach((row) => {
                const name = row.dataset.agent;
                const agent = byAgent.get(name);
                if (!agent) return;
                const s = aggregateAgent(agent, selected);
                stats.set(name, s);
                if (s.ranCount > maxRan) maxRan = s.ranCount;
            });

            const plotData = [];

            rows.forEach((row) => {
                const name = row.dataset.agent;
                const agent = byAgent.get(name);
                const s = stats.get(name);
                if (!agent || !s) return;

                const completeness = maxRan ? (s.ranCount / maxRan) * 100 : 0;

                // Pass rate cell.
                const passCell = row.querySelector('td[data-col="pass_rate"]');
                if (passCell) {
                    passCell.innerHTML = statSpan(
                        formatPercent(s.passRate),
                        formatPercentageCI(s.passRateCI)
                    );
                    setNumericSortValue(passCell, s.passRate);
                }

                // Continuous columns.
                const tokensCell = row.querySelector('td[data-col="tokens"]');
                if (tokensCell) {
                    tokensCell.innerHTML = statSpan(
                        formatTokens(s.avgTokens),
                        formatCIGeneric(s.tokensCI, formatTokens)
                    );
                    setNumericSortValue(tokensCell, s.avgTokens);
                }

                const tpsCell = row.querySelector('td[data-col="tps"]');
                if (tpsCell) {
                    tpsCell.innerHTML = statSpan(
                        formatTps(s.avgTps),
                        formatCIGeneric(s.tpsCI, formatTps)
                    );
                    setNumericSortValue(tpsCell, s.avgTps);
                }

                const completenessCell = row.querySelector('td[data-col="completeness"]');
                if (completenessCell) {
                    completenessCell.textContent = formatPercent(completeness);
                    setNumericSortValue(completenessCell, completeness);
                }

                const costCell = row.querySelector('td[data-col="cost"]');
                if (costCell) {
                    costCell.innerHTML = statSpan(
                        formatCost(s.avgCost),
                        formatCIGeneric(s.costCI, formatCost)
                    );
                    setNumericSortValue(costCell, s.avgCost);
                }

                const walltimeCell = row.querySelector('td[data-col="walltime"]');
                if (walltimeCell) {
                    walltimeCell.innerHTML = statSpan(
                        formatDuration(s.avgWalltime),
                        formatCIGeneric(s.walltimeCI, formatDuration)
                    );
                    setNumericSortValue(walltimeCell, s.avgWalltime);
                }

                // Per-group summary cells: hide deselected groups, refresh rate.
                row.querySelectorAll('td[data-col="group"]').forEach((cell) => {
                    const groupName = cell.dataset.groupName;
                    const isSelected = selected.has(groupName);
                    cell.classList.toggle("group-deselected", !isSelected);
                    const { rate, ran } = groupCellRate(agent, groupName);
                    cell.textContent = ran ? formatPercent(rate) : "-";
                    cell.setAttribute(
                        "data-group-summary",
                        ran ? formatPercent(rate) : "-"
                    );
                    cell.classList.remove("status-pass", "status-fail", "status-muted");
                    cell.classList.add(statusClassForRate(rate, ran));
                });

                // Row-level data attributes used by filters/sort.
                row.dataset.passRate = s.passRate.toFixed(6);
                row.dataset.completeness = completeness.toFixed(6);

                // Build the plot dataset entry (mutated pass rate + CI).
                plotData.push(
                    Object.assign({}, agent, {
                        pass_rate: Math.round(s.passRate * 10000) / 10000,
                        ci_low: s.passRateCI
                            ? Math.round(s.passRateCI[0] * 10000) / 10000
                            : null,
                        ci_high: s.passRateCI
                            ? Math.round(s.passRateCI[1] * 10000) / 10000
                            : null,
                        completeness: Math.round(completeness * 10000) / 10000,
                    })
                );
            });

            // Hand the plot its recomputed dataset and redraw if visible.
            if (window.setPlotData) window.setPlotData(plotData);
            const plotView = document.getElementById("view-plot");
            if (window.renderPlot && plotView && !plotView.classList.contains("hidden")) {
                window.renderPlot();
            }

            // Re-apply the currently active sort so ordering reflects new values.
            if (window.reapplySort) window.reapplySort();
        }

        checkboxes.forEach((cb) => cb.addEventListener("change", recompute));

        const selectAll = document.getElementById("group-select-all");
        const selectNone = document.getElementById("group-select-none");
        if (selectAll) {
            selectAll.addEventListener("click", () => {
                checkboxes.forEach((cb) => (cb.checked = true));
                recompute();
            });
        }
        if (selectNone) {
            selectNone.addEventListener("click", () => {
                checkboxes.forEach((cb) => (cb.checked = false));
                recompute();
            });
        }

        // No initial recompute needed: the server already rendered the
        // all-groups-selected state with identical formulas. recompute() only
        // runs on user interaction.
    });
})();
