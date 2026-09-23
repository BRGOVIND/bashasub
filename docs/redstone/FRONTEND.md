# Redstone frontend foundation

## Boundary and information architecture

`frontend/` is a separate TypeScript application. It does not replace the root
BhashaSub translation page or import Python internals. During local development,
Vite proxies `/api` to the loopback Redstone API. Deploying it requires an
authenticated API boundary; the current API has no user authentication.

The shell has three destinations: Workspace (the primary artifact and agent),
Providers (connection architecture and current availability), and Field guide
(documentation/discovery entry). Workspace uses a project rail, an engineering
panel, and a preview stage. Below desktop width, these become selectable
surfaces, with preview first. A command palette indexes navigation and available
actions; disabled future actions never simulate success.

## Data and component boundaries

`src/api/` owns HTTP and public response types. Components never call `fetch`.
`src/state/` owns neutral provider descriptors. The app coordinator owns
ephemeral UI state and server-response state. Events are real backend events
read from the task history endpoint after the synchronous task returns; a
future stream transport can feed the same event state. No fabricated agent
timeline is rendered. Runtime and preview state can be refreshed through the
existing project GET endpoints; a 404 means that resource has not started.

Project creation returns only an ID, not a project listing or detail view. The
current session can display the created project, but it does not invent a
durable project library. There are no file-tree or file-content API endpoints;
the project rail labels that surface unavailable. An agent task POST is
synchronous, so no progress events can arrive until it returns. Runtime and
preview APIs exist, but a newly created project defaults to `static`, which
cannot run the Vite preview. The UI keeps those controls unavailable for that
project rather than claiming a running app.

## Security and preview

No credentials are stored in React state, storage, URLs, telemetry, or UI. The
provider screen describes modes only; BYOK input is deferred until a reviewed
one-request credential handoff exists. No analytics or service worker. API
errors are mapped to fixed, safe messages. Preview URLs are accepted only
from the API, and rendered only as an iframe on a separate origin with a
restrictive `sandbox` attribute. The gateway's default `frame-ancestors 'self'`
blocks embedding until the operator explicitly sets the UI origin. Production
also needs a distinct registrable preview domain, authentication, and the
unfinished P0 backend work. None is implied by this frontend.

## Visual system

Retro-futurist observatory: warm paper surfaces, dark ocean ink, turquoise
machinery, signal orange, and measured yellow. Large geometric stage, ruled
lines, circular instruments, and an original CSS/SVG illustration form one
coherent world. Typography is temporary and fully tokenized (`--font-display`,
`--font-body`, `--font-mono`; semantic size tokens). Color, spacing, borders,
and motion are CSS variables. No random gradients or decorative cards.

Motion marks changes of machine state and panel navigation, not ambient
activity. `prefers-reduced-motion` disables it. Focus rings, semantic buttons,
labels, live status, and keyboard command access are built in. Navigation
becomes a compact rail on smaller screens; workspace surfaces are chosen by
tabs instead of simply stacking three columns.

## Growth points

Provider descriptors are neutral records (`server`, `byok`, `local` modes;
capabilities and connection state). The provider catalog is informational,
not an assertion of backend availability. Skills, plugins, tools, models, and
Markdown docs share a future discovery taxonomy; no catalog endpoint, install
operation, pricing, or fake item is implemented yet. Future commands should
carry capability checks and route to typed API methods, not bespoke component
fetches.

## Local use

From `frontend/`, run `npm install` then `npm run dev`. Separately run
`python -m redstone.api.serve` for live API data. Run `npm test`, `npm run
typecheck`, and `npm run build` before committing. The frontend can render
its disconnected state without Docker or an API process.
