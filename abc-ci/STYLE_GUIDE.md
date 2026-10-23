# abc-ci Web Interface Style Guide

## Purpose

This document describes the visual and interaction style for the **abc-ci** web interface.

The goal is to define a consistent design language for pages that present structured benchmark information, model results, run metadata, and related tabular reports. This is not a full product specification and does not prescribe the complete page set, routing model, data model, or application behavior. It is a style guide for producing web pages that match the clean, restrained prototype direction already explored.

The desired interface should feel like a careful internal technical report rendered as a web page: plain, readable, durable, and focused on the data. It should avoid the over-stylized look common to many generated dashboards.

---

# Overall Design Character

The **abc-ci** interface should feel:

* Data-first.
* Plain but deliberate.
* Compact without being cramped.
* Familiar to technical users.
* Closer to an HTML report than a commercial dashboard.
* Easy to inspect, print, screenshot, and compare.

It should not feel:

* Like a startup SaaS dashboard.
* Like a marketing site.
* Like a component-library demo.
* Like a dark neon developer console.
* Like a “vibe-coded” interface with excessive visual effects.

The preferred aesthetic is “simple HTML, carefully typeset.” The page should look intentionally plain, not unfinished.

---

# Core Visual Principles

## 1. Use tables as first-class UI elements

Many **abc-ci** pages will likely contain structured results, comparisons, run summaries, benchmark outcomes, or metadata. Tables should be treated as primary interface elements, not as fallback presentation.

Use real HTML tables for tabular data. Do not replace tables with grids of cards unless the data is genuinely non-tabular.

## 2. Prefer borders over shadows

Use thin borders to define page regions, table cells, tabs, and report sections.

Avoid relying on shadows, floating panels, glass effects, or layered cards. This interface should feel stable and document-like.

## 3. Keep the color system sparse

Most of the page should be black text on white background with gray borders and gray table headers.

Use color only when it carries meaning, such as links, pass/fail state, or selected navigation.

## 4. Make hierarchy clear but modest

Headings should establish page structure, but they should not dominate the content. Avoid oversized titles, hero sections, large metric cards, and promotional blocks.

## 5. Preserve inspectability

A user should be able to quickly answer:

* What page am I on?
* What result or model am I looking at?
* What category or filter is active?
* Which rows passed or failed?
* Where can I go next?
* What was last updated?

The interface should support careful reading and comparison, not just visual scanning.

---

# Page Structure

## Recommended Page Container

Use a wide, centered page container with modest padding.

```css
body {
  margin: 0;
  padding: 0;
  background: #fff;
}

.page {
  max-width: 1500px;
  margin: 0 auto;
  padding: 16px 24px 32px;
}
```

Pages with very wide tables may use nearly the full viewport width. Avoid narrow centered layouts that make data tables uncomfortable to read.

## Report Frame

A simple bordered frame may be used around a major page section.

```css
.report-frame {
  border: 1px solid #333;
  padding: 18px;
  background: #fff;
}
```

This is a useful visual motif for **abc-ci**: it gives the page a structured, report-like quality without adding decorative styling.

Avoid stacking many independent rounded cards. If the page has multiple regions, prefer simple sections separated by headings, borders, or tables.

---

# Typography

## Font Family

Use system sans-serif fonts. Arial/Helvetica is especially appropriate for the plain HTML-report aesthetic.

```css
body {
  font-family: Arial, Helvetica, sans-serif;
  font-size: 14px;
  line-height: 1.35;
  color: #111;
}
```

A system UI stack is also acceptable:

```css
body {
  font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
```

Do not use display fonts.

## Heading Scale

Use conservative heading sizes.

```css
h1 {
  font-size: 28px;
  font-weight: 700;
  margin: 0 0 16px;
}

h2 {
  font-size: 22px;
  font-weight: 700;
  margin: 24px 0 8px;
}

h3 {
  font-size: 18px;
  font-weight: 700;
  margin: 20px 0 8px;
}
```

The headings should identify structure, not create a landing-page feel.

