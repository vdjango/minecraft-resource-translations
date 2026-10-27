# Minecraft Resource Translations frontend

Vue 3 and Vite compile the existing server-rendered pages to browser-ready JavaScript modules. The HTML, CSS, text, route URLs and page structure remain in the original web directory (`src/mc_translate/web`). Vue renders only the list areas that have been migrated to components; the other established controllers run in a Vue-managed page lifecycle while they are moved incrementally.

FastAPI is not introduced: existing aiohttp API, authentication, database, queue and routes stay as-is.

## Build and test

Requirements: Node.js 20.19 or newer. Run from this directory:

```sh
npm ci
npm run build
npm test
```

`npm run dev` rebuilds entries when source changes; run the existing Python service separately to view those assets.

The build writes compiled entries beside the existing assets, does not delete CSS or HTML, and updates each module URL with a content revision so a browser does not keep an old entry. Shared runtime chunks use immutable hash names. Node is a build-time dependency only; it is not needed by the deployed Python service.

`pages.json` (and `pages.site.json`) lists the original page controllers included in each module. `src/controllers/` holds the compatibility bridges for the resource catalogue, release records and node network list. The original public JavaScript files remain available for old or cached HTML. `src/runtime.ts` owns page-scoped event handlers, polling timers, request timeouts/cancellation, mounted Vue views and cleanup.
