from __future__ import annotations

from pathlib import Path

from .models import ProjectSpace


SCAFFOLD_MARKER = "chat-orchestrate fallback scaffold"


def should_scaffold(goal: str, role: str = "") -> bool:
    lowered = f"{goal} {role}".lower()
    role_lowered = role.lower()
    implementation_role = any(term in role_lowered for term in ["frontend", "backend", "engineer", "implementation"])
    implementation_goal = any(term in lowered for term in ["website", "frontend", "backend", "api", "app", "page", "ui"])
    return implementation_role and implementation_goal


def scaffold_project(project: ProjectSpace, goal: str, role: str = "") -> list[Path]:
    if not should_scaffold(goal, role):
        return []
    project.path.mkdir(parents=True, exist_ok=True)
    brand = _brand_from_goal(goal)
    files = {
        "frontend/index.html": _index_html(brand),
        "frontend/styles.css": _styles_css(brand),
        "frontend/app.js": _app_js(brand),
        "backend/app.py": _backend_py(brand),
        "README.generated.md": _readme(brand, goal),
    }
    written = []
    for relative, content in files.items():
        written.append(_safe_write(project.path / relative, content))
    return written


def _safe_write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        current = path.read_text(encoding="utf-8", errors="replace")
        if SCAFFOLD_MARKER not in current:
            path = path.with_name(f"{path.stem}.generated{path.suffix}")
    path.write_text(content, encoding="utf-8")
    return path


def _brand_from_goal(goal: str) -> str:
    lowered = goal.lower()
    if "github" in lowered:
        return "github"
    if "google" in lowered:
        return "google"
    return "project"


def _index_html(brand: str) -> str:
    title = "GitHub-style Project Hub" if brand == "github" else "Search Workspace" if brand == "google" else "Project Workspace"
    return f"""<!doctype html>
<!-- {SCAFFOLD_MARKER}: frontend entry -->
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{title}</title>
    <link rel="stylesheet" href="./styles.css" />
  </head>
  <body>
    <main class="app-shell">
      <section class="hero">
        <div>
          <p class="eyebrow">Distributed agent build</p>
          <h1>{title}</h1>
          <p class="lede">A visible scaffold created inside the project workspace while the selected local CLI is unavailable.</p>
        </div>
        <button id="refresh-button" type="button">Refresh data</button>
      </section>
      <section class="repo-grid" id="repo-grid" aria-live="polite"></section>
    </main>
    <script src="./app.js"></script>
  </body>
</html>
"""


def _styles_css(brand: str) -> str:
    if brand == "github":
        colors = {
            "bg": "#0d1117",
            "panel": "#161b22",
            "text": "#f0f6fc",
            "muted": "#8b949e",
            "accent": "#2f81f7",
            "border": "#30363d",
        }
    else:
        colors = {
            "bg": "#101418",
            "panel": "#172026",
            "text": "#f7fafc",
            "muted": "#a7b0b8",
            "accent": "#14b8a6",
            "border": "#2d3a42",
        }
    return f"""/* {SCAFFOLD_MARKER}: frontend styles */
:root {{
  color-scheme: dark;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: {colors["bg"]};
  color: {colors["text"]};
}}

* {{ box-sizing: border-box; }}

body {{
  margin: 0;
  min-height: 100vh;
  background: {colors["bg"]};
}}

.app-shell {{
  width: min(1100px, calc(100vw - 32px));
  margin: 0 auto;
  padding: 48px 0;
}}

.hero {{
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 24px;
  padding-bottom: 28px;
  border-bottom: 1px solid {colors["border"]};
}}

.eyebrow {{
  margin: 0 0 8px;
  color: {colors["accent"]};
  font-size: 13px;
  font-weight: 700;
  text-transform: uppercase;
}}

h1 {{
  margin: 0;
  font-size: 42px;
  line-height: 1.08;
  letter-spacing: 0;
}}

.lede {{
  max-width: 680px;
  color: {colors["muted"]};
  font-size: 16px;
  line-height: 1.6;
}}

button {{
  min-height: 40px;
  border: 1px solid {colors["border"]};
  border-radius: 8px;
  padding: 0 16px;
  background: {colors["panel"]};
  color: {colors["text"]};
  font-weight: 700;
  cursor: pointer;
}}

.repo-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 16px;
  padding-top: 24px;
}}

.repo-card {{
  min-height: 168px;
  border: 1px solid {colors["border"]};
  border-radius: 8px;
  padding: 18px;
  background: {colors["panel"]};
}}

.repo-card h2 {{
  margin: 0 0 10px;
  font-size: 18px;
}}

.repo-card p {{
  color: {colors["muted"]};
  line-height: 1.5;
}}

.repo-meta {{
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 18px;
}}

.repo-meta span {{
  border-radius: 999px;
  padding: 4px 9px;
  background: {colors["bg"]};
  color: {colors["muted"]};
  font-size: 12px;
}}
"""