## Body Text

Keep body text direct and sparse. Most pages should not need much prose. When explanatory text is necessary, keep it close to the relevant table or section.

```css
p {
  margin: 6px 0 10px;
}
```

Use muted text sparingly for metadata, notes, and secondary descriptions.

```css
.muted {
  color: #555;
}
```

---

# Color Palette

Use a minimal palette.

```css
:root {
  --text: #111;
  --background: #fff;

  --border-dark: #333;
  --border: #b8b8b8;
  --border-light: #d0d0d0;

  --header-bg: #f0f0f0;
  --tab-bg: #f8f8f8;
  --tab-active-bg: #e6e6e6;

  --link: #0000aa;
  --pass: #078300;
  --fail: #c00000;
  --muted: #555;
}
```

## Links

Use conventional blue underlined links.

```css
a {
  color: var(--link);
  text-decoration: underline;
}
```

This is intentional. It contributes to the plain technical-report feel and makes navigation obvious.

## Status Colors

Use color only as reinforcement. The text itself must still communicate state.

```css
.status-pass {
  color: var(--pass);
  font-weight: 700;
}

.status-fail {
  color: var(--fail);
  font-weight: 700;
}
```

Prefer:

```text
Pass
Fail
```

Avoid relying only on icons or colored dots.

---

# Header and Navigation

## Site Header

A typical **abc-ci** page should have a simple header with the project name on the left and a small set of links on the right.

```html
<header class="site-header">
  <h1>abc-ci</h1>
  <nav>
    <a href="/leaderboard">Leaderboard</a>
    <a href="/models">Models</a>
    <a href="/runs">Runs</a>
    <a href="/docs">Docs</a>
  </nav>
</header>
```

```css
.site-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 24px;
  margin-bottom: 14px;
}

.site-header h1 {
  margin: 0;
  font-size: 28px;
  font-weight: 700;
}

.site-header nav {
  display: flex;
  gap: 28px;
}
```

Navigation should be plain. Do not use pill buttons, large nav bars, sidebars by default, or animated active states.

## Breadcrumbs

For drill-down pages, include breadcrumbs.

```html
<div class="breadcrumbs">
  <a href="/leaderboard">Leaderboard</a>
  <span>&gt;</span>
  <span>AuroraCoder 1.3</span>
</div>
```

```css
.breadcrumbs {
  display: flex;
  gap: 8px;
  align-items: center;
}
```

Use breadcrumbs to preserve orientation when navigating from aggregate pages into detailed reports.

---

# Tabs and Category Controls

Tabs are appropriate for switching between categories, result groups, views, or subsets of benchmark data.

The tabs should look simple and table-adjacent.

```html
<div class="tabs">
  <a class="tab active" href="?category=all">All</a>
  <a class="tab" href="?category=hpc">HPC</a>
  <a class="tab" href="?category=fortran">Fortran</a>
  <a class="tab" href="?category=numerical-libraries">Numerical Libraries</a>
  <a class="tab" href="?category=data-systems">Data Systems</a>
</div>
```

```css
.tabs {
  display: flex;
  width: fit-content;
  max-width: 100%;
  overflow-x: auto;
  border: 1px solid var(--border);
  border-bottom: none;
  margin: 10px 0 0;
}

.tab {
  min-width: 90px;
  padding: 5px 14px;
  text-align: center;
  border-right: 1px solid var(--border-light);
  background: var(--tab-bg);
  color: var(--link);
  text-decoration: none;
  white-space: nowrap;
}

.tab:last-child {
  border-right: none;
}

.tab.active {
  background: var(--tab-active-bg);
  color: #000066;
  font-weight: 700;
}
```

Tabs should not be large, rounded, animated, or visually dominant. They are controls, not decoration.

---

# Tables

Tables are the central visual language of **abc-ci**.

## General Table Styling

