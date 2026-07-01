# MCP Connection Collapse Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make each provider connection on the MCP dashboard collapsed by default while keeping its label, status, and actions visible.

**Architecture:** This is a presentation-only change in the server-rendered MCP template. Native `<details>` and `<summary>` elements provide collapse/expand behavior without JavaScript or persisted state. CSS keeps the summary compact and responsive.

**Tech Stack:** FastAPI, Jinja2 templates, pytest integration tests, plain CSS.

---

## File Structure

- Modify `tests/integration/test_web_mcp_template.py` to add a regression test for collapsed connection markup.
- Modify `jarvis/web/templates/mcp.html` to wrap each provider connection in `<details class="conn-row">` with a `<summary class="conn-summary">`.
- Modify `jarvis/web/static/style.css` to style the connection summary and expanded body.

### Task 1: Regression test

**Files:**
- Test: `tests/integration/test_web_mcp_template.py`

- [ ] **Step 1: Write the failing test**

Add this test after `test_page_renders_management_forms`:

```python
def test_provider_connections_are_collapsed_by_default(client):
    page = client.get("/mcp").text
    assert '<details class="conn-row">' in page
    assert "<summary" in page
    assert "Personal" in page

    details_start = page.index('<details class="conn-row">')
    summary_start = page.index("<summary", details_start)
    summary_end = page.index("</summary>", summary_start)
    assert "Personal" in page[summary_start:summary_end]
    assert "open" not in page[details_start:summary_start]
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run:

```bash
uv run pytest tests/integration/test_web_mcp_template.py::test_provider_connections_are_collapsed_by_default -q
```

Expected: FAIL because connection rows are still plain `<div class="conn-row">` elements.

### Task 2: Template and styling

**Files:**
- Modify: `jarvis/web/templates/mcp.html`
- Modify: `jarvis/web/static/style.css`
- Test: `tests/integration/test_web_mcp_template.py`

- [ ] **Step 1: Implement minimal template change**

Replace each provider connection wrapper with:

```html
<details class="conn-row">
  <summary class="conn-summary">
    <strong>{{ c.label }}</strong>
    <span class="badge {% if c.runtime_status=='connected' %}badge-ok{% elif c.runtime_status=='error' %}badge-err{% else %}badge-warn{% endif %}">{{ c.runtime_status }}</span>
    ...
  </summary>
  <div class="conn-details">
    {% if c.last_error %}<pre>{{ c.last_error }}</pre>{% endif %}
    {{ tools_table(c.tools) }}
  </div>
</details>
```

Move the existing connection action controls into the summary. Keep the existing form actions and conditions unchanged.

- [ ] **Step 2: Implement minimal CSS**

Add styles for `.conn-summary` and `.conn-details` so the collapsed header is compact and the expanded body keeps the existing spacing.

- [ ] **Step 3: Run the focused test to verify it passes**

Run:

```bash
uv run pytest tests/integration/test_web_mcp_template.py::test_provider_connections_are_collapsed_by_default -q
```

Expected: PASS.

- [ ] **Step 4: Run MCP template and page tests**

Run:

```bash
uv run pytest tests/integration/test_web_mcp_template.py tests/integration/test_web_mcp.py -q
```

Expected: PASS.

### Task 3: Rendered verification

**Files:**
- No source changes expected.

- [ ] **Step 1: Start or reuse the local web app**

Inspect the repo scripts and run the smallest local command that serves the dashboard, or use the existing app test client if the full app requires runtime services not present locally.

- [ ] **Step 2: Verify the MCP flow**

The flow under test is: `/mcp` loads -> provider connection renders collapsed -> expanding the connection reveals the existing details without console or framework errors.

