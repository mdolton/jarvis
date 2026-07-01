# MCP connection collapse

## Goal

Reduce vertical space on the MCP dashboard by making each provider connection collapsible. Every connection is collapsed by default when `/mcp` renders.

## Scope

- Applies to provider connections in the "Providers & connections" section.
- Does not change provider rows, stdio server rows, connection lifecycle actions, OAuth actions, tool policy updates, or backend state.
- Does not persist expanded or collapsed state across requests.

## Design

Each connection row becomes a native HTML `<details>` element. The connection header moves into `<summary>` and remains visible while collapsed:

- connection label
- runtime status badge
- existing connect, disconnect, enable, disable, and remove controls

Expanding a connection reveals the existing detail content:

- last error, if present
- tool policy table, if tools exist

The template omits the `open` attribute so all connections start collapsed. Native browser behavior handles keyboard and pointer toggling without JavaScript.

## Styling

The existing `.conn-row` visual indentation remains. A small `.conn-summary` flex layout keeps the label, status, and controls scannable on desktop while allowing wrapping on narrow screens. Existing button, badge, form, table, and mobile styles continue to apply.

## Testing

Add a template-level integration test proving a rendered connection uses `<details class="conn-row">`, has a `<summary>` containing the connection label, and does not include an `open` attribute by default. Existing MCP page tests continue to cover lifecycle forms and tool policy forms.