```css
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

th,
td {
  border: 1px solid var(--border-light);
  padding: 6px 8px;
  vertical-align: middle;
}

th {
  background: var(--header-bg);
  font-weight: 700;
  text-align: left;
}

td.numeric,
th.numeric {
  text-align: center;
  font-variant-numeric: tabular-nums;
}
```

Use `border-collapse: collapse`. Avoid rounded table containers unless required by a broader layout.

## Header Cells

Use gray table headers with clear labels. Multi-line headers are acceptable when they keep tables compact.

```html
<th class="numeric">HPC<br>(%)</th>
```

## Numeric Alignment

Numerical values should use centered or right-aligned tabular formatting. Be consistent within a table.

Recommended for percentages and counts:

```css
.numeric {
  text-align: center;
  font-variant-numeric: tabular-nums;
}
```

## Row Hover

A subtle hover state is acceptable.

```css
tbody tr:hover {
  background: #fafafa;
}
```

Avoid bright or animated hover effects.

---

# Data Formatting

## Percentages

Use one decimal place unless the data requires more precision.

Preferred:

```text
74.6
```

Use column headers to indicate units:

```text
All (%)
Pass Rate (%)
```

Avoid adding `%` to every table cell in large tables.

## Counts

Use raw counts when they help make benchmark outcomes auditable.

Preferred:

```text
12 / 15
```

or split into explicit columns:

```text
Tests Passed
Total Tests
```

## Dates

Use ISO dates in tables:

```text
2026-06-02
```

Use prose dates only in metadata text if desired:

```text
Last Updated: June 2, 2026
```

Be consistent within a page.

## Missing Data

Use an em dash for missing values.

```text
—
```

Avoid mixing `N/A`, empty cells, `null`, and `-` unless each has a distinct meaning.

---

# Common Page Patterns

This section describes reusable patterns, not a complete page inventory.

## Aggregate Comparison Pages

For pages that compare models, runs, benchmark suites, or categories, use:

* A clear page title.
* Optional category or view tabs.
* A primary comparison table.
* Linked names for drill-down entities.
* A short table note indicating result count or data scope.

Example structure:

```html
<section class="report-frame">
  <header class="site-header">
    <h1>abc-ci</h1>
    <nav>
      <a href="/leaderboard">Leaderboard</a>
      <a href="/models">Models</a>
      <a href="/runs">Runs</a>
      <a href="/docs">Docs</a>
    </nav>
  </header>

  <h2>Leaderboard</h2>

  <div class="tabs">
    <a class="tab active" href="?category=all">All</a>
    <a class="tab" href="?category=hpc">HPC</a>
    <a class="tab" href="?category=fortran">Fortran</a>
  </div>

  <div class="table-scroll">
    <table>
      <!-- comparison data -->
    </table>
  </div>

  <p class="table-note">Showing 1 to 7 of 12 models.</p>
</section>
```

## Detail Report Pages

For pages focused on a single model, run, benchmark, or result set, use:

* Breadcrumbs.
* A clear title.
* Metadata immediately under the title.
* A compact summary table.
* Detailed tables below.
* Category tabs or filters where useful.

Example structure:

```html
<section class="report-frame">
  <div class="report-nav">
    <div class="breadcrumbs">
      <a href="/leaderboard">Leaderboard</a>
      <span>&gt;</span>
      <span>AuroraCoder 1.3</span>
    </div>
    <a href="/leaderboard">← Back to Leaderboard</a>
  </div>

  <h1>AuroraCoder 1.3 Report</h1>
  <p class="metadata">Last Updated: 2026-06-02</p>

  <table class="summary-table">
    <!-- compact summary metrics -->
  </table>

  <h2>Benchmark Results</h2>

  <div class="tabs">
    <a class="tab active" href="?category=all">All</a>
    <a class="tab" href="?category=hpc">HPC</a>
    <a class="tab" href="?category=fortran">Fortran</a>
  </div>

  <div class="table-scroll">
    <table>
      <!-- detailed rows -->
    </table>
  </div>
</section>
```

## Metadata Blocks

For compact metadata, prefer simple text or a small table.

Good:

