# Frontend (React + TypeScript + Vite)

## Dev

Prereqs:

- Node.js 18+ (Windows/macOS)

Commands:

- `npm install`
- `npm run dev`

The dev server runs on `http://127.0.0.1:5173` and proxies `/api/*` requests to the backend on `http://127.0.0.1:8000`.

## Gallery

The UI also loads a thumbnail gallery from the backend by calling:

- `GET /api/images?scope=repo`

Selecting a thumbnail auto-fills “Server path” mode using a repo-relative path and previews it via:

- `GET /api/image?path=<repo-relative-path>`
