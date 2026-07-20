# Restaurant Review Workbook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert the 460-row Naver restaurant candidate CSV into a readable Excel review workbook with live summaries, controlled review statuses, filters, links, and review guidance.

**Architecture:** A single temporary JavaScript builder imports the UTF-8 CSV with `@oai/artifact-tool`, creates three worksheets, applies formulas and review-focused formatting, renders each sheet for visual QA, and exports one `.xlsx` artifact. The source CSV remains unchanged and is the sole input.

**Tech Stack:** Bundled Node.js runtime, `@oai/artifact-tool`, Excel `.xlsx`

## Global Constraints

- Source: `data/curation/restaurant_candidates.csv`
- Output: `outputs/restaurant-review/restaurant_candidates_review.xlsx`
- Preserve all 21 source fields and all 460 source rows.
- Create exactly three sheets: `검토 현황`, `후보 검토`, `검토 가이드`.
- Do not modify the source CSV.
- Use formulas for status and district summaries.
- Visually verify every worksheet before final export.

---

### Task 1: Build the review workbook

**Files:**
- Create: conversation-specific `build_restaurant_review_workbook.mjs`
- Read: `data/curation/restaurant_candidates.csv`
- Create: `outputs/restaurant-review/restaurant_candidates_review.xlsx`

**Interfaces:**
- Consumes: UTF-8 CSV text with 21 named columns.
- Produces: one workbook whose `후보 검토` table contains 460 data rows.

- [ ] **Step 1: Load the bundled spreadsheet runtime**

Run `codex_app__load_workspace_dependencies` and create a junction from the conversation-specific workspace to the returned `node_modules` directory.

- [ ] **Step 2: Create the builder**

Use `Workbook.fromCSV(csvText, { sheetName: "후보 검토" })`, preserve every source field, and reorder columns into this review-first order:

```text
review_status, district, name, category, recommendation_score,
recommendation_reason, address, road_address, blog_search_url,
naver_link, possible_duplicate, reject_reason, latest_post_date,
recent_blog_count, distinct_blogger_count, blog_result_count,
local_hit_count, comment_sort_hit_count, matched_queries,
latitude, longitude
```

Convert count and score columns to numbers. Convert non-empty `latest_post_date` values from `YYYYMMDD` text into real dates.

- [ ] **Step 3: Format the candidate sheet**

Add an Excel table named `RestaurantCandidates`, freeze the header row, apply filters, set review-oriented column widths, wrap descriptive fields, and add list validation to `A2:A461` with these values:

```text
pending, approved, hold, rejected
```

Apply conditional formats to `A2:A461`: approved green, hold amber, rejected red, pending gray. Keep URL cells as clickable hyperlinks while retaining their source URLs.

- [ ] **Step 4: Create the summary sheet**

Create `검토 현황` before the candidate sheet and populate formulas such as:

```excel
=COUNTA('후보 검토'!$C$2:$C$461)
=COUNTIF('후보 검토'!$A$2:$A$461,"approved")
=COUNTIFS('후보 검토'!$B$2:$B$461,$A12,'후보 검토'!$A$2:$A$461,B$11)
```

Show overall status cards and a district-by-status matrix for 대덕구, 동구, 서구, 유성구, 중구.

- [ ] **Step 5: Create the guide sheet**

Add status definitions, representative rejection reasons, manual verification fields, and a note that recommendation scores only determine review order.

- [ ] **Step 6: Export a checkpoint workbook**

Run the builder with the bundled Node.js executable and export `outputs/restaurant-review/restaurant_candidates_review.xlsx`.

Expected: the output file exists and contains three worksheets.

### Task 2: Verify data and visual quality

**Files:**
- Inspect: `outputs/restaurant-review/restaurant_candidates_review.xlsx`
- Create: conversation-specific PNG previews for QA only

**Interfaces:**
- Consumes: the workbook from Task 1.
- Produces: inspection evidence and three rendered previews; no second workbook variant.

- [ ] **Step 1: Inspect workbook structure and key ranges**

Use `workbook.inspect` to confirm:

```text
검토 현황: summary formulas present
후보 검토: 461 rows including header, 21 columns
검토 가이드: status and rejection guidance populated
```

- [ ] **Step 2: Reconcile source totals**

Confirm 460 total rows and district counts:

```text
대덕구=100, 동구=100, 서구=100, 유성구=100, 중구=60
```

- [ ] **Step 3: Scan formula errors**

Run a regex match inspection for `#REF!|#DIV/0!|#VALUE!|#NAME?|#N/A` and require zero results.

- [ ] **Step 4: Render every worksheet**

Render compact ranges from `검토 현황`, `후보 검토`, and `검토 가이드`. Visually inspect headers, summary values, status controls, links, column widths, wrapping, and clipping.

- [ ] **Step 5: Apply focused fixes and re-export**

If visual defects exist, patch the single builder, rerun it, repeat the affected render, and keep only the final `.xlsx` artifact.

- [ ] **Step 6: Final verification**

Confirm the source CSV remains unchanged, the output workbook opens through `SpreadsheetFile.importXlsx`, and only the intended `.xlsx` artifact is delivered.