```html
<p class="metadata">Last Updated: 2026-06-02</p>
```

Also good:

```html
<table class="metadata-table">
  <tbody>
    <tr>
      <th>Run ID</th>
      <td>run-2026-06-02-001</td>
    </tr>
    <tr>
      <th>Evaluator Version</th>
      <td>0.3.1</td>
    </tr>
  </tbody>
</table>
```

Avoid large metadata cards unless there is a specific need.

---

# Forms and Filters

Forms should be plain and compact.

```html
<form class="filters">
  <label>
    Search:
    <input type="search" name="q">
  </label>

  <label>
    Category:
    <select name="category">
      <option>All</option>
      <option>HPC</option>
      <option>Fortran</option>
    </select>
  </label>

  <button type="submit">Apply</button>
</form>
```

```css
.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: end;
  margin: 12px 0;
}

input,
select,
button {
  font: inherit;
  padding: 3px 6px;
}
```

Buttons should look like ordinary form controls. Avoid oversized filled CTA buttons.

---

# Status and Result Display

Pass/fail states should be shown as text in tables.

```html
<td class="status-pass">Pass</td>
<td class="status-fail">Fail</td>
```

If additional statuses are needed, keep them textual and restrained.

Examples:

```text
Pass
Fail
Timeout
Error
Skipped
Pending
```

For non-pass/fail statuses, use neutral styling unless there is a strong reason to color them.

```css
.status-muted {
  color: var(--muted);
  font-weight: 700;
}
```

Avoid icon-only statuses.

---

# Responsive Behavior

The interface may be table-heavy. It does not need to transform into a card-based mobile app.

Use horizontal scrolling for wide tables.

```html
<div class="table-scroll">
  <table>
    ...
  </table>
</div>
```

```css
.table-scroll {
  overflow-x: auto;
}
```

For small screens, stack the header and navigation.

```css
@media (max-width: 800px) {
  .page {
    padding: 12px;
  }

  .site-header,
  .report-nav {
    flex-direction: column;
    align-items: flex-start;
    gap: 8px;
  }

  .site-header nav {
    flex-wrap: wrap;
    gap: 12px;
  }
}
```

Do not redesign tables into mobile cards unless a specific page requires it.

---

# Accessibility

Use semantic HTML.

Required practices:

* Use real table markup for tabular data.
* Use `th` for headers.
* Use `scope="col"` and `scope="row"` where helpful.
* Use text labels for status values.
* Do not rely on color alone.
* Keep link text descriptive.
* Preserve keyboard navigation.
* Maintain strong color contrast.

Useful utility:

```css
.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
```

---

# Things to Avoid

Avoid these visual patterns:

* Gradient backgrounds.
* Glassmorphism.
* Neon colors.
* Large rounded cards.
* Heavy shadows.
* Animated counters.
* Decorative icons.
* Emoji in the interface.
* Hero sections.
* Marketing language.
* Dark mode as the default visual identity.
* Excessive whitespace around dense data.
* Large metric tiles as the primary layout.
* Custom chart decorations when a table would be clearer.
* Ambiguous headings like “Insights” or “Overview” when a more precise label is available.

Avoid these interaction patterns:

* Infinite scrolling for audit-oriented tables.
* Modal-only access to important benchmark details.
* Hidden hover-only controls.
* Pass/fail indicators that require tooltips.
* Client-side effects that make the page hard to print or screenshot.

---

# Preferred Language

Use plain, precise labels.

Preferred labels:

```text
Leaderboard
Model Report
Benchmark Results
Run Summary
Category
Pass / Fail
Tests Passed
Total Tests
Pass Rate (%)
Last Updated
Back to Leaderboard
```

Avoid promotional or vague labels:

```text
Explore Performance
Model Intelligence
Deep Dive
Agent Insights
Crush Rate
```

The UI language should be conventional and technical.

---

# Recommended CSS Baseline

