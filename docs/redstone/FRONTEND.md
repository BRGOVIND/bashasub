# Redstone frontend foundation

## Boundary and information architecture

`frontend/` is a separate TypeScript application. It does not replace the root
BhashaSub translation page or import Python internals. During local development,
Vite proxies `/api` to the loopback Redstone API. Deploying it requires an
authenticated API boundary; the current API has no user authentication.

The shell has Workspace (the primary artifact and agent), Providers,
Ecosystem (category structure, no fake installs), Help, and Legal routes.
Unknown paths render a branded 404 with a route home. Workspace uses a project rail, an engineering
panel, and a preview stage. Below desktop width, these become selectable
surfaces, with preview first. A command palette indexes navigation and available
actions; disabled future actions never simulate success.

For production hosting, route `/api` to the Redstone API and rewrite other
frontend paths to `index.html` so direct `/help`, `/legal`, and unknown-route
visits reach the client router. The client 404 is visual; the SPA host may
still return HTTP 200 unless a server-side routing layer is added.

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
lines, circular instruments, and original illustration form one coherent
world. The principal illustration is an original generated raster
asset, saved under `frontend/public/art/`; the dust-inspired mark and favicon
are original SVG, not game assets. The source illustration is retained in the
Codex generated-images folder; the frontend serves a 564 KB JPEG encoding.
Typography is temporary and fully tokenized (`--font-display`,
`--font-body`, `--font-mono`; semantic size tokens). Color, spacing, borders,
and motion are CSS variables. No random gradients or decorative cards.

One-shot panel/mark entrances and a slow, low-cost ambient panorama give the
world motion without a JavaScript animation loop. `prefers-reduced-motion`
disables them. Focus rings, semantic links and buttons,
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

The agent panel accepts a local `.txt` or `.md` idea file up to 16 KB. Reading
it fills the editable draft; no upload occurs on file selection. Only pressing
Send uses the existing agent API. The user should inspect the draft and remove
secrets before sending. Other media need a future reviewed backend contract.

## Art-direction prompt

> Design Redstone as a development observatory imagined by a 1960s future-city
> illustrator: hand-made poster texture, glass domes, elevated routes, human
> scale, sea-blue and turquoise machinery, cream paper, saffron light, coral
> red energy. Keep preview as the primary artifact and controls useful rather
> than decorative. Use sentence-case labels, editorial type, asymmetry, sparse
> geometric linework, and original art. Animate only transform/opacity, with
> reduced-motion support. Avoid generic AI gradients, glass cards, fake events,
> copied game marks, and placeholder marketplace listings.

The illustration prompt was: "Original optimistic 1960s imagined future city
and maker observatory, with a giant glass-domed pavilion, elevated walkways
and transit, small human explorers, layered architecture, and geometric
landscaping. Hand-painted mid-century science-fiction magazine illustration;
visible screenprint grain and imperfect ink registration. Wide panorama with
quiet sky for responsive crops. Turquoise, sea blue, aqua, warm cream, pale
saffron, coral red, moss green, restrained dark teal ink. No text, logos,
watermarks, robots, cyberpunk, or direct copy of the supplied reference."
The raster is a new composition, not a copy of the supplied reference image.

## Public-launch boundary

This site deliberately has `noindex,nofollow` while authentication, P0 security,
operator identification, hosting, retention, third-party processing, and
public service terms remain unresolved. The Legal route is a current-state
notice, **not** a completed privacy policy or hosted-service agreement.
GitHub `@BRGOVIND` is the only approved public contact; the git-config email
is not published. Remove `noindex` and review actual disclosures only when
the production deployment and legal/operator details are known.
The [ICO privacy-notice guide](https://ico.org.uk/for-organisations/uk-gdpr-guidance-and-resources/individual-rights/the-right-to-be-informed/what-privacy-information-should-we-provide/)
is a useful checklist, not a substitute for jurisdiction-specific legal review.
The logo is deliberately original rather than copying game art; see the
[Minecraft usage guidelines](https://www.minecraft.net/en-us/usage-guidelines)
when evaluating any closer game-brand resemblance.

## Local use

From `frontend/`, run `npm install` then `npm run dev`. Separately run
`python -m redstone.api.serve` for live API data. Run `npm test`, `npm run
typecheck`, and `npm run build` before committing. The frontend can render
its disconnected state without Docker or an API process.