def _app_js(brand: str) -> str:
    repos = (
        [
            ("agent-harness", "Distributed Chainlit coordination with live CLI streams.", "Python"),
            ("repo-preview", "Frontend workspace showing generated artifacts and preview links.", "JavaScript"),
            ("mesh-coordinator", "Token-gated machine roster and task handoff state.", "FastAPI"),
        ]
        if brand == "github"
        else [
            ("search-ui", "Simple frontend search surface.", "JavaScript"),
            ("api-layer", "Backend routes for demo search data.", "FastAPI"),
            ("workspace-preview", "Local preview server for generated artifacts.", "Python"),
        ]
    )
    repo_literal = ",\n".join(
        f'  {{ name: "{name}", description: "{description}", language: "{language}" }}'
        for name, description, language in repos
    )
    return f"""// {SCAFFOLD_MARKER}: frontend behavior
const fallbackRepos = [
{repo_literal}
];
const apiBase = (
  window.CHAT_ORCHESTRATE_API_BASE ||
  window.FORGEHUB_API_BASE ||
  window.SEARCHLY_API_BASE ||
  "http://127.0.0.1:8000"
).replace(new RegExp("/+$"), "");

async function loadRepos() {{
  try {{
    const response = await fetch(`${{apiBase}}/api/repos`);
    if (!response.ok) throw new Error(`API returned ${{response.status}}`);
    return await response.json();
  }} catch (error) {{
    return fallbackRepos;
  }}
}}

function renderRepos(repos) {{
  const grid = document.querySelector("#repo-grid");
  grid.innerHTML = repos.map((repo) => `
    <article class="repo-card">
      <h2>${{repo.name}}</h2>
      <p>${{repo.description}}</p>
      <div class="repo-meta">
        <span>${{repo.language}}</span>
        <span>agent-ready</span>
      </div>
    </article>
  `).join("");
}}

async function refresh() {{
  renderRepos(await loadRepos());
}}

document.querySelector("#refresh-button").addEventListener("click", refresh);
refresh();
"""


def _backend_py(brand: str) -> str:
    title = "github-style-project-hub" if brand == "github" else "search-workspace"
    return f'''"""FastAPI backend generated by {SCAFFOLD_MARKER}."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="{title}")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

REPOS = [
    {{"name": "agent-harness", "description": "Distributed local-agent coordination.", "language": "Python"}},
    {{"name": "repo-preview", "description": "Visible frontend workspace artifacts.", "language": "JavaScript"}},
    {{"name": "mesh-coordinator", "description": "Token-gated machine roster and tasks.", "language": "FastAPI"}},
]


@app.get("/api/health")
def health() -> dict[str, str]:
    return {{"status": "ok"}}


@app.get("/api/repos")
def repos() -> list[dict[str, str]]:
    return REPOS
'''


def _readme(brand: str, goal: str) -> str:
    return f"""# Generated Workspace Scaffold

<!-- {SCAFFOLD_MARKER}: readme -->

Goal: {goal}

This scaffold was created because the selected local-agent CLI was not callable from the Chainlit process. It gives the workspace concrete code artifacts while you fix the CLI login/path.

Run:

```powershell
python scripts/preview_workspace.py --workspace default
```

Then open `http://localhost:5173`.
"""