```css
:root {
  --text: #111;
  --background: #fff;
  --border-dark: #333;
  --border: #b8b8b8;
  --border-light: #d0d0d0;
  --header-bg: #f0f0f0;
  --tab-bg: #f8f8f8;
  --tab-active-bg: #e6e6e6;
  --link: #0000aa;
  --pass: #078300;
  --fail: #c00000;
  --muted: #555;
}

* {
  box-sizing: border-box;
}

body {
  margin: 0;
  font-family: Arial, Helvetica, sans-serif;
  font-size: 14px;
  line-height: 1.35;
  color: var(--text);
  background: var(--background);
}

.page {
  max-width: 1500px;
  margin: 0 auto;
  padding: 16px 24px 32px;
}

.report-frame {
  border: 1px solid var(--border-dark);
  padding: 18px;
  background: #fff;
}

.site-header,
.report-nav {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 24px;
}

.site-header {
  margin-bottom: 14px;
}

.site-header h1 {
  margin: 0;
  font-size: 28px;
  font-weight: 700;
}

.site-header nav {
  display: flex;
  gap: 28px;
}

a {
  color: var(--link);
  text-decoration: underline;
}

h1 {
  margin: 16px 0 6px;
  font-size: 26px;
  font-weight: 700;
}

h2 {
  margin: 24px 0 8px;
  font-size: 20px;
  font-weight: 700;
}

h3 {
  margin: 20px 0 8px;
  font-size: 18px;
  font-weight: 700;
}

p {
  margin: 6px 0 10px;
}

.metadata,
.table-note {
  color: var(--text);
  margin: 6px 0 14px;
}

.breadcrumbs {
  display: flex;
  gap: 8px;
  align-items: center;
}

.tabs {
  display: flex;
  width: fit-content;
  max-width: 100%;
  overflow-x: auto;
  border: 1px solid var(--border);
  border-bottom: none;
  margin: 10px 0 0;
}

.tab {
  min-width: 90px;
  padding: 5px 14px;
  text-align: center;
  border-right: 1px solid var(--border-light);
  background: var(--tab-bg);
  color: var(--link);
  text-decoration: none;
  white-space: nowrap;
}

.tab:last-child {
  border-right: none;
}

.tab.active {
  background: var(--tab-active-bg);
  color: #000066;
  font-weight: 700;
}

.table-scroll {
  overflow-x: auto;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

th,
td {
  border: 1px solid var(--border-light);
  padding: 6px 8px;
  vertical-align: middle;
}

th {
  background: var(--header-bg);
  font-weight: 700;
  text-align: left;
}

td.numeric,
th.numeric {
  text-align: center;
  font-variant-numeric: tabular-nums;
}

tbody tr:hover {
  background: #fafafa;
}

.summary-table {
  width: auto;
  min-width: 860px;
  margin-top: 14px;
}

.status-pass {
  color: var(--pass);
  font-weight: 700;
}

.status-fail {
  color: var(--fail);
  font-weight: 700;
}

.status-muted {
  color: var(--muted);
  font-weight: 700;
}

.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: end;
  margin: 12px 0;
}

input,
select,
button {
  font: inherit;
  padding: 3px 6px;
}

.pagination {
  display: flex;
  gap: 12px;
  align-items: center;
  margin-top: 12px;
}

@media (max-width: 800px) {
  .page {
    padding: 12px;
  }

  .site-header,
  .report-nav {
    flex-direction: column;
    align-items: flex-start;
    gap: 8px;
  }

  .site-header nav {
    flex-wrap: wrap;
    gap: 12px;
  }
}
```

---

# Design Acceptance Criteria

A page in **abc-ci** follows this style if:

* It is readable without interpretation or visual decoding.
* The main data is presented in tables or simple metadata blocks.
* Navigation is visible and conventional.
* Links look like links.
* Pass/fail or status information is text-first.
* Borders and table structure do most of the layout work.
* The page uses color sparingly and semantically.
* It avoids decorative dashboard conventions.
* It feels like a technical report with a clean web presentation.

The target result is simple, structured, and slightly old-fashioned, while still looking intentional and maintained.
