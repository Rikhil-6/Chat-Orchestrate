from chat_orchestrate.api_harness import apply_api_harness_response, build_api_harness_prompt
from chat_orchestrate.models import ProjectSpace


def test_api_harness_applies_file_manifest_inside_workspace(tmp_path):
    project = ProjectSpace("demo", tmp_path / "demo")
    response = """Implemented the theme toggle.

```chat-orchestrate-files
{
  "summary": "Adds a theme toggle.",
  "files": [
    {
      "path": "frontend/app.js",
      "content": "document.body.dataset.theme = 'dark';\\n"
    }
  ],
  "commands": ["python scripts/preview_workspace.py --workspace demo"]
}
```
"""

    result = apply_api_harness_response(project, response)

    assert (project.path / "frontend" / "app.js").read_text(encoding="utf-8") == "document.body.dataset.theme = 'dark';\n"
    assert "Applied Workspace Changes" in result.content
    assert "`frontend/app.js`" in result.content
    assert len(result.applied_files) == 1


def test_api_harness_rejects_paths_outside_workspace(tmp_path):
    project = ProjectSpace("demo", tmp_path / "demo")
    response = """Nope.

```chat-orchestrate-files
{"files":[{"path":"../escape.txt","content":"bad"}]}
```
"""

    result = apply_api_harness_response(project, response)

    assert not (tmp_path / "escape.txt").exists()
    assert result.applied_files == []


def test_api_harness_applies_markdown_file_blocks(tmp_path):
    project = ProjectSpace("demo", tmp_path / "demo")
    response = """I'll update the UI file.

1) `frontend/app.js`

```js
document.body.textContent = "done";
```

2) frontend/styles.css

```css
body { color: red; }
```
"""

    result = apply_api_harness_response(project, response)

    assert (project.path / "frontend" / "app.js").read_text(encoding="utf-8") == 'document.body.textContent = "done";\n'
    assert (project.path / "frontend" / "styles.css").read_text(encoding="utf-8") == "body { color: red; }\n"
    assert "Applied Workspace Changes" in result.content
    assert "document.body.textContent" not in result.content
    assert len(result.applied_files) == 2


def test_api_harness_prompt_includes_workspace_snapshot(tmp_path):
    project = ProjectSpace("demo", tmp_path / "demo")
    (project.path / "frontend").mkdir(parents=True)
    (project.path / "frontend" / "index.html").write_text("<main>old</main>", encoding="utf-8")

    prompt = build_api_harness_prompt("base prompt", project)

    assert "local harness that can write files" in prompt
    assert "frontend/index.html" in prompt
    assert "<main>old</main>" in prompt
