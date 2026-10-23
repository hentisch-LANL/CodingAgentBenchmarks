document.addEventListener("DOMContentLoaded", () => {
    const table = document.querySelector("table");
    if (!table) return;

    const headers = table.querySelectorAll("thead th[data-sort-type]");
    const groupHeaders = table.querySelectorAll("thead th.group-header");
    const taskHeaders = table.querySelectorAll("thead th.task-header");
    const allHeaders = [...headers, ...groupHeaders, ...taskHeaders];
    if (allHeaders.length === 0) return;

    const getCellValue = (cell, sortType) => {
        const raw = cell.getAttribute("data-sort-value");
        if (raw === null) {
            return cell.textContent.trim();
        }
        if (sortType === "number") {
            return raw === "" ? -Infinity : parseFloat(raw);
        }
        if (sortType === "interval") {
            const [low, high] = raw.split("|").map(parseFloat);
            return isNaN(low) ? -Infinity : low;
        }
        return raw;
    };

    const compare = (a, b, sortType) => {
        const av = getCellValue(a, sortType);
        const bv = getCellValue(b, sortType);
        if (av === bv) return 0;
        if (av < bv) return -1;
        return 1;
    };

    const compareStatus = (textA, textB) => {
        const valA = textA === "Pass" ? 3 : textA === "Fail" ? 2 : textA === "-" ? 0 : 1;
        const valB = textB === "Pass" ? 3 : textB === "Fail" ? 2 : textB === "-" ? 0 : 1;
        if (valA === valB) return 0;
        return valA < valB ? -1 : 1;
    };

    const updateSortIndicators = (clickedHeader, direction) => {
        allHeaders.forEach((h) => {
            const btn = h.querySelector(".sort-button, .group-sort-btn, .task-sort-btn");
            if (!btn) return;
            if (h === clickedHeader) {
                const sortValue = direction === 1 ? "ascending" : "descending";
                h.setAttribute("aria-sort", sortValue);
                btn.setAttribute("data-sort-direction", sortValue);
                if (btn.dataset.sortLabel) {
                    btn.setAttribute("aria-label", `${btn.dataset.sortLabel}: ${sortValue}`);
                }
            } else {
                h.setAttribute("aria-sort", "none");
                btn.setAttribute("data-sort-direction", "none");
                if (btn.dataset.sortLabel) {
                    btn.setAttribute("aria-label", btn.dataset.sortLabel || "Sort column");
                }
            }
        });
    };

    // Remember the active sort so recompute.js can re-apply it after the cell
    // values change (e.g. when task groups are toggled). Each sort registers a
    // closure that re-sorts the current rows in the active direction.
    let activeSort = null;
    window.reapplySort = () => {
        if (activeSort) activeSort();
    };

    headers.forEach((header) => {
        const btn = header.querySelector(".sort-button");
        if (!btn) return;

        const sortType = header.getAttribute("data-sort-type");
        let direction = -1;
        const defaultSort = header.getAttribute("data-sort-default");
        if (defaultSort === "ascending") {
            direction = 1;
            updateSortIndicators(header, 1);
        } else if (defaultSort === "descending") {
            direction = -1;
            updateSortIndicators(header, -1);
        } else {
            btn.setAttribute("data-sort-direction", "none");
        }

        btn.addEventListener("click", () => {
            direction *= -1;
            const doSort = () => {
                const tbody = table.querySelector("tbody");
                if (!tbody) return;
                const rows = Array.from(tbody.querySelectorAll("tr"));
                const colIndex = Array.from(header.parentNode.children).indexOf(header);
                rows.sort((rowA, rowB) => {
                    const cellA = rowA.children[colIndex];
                    const cellB = rowB.children[colIndex];
                    if (!cellA || !cellB) return 0;
                    return direction * compare(cellA, cellB, sortType);
                });
                rows.forEach((row) => tbody.appendChild(row));
            };
            doSort();
            activeSort = doSort;
            updateSortIndicators(header, direction);
        });
    });

    // Add sorting support for group headers (single-click sorts)
    groupHeaders.forEach((header) => {
        const btn = header.querySelector(".group-sort-btn");
        if (!btn) return;

        let direction = -1;
        btn.setAttribute("data-sort-direction", "none");

        btn.addEventListener("click", () => {
            direction *= -1;
            const doSort = () => {
                const tbody = table.querySelector("tbody");
                if (!tbody) return;
                const rows = Array.from(tbody.querySelectorAll("tr"));
                const allHeaderCells = Array.from(header.parentNode.children);
                const colIndex = allHeaderCells.indexOf(header);
                rows.sort((rowA, rowB) => {
                    const cellA = rowA.children[colIndex];
                    const cellB = rowB.children[colIndex];
                    if (!cellA || !cellB) return 0;

                    const textA = cellA.textContent.trim();
                    const textB = cellB.textContent.trim();

                    const percentA = parseFloat(textA.replace('%', ''));
                    const percentB = parseFloat(textB.replace('%', ''));

                    if (!isNaN(percentA) && !isNaN(percentB)) {
                        if (percentA === percentB) return 0;
                        return direction * (percentA < percentB ? -1 : 1);
                    }

                    return direction * compareStatus(textA, textB);
                });
                rows.forEach((row) => tbody.appendChild(row));
            };
            doSort();
            activeSort = doSort;
            updateSortIndicators(header, direction);
        });
    });

    // Add sorting support for task headers (when expanded)
    taskHeaders.forEach((header) => {
        const btn = header.querySelector(".task-sort-btn");
        if (!btn) return;

        let direction = -1;
        btn.setAttribute("data-sort-direction", "none");

        btn.addEventListener("click", (e) => {
            // Prevent the link from being followed
            e.preventDefault();

            direction *= -1;
            const tbody = table.querySelector("tbody");
            if (!tbody) return;

            const rows = Array.from(tbody.querySelectorAll("tr"));
            const allHeaderCells = Array.from(header.parentNode.children);
            const colIndex = allHeaderCells.indexOf(header);

            rows.sort((rowA, rowB) => {
                const cellA = rowA.children[colIndex];
                const cellB = rowB.children[colIndex];
                if (!cellA || !cellB) return 0;

                const textA = cellA.textContent.trim();
                const textB = cellB.textContent.trim();

                return direction * compareStatus(textA, textB);
            });

            rows.forEach((row) => tbody.appendChild(row));
            updateSortIndicators(header, direction);
        });
    });
});
