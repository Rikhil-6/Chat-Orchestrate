import { useEffect, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Cpu,
  Archive,
  ExternalLink,
  FileCode2,
  GitBranch,
  Handshake,
  ListChecks,
  LogOut,
  MessageSquareText,
  Network,
  Power,
  Plus,
  RadioTower,
  RefreshCw,
  RotateCw,
  Search,
  Settings,
  Users,
} from "lucide-react";

function action(name, payload = {}) {
  return callAction({ name, payload });
}

function cleanCurrentUrl() {
  return `${window.location.origin}${window.location.pathname || "/"}`;
}

function clearVisibleChatMessages() {
  document.querySelectorAll("article").forEach((node) => {
    if (!node.closest('[role="dialog"]')) {
      node.remove();
    }
  });
}

function reloadCleanChat() {
  window.sessionStorage.removeItem("chat-orchestrate:harness-dashboard-scroll");
  clearVisibleChatMessages();
  window.setTimeout(() => {
    window.location.replace(cleanCurrentUrl());
  }, 80);
}

async function resetToNewChat() {
  await action("new_chat", { silent: true });
  reloadCleanChat();
}

async function openSavedChat(chatId) {
  if (!chatId) return;
  await action("switch_chat", { chat_id: chatId, silent: true });
  reloadCleanChat();
}

async function archiveSavedChat(chatId) {
  if (!chatId) return;
  await action("archive_chat", { chat_id: chatId, silent: true });
  reloadCleanChat();
}

function findScrollParent(node) {
  let current = node ? node.parentElement : null;
  while (current) {
    const style = window.getComputedStyle(current);
    if (/(auto|scroll)/.test(`${style.overflowY}${style.overflow}`) && current.scrollHeight > current.clientHeight) {
      return current;
    }
    current = current.parentElement;
  }
  return document.scrollingElement || document.documentElement;
}

function MachineCard({ machine, orchestratorId }) {
  const isOrchestrator = machine.machine_id === orchestratorId;
  const status = machine.status_label || machine.status || "unknown";
  const assignments = machine.assignments || [];
  const completedAssignments = assignments.filter((task) => task.status === "completed").length;
  return (
    <Card className={isOrchestrator ? "border-primary/70" : ""}>
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <CardTitle className="truncate text-sm">{machine.machine_id}</CardTitle>
            <p className="mt-1 truncate text-xs text-muted-foreground">{machine.hostname}</p>
          </div>
          <Badge variant={isOrchestrator ? "default" : "secondary"}>
            {isOrchestrator ? "orchestrator" : machine.role}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid grid-cols-2 gap-2 text-xs">
          <div className="text-muted-foreground">Status</div>
          <div className="text-right font-medium">{status}</div>
          <div className="text-muted-foreground">Seen</div>
          <div className="text-right font-medium">{machine.seen_seconds}s ago</div>
        </div>
        <div className="flex flex-wrap gap-1.5">
          {(machine.agent_backends || []).length > 0 ? (
            (machine.agent_backends || []).map((backend) => (
              <Badge key={backend} variant="outline" className="text-[11px]">
                {backend}
              </Badge>
            ))
          ) : (
            <span className="text-[11px] text-muted-foreground">Agent backend advertises after selection</span>
          )}
        </div>
        {assignments.length > 0 && (
          <div className="space-y-1.5 rounded-md border border-primary/30 bg-primary/5 p-2">
            <div className="flex items-center justify-between gap-2 text-[11px] font-semibold text-primary">
              <span>Latest assignment</span>
              <span>{completedAssignments}/{assignments.length} done</span>
            </div>
            {assignments.map((task) => (
              <div key={task.task_id || `${task.role}-${task.status}`} className="grid gap-1">
                <div className="flex flex-wrap items-center gap-1.5">
                  <Badge variant={task.status === "running" ? "default" : "secondary"} className="text-[11px]">
                    {task.status}
                  </Badge>
                  <Badge variant="outline" className="text-[11px]">
                    {task.role}
                  </Badge>
                  <span className="text-[11px] text-muted-foreground">via {task.backend}</span>
                </div>
                <div className="truncate text-[11px] text-muted-foreground">{task.title}</div>
              </div>
            ))}
          </div>
        )}
        <div className="flex flex-wrap gap-1.5">
          {(machine.capabilities || []).length > 0 ? (
            (machine.capabilities || []).slice(0, 8).map((capability) => (
              <span
                key={capability}
                className="rounded-full bg-muted px-2 py-1 text-[11px] text-muted-foreground"
              >
                {capability}
              </span>
            ))
          ) : (
            <span className="text-[11px] text-muted-foreground">Role tags appear after a chat goal</span>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function Stat({ label, value }) {
  return (
    <div className="rounded-md border bg-card px-3 py-2">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className="mt-1 truncate text-sm font-semibold">{value}</div>
    </div>
  );
}

function SessionStatus({ overview }) {
  if (overview.hosting_live) {
    return (
      <div className="rounded-md border border-emerald-500/50 bg-emerald-500/10 p-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="text-sm font-semibold">Hosting live</div>
            <p className="mt-1 text-xs text-muted-foreground">
              This machine is the coordinator host for the current chat session.
            </p>
          </div>
          <Badge variant="outline">{overview.host_port || "live"}</Badge>
        </div>
        {(overview.host_urls || []).length > 0 && (
          <div className="mt-3 grid gap-1 text-[11px] text-muted-foreground">
            {(overview.host_urls || []).slice(0, 2).map((url) => (
              <div key={url} className="truncate rounded bg-background/70 px-2 py-1">
                {url}
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }
  if (overview.connected_to_http) {
    return (
      <div className="rounded-md border border-sky-500/50 bg-sky-500/10 p-3">
        <div className="text-sm font-semibold">Connected to coordinator</div>
        <p className="mt-1 text-xs text-muted-foreground">
          This session is joined to a hosted coordinator, so hosting is locked for this chat.
        </p>
        {overview.coordinator_url && (
          <div className="mt-3 truncate rounded bg-background/70 px-2 py-1 text-[11px] text-muted-foreground">
            {overview.coordinator_url}
          </div>
        )}
      </div>
    );
  }
  return null;
}

function ConnectionPanel({ overview, workspace }) {
  const [hostName, setHostName] = useState(workspace.name || "friends-project");
  const [connectionText, setConnectionText] = useState(overview.coordinator_url || "");
  const [busy, setBusy] = useState("");

  useEffect(() => {
    setHostName(workspace.name || "friends-project");
  }, [workspace.name]);

  useEffect(() => {
    if (overview.coordinator_url && !connectionText.trim()) {
      setConnectionText(overview.coordinator_url);
    }
  }, [overview.coordinator_url]);

  const runAction = async (name, payload = {}) => {
    setBusy(name);
    try {
      await action(name, payload);
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="rounded-md border bg-card p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-sm font-semibold">
            <Network className="h-4 w-4" /> Connection
          </div>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            Host a coordinator here, or join one running on another machine.
          </p>
        </div>
        <Badge variant={overview.hosting_live ? "default" : overview.connected_to_http ? "secondary" : "outline"}>
          {overview.hosting_live ? "hosting" : overview.connected_to_http ? "connected" : "local"}
        </Badge>
      </div>

      <div className="mt-3 grid gap-2 text-xs">
        <div className="flex justify-between gap-3">
          <span className="text-muted-foreground">Mode</span>
          <strong>{overview.coordination_backend || "file"}</strong>
        </div>
        {overview.token_set && (
          <div className="flex justify-between gap-3">
            <span className="text-muted-foreground">Token</span>
            <strong>set</strong>
          </div>
        )}
        {(overview.host_urls || []).length > 0 && (
          <div className="grid gap-1">
            <span className="text-muted-foreground">Host URLs</span>
            {(overview.host_urls || []).slice(0, 3).map((url) => (
              <div key={url} className="truncate rounded bg-muted/60 px-2 py-1" title={url}>
                {url}
              </div>
            ))}
          </div>
        )}
        {overview.connected_to_http && overview.coordinator_url && !overview.hosting_live && (
          <div className="grid gap-1">
            <span className="text-muted-foreground">Coordinator URL</span>
            <div className="truncate rounded bg-muted/60 px-2 py-1" title={overview.coordinator_url}>
              {overview.coordinator_url}
            </div>
          </div>
        )}
      </div>

      {!overview.connected_to_http && !overview.hosting_live && (
        <div className="mt-3 grid gap-2">
          <label className="grid gap-1 text-xs">
            <span className="text-muted-foreground">Host session name</span>
            <input
              className="h-9 min-w-0 rounded-md border border-input bg-background px-3 text-sm outline-none ring-offset-background focus:ring-2 focus:ring-ring"
              value={hostName}
              placeholder="friends-project"
              onChange={(event) => setHostName(event.target.value)}
            />
          </label>
        </div>
      )}

      {!overview.hosting_live && (
        <div className="mt-3 grid gap-2">
          <label className="grid gap-1 text-xs">
            <span className="text-muted-foreground">Connection pack or URL</span>
            <textarea
              className="min-h-16 min-w-0 rounded-md border border-input bg-background px-3 py-2 text-xs outline-none ring-offset-background focus:ring-2 focus:ring-ring"
              value={connectionText}
              placeholder="Paste Coordinator URL, or the full connection pack from the host"
              onChange={(event) => setConnectionText(event.target.value)}
            />
          </label>
        </div>
      )}

      <div className="mt-3 grid grid-cols-2 gap-2">
        {overview.hosting_live ? (
          <Button variant="destructive" disabled={busy === "end_host"} onClick={() => runAction("end_host")}>
            <Power className="mr-2 h-4 w-4" /> End Host
          </Button>
        ) : (
          <Button
            variant="outline"
            disabled={!overview.can_host || busy === "host_coordinator"}
            onClick={() => runAction("host_coordinator", { project_name: hostName })}
          >
            <RadioTower className="mr-2 h-4 w-4" /> {overview.connected_to_http ? "Hosting locked" : "Host"}
          </Button>
        )}
        <Button
          variant="outline"
          disabled={overview.hosting_live || busy === "configure_http"}
          onClick={() => runAction("configure_http", { connection_text: connectionText })}
        >
          <Network className="mr-2 h-4 w-4" /> Connect
        </Button>
        {overview.connected_to_http && !overview.hosting_live && (
          <Button className="col-span-2" variant="outline" onClick={() => runAction("end_session")}>
            <LogOut className="mr-2 h-4 w-4" /> End Session
          </Button>
        )}
      </div>
    </div>
  );
}

function ChatSwitcher({ chats }) {
  const threads = chats.threads || [];
  const activeId = chats.active_id || (threads[0] && threads[0].id) || "";
  const activeThread = threads.find((thread) => thread.id === activeId) || threads[0];
  const preview = activeThread && activeThread.preview ? activeThread.preview : "";
  const title = activeThread && activeThread.title ? activeThread.title : "";
  const showPreview =
    preview &&
    preview.toLowerCase() !== title.toLowerCase() &&
    !preview.toLowerCase().startsWith(title.toLowerCase().replace(/\.\.\.$/, ""));

  return (
    <div className="rounded-md border bg-card p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-sm font-semibold">
            <MessageSquareText className="h-4 w-4" /> Chats
          </div>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            Local history is saved per chat on this machine.
          </p>
        </div>
        {Number(chats.archived_count || 0) > 0 && (
          <Badge variant="outline">{chats.archived_count} archived</Badge>
        )}
      </div>
      <div className="mt-3 grid gap-2">
        <select
          className="h-9 min-w-0 rounded-md border border-input bg-background px-3 text-sm outline-none ring-offset-background focus:ring-2 focus:ring-ring"
          value={activeId}
          onChange={(event) => openSavedChat(event.target.value)}
        >
          {threads.length === 0 ? (
            <option value="">New chat</option>
          ) : (
            threads.map((thread) => (
              <option key={thread.id} value={thread.id}>
                {thread.title} ({thread.message_count})
              </option>
            ))
          )}
        </select>
        {showPreview && (
          <div className="truncate text-[11px] text-muted-foreground" title={preview}>
            {preview}
          </div>
        )}
        <div className="grid grid-cols-3 gap-2">
          <Button size="sm" variant="outline" onClick={resetToNewChat}>
            <Plus className="mr-2 h-3.5 w-3.5" /> New
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={!activeId}
            onClick={() => action("restore_chat", { chat_id: activeId })}
          >
            <RefreshCw className="mr-2 h-3.5 w-3.5" /> Restore
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={!activeId}
            onClick={() => archiveSavedChat(activeId)}
          >
            <Archive className="mr-2 h-3.5 w-3.5" /> Archive
          </Button>
        </div>
      </div>
    </div>
  );
}

function ProjectSpaceCard({ workspace }) {
  const [draftName, setDraftName] = useState(workspace.name || "default");
  const [isSaving, setIsSaving] = useState(false);
  const [saveError, setSaveError] = useState("");

  useEffect(() => {
    setDraftName(workspace.name || "default");
  }, [workspace.name]);

  const saveProject = async () => {
    const cleanName = draftName.trim();
    if (!cleanName) {
      setSaveError("Add a project name first.");
      return;
    }
    setIsSaving(true);
    setSaveError("");
    try {
      await action("save_project_space", { project_name: cleanName, silent: true });
    } catch (error) {
      setSaveError("Project name was not saved. Try again.");
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <div className="rounded-md border bg-card p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-sm font-semibold">Project Space</div>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            Set the active project name before asking agents to write code.
          </p>
        </div>
        <Button size="sm" variant="outline" disabled={isSaving} onClick={saveProject}>
          {isSaving ? "Saving" : "Save"}
        </Button>
      </div>
      <div className="mt-3 grid gap-2">
        <input
          className="h-9 min-w-0 rounded-md border border-input bg-background px-3 text-sm outline-none ring-offset-background focus:ring-2 focus:ring-ring"
          value={draftName}
          placeholder="my-project"
          onChange={(event) => setDraftName(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") saveProject();
          }}
        />
        {saveError && <div className="text-[11px] text-destructive">{saveError}</div>}
      </div>
      <div className="mt-3 grid min-w-0 gap-2 text-xs">
        <div className="flex min-w-0 justify-between gap-3">
          <span className="shrink-0 text-muted-foreground">Folder</span>
          <strong className="min-w-0 max-w-[62%] truncate text-right" title={workspace.path || ""}>
            {workspace.path || "workspaces/default"}
          </strong>
        </div>
        <div className="flex min-w-0 justify-between gap-3">
          <span className="text-muted-foreground">Mode</span>
          <strong className="min-w-0 truncate text-right">{workspace.mode || "local"}</strong>
        </div>
        {workspace.branch && (
          <div className="flex min-w-0 justify-between gap-3">
            <span className="text-muted-foreground">Branch</span>
            <strong className="min-w-0 max-w-[62%] truncate text-right">{workspace.branch}</strong>
          </div>
        )}
      </div>
    </div>
  );
}

function FlowCard({ title, body, accent }) {
  return (
    <div className="rounded-md border bg-card p-3">
      <div className="flex items-center gap-2">
        <span className={`h-2 w-2 rounded-full ${accent || "bg-primary"}`} />
        <div className="text-sm font-semibold">{title}</div>
      </div>
      <p className="mt-2 text-xs leading-5 text-muted-foreground">{body}</p>
    </div>
  );
}

function EmptyPlan({ repo }) {
  return (
    <div className="rounded-md border border-dashed bg-card p-3">
      <div className="text-sm font-semibold">No delegation plan yet</div>
      <p className="mt-2 text-xs leading-5 text-muted-foreground">
        Connect machines, pick the local agent backend, and choose the project workspace. The next chat task will
        decide which roles are needed, where they should run, and how repo outputs should converge.
      </p>
      <div className="mt-3 grid gap-2 text-xs">
        <div className="flex justify-between gap-3">
          <span className="text-muted-foreground">Worker outputs</span>
          <strong className="text-right">{repo.worker_outputs}</strong>
        </div>
        <div className="flex justify-between gap-3">
          <span className="text-muted-foreground">Canonical repo</span>
          <strong className="text-right">{repo.canonical}</strong>
        </div>
      </div>
    </div>
  );
}

function EvaluationPanel({ evaluation }) {
  if (!evaluation) return null;
  const stats = evaluation.task_stats || {};
  const total = Number(stats.total || 0);
  const completed = Number(stats.completed || 0);
  return (
    <div className="rounded-md border bg-muted/30 p-2">
      <div className="mb-2 text-xs font-semibold">Build evaluation</div>
      <div className="grid gap-1.5 text-[11px]">
        <div className="flex items-start justify-between gap-3">
          <span className="text-muted-foreground">Frontend</span>
          <span className="max-w-[66%] text-right">
            <Badge variant={evaluation.frontend_status === "ready" ? "default" : "secondary"} className="mr-1 text-[10px]">
              {evaluation.frontend_status || "missing"}
            </Badge>
            {evaluation.frontend_files?.length ? evaluation.frontend_files.join(", ") : "waiting for files"}
          </span>
        </div>
        <div className="flex items-start justify-between gap-3">
          <span className="text-muted-foreground">Backend</span>
          <span className="max-w-[66%] text-right">
            <Badge variant={evaluation.backend_status === "ready" ? "default" : "secondary"} className="mr-1 text-[10px]">
              {evaluation.backend_status || "missing"}
            </Badge>
            {evaluation.backend_files?.length ? evaluation.backend_files.join(", ") : "waiting for files"}
          </span>
        </div>
        {total > 0 && (
          <div className="flex items-center justify-between gap-3">
            <span className="text-muted-foreground">Agent tasks</span>
            <strong>{completed}/{total} completed</strong>
          </div>
        )}
      </div>
    </div>
  );
}

function ArtifactList({ repo }) {
  const artifacts = repo.artifacts || [];
  if (!artifacts.length) {
    return (
      <div className="grid gap-2 rounded-md border border-dashed bg-card p-3">
        <div className="text-sm font-semibold">No generated files surfaced yet</div>
        <p className="mt-2 text-xs leading-5 text-muted-foreground">
          Once an agent writes files inside the active project space, they will appear here with preview paths.
        </p>
        <EvaluationPanel evaluation={repo.evaluation} />
      </div>
    );
  }

  return (
    <div className="rounded-md border bg-card p-3">
      <EvaluationPanel evaluation={repo.evaluation} />
      <div className="mt-3 grid gap-2 text-xs">
        <div className="flex justify-between gap-3">
          <span className="text-muted-foreground">Workspace</span>
          <strong className="max-w-[62%] truncate text-right">{repo.code_path || "workspaces/default"}</strong>
        </div>
        <div className="flex justify-between gap-3">
          <span className="text-muted-foreground">Preview</span>
          <strong className="max-w-[62%] truncate text-right">{repo.preview_command || "python scripts/preview_workspace.py"}</strong>
        </div>
      </div>
      <div className="mt-3 grid gap-1.5">
        {artifacts.slice(0, 8).map((artifact) => (
          <div key={artifact.relative_path} className="rounded-md bg-muted/60 px-2 py-1.5">
            <div className="flex items-center justify-between gap-2 text-xs">
              <strong className="truncate">{artifact.label || artifact.relative_path}</strong>
              <span className="shrink-0 text-muted-foreground">{artifact.kind}</span>
            </div>
            <div className="mt-1 flex items-center justify-between gap-2 text-[11px] text-muted-foreground">
              <span className="truncate">{artifact.relative_path}</span>
              {artifact.preview_url && (
                <a
                  href={artifact.preview_url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex shrink-0 items-center gap-1 text-primary"
                >
                  <ExternalLink className="h-3 w-3" /> preview
                </a>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function HarnessDashboard() {
  const rootRef = useRef(null);
  const refreshing = useRef(false);
  const mountedAt = useRef(Date.now());
  const [tick, setTick] = useState(Date.now());
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState("");
  const machines = props.machines || [];
  const overview = props.overview || {};
  const workspace = props.workspace || {};
  const policy = props.policy || {};
  const repo = props.repo || {};
  const run = props.run || {};
  const chats = props.chats || {};
  const goalLabel = run.goal_summary || run.goal || "";
  const goalRoles = policy.goal_roles || [];
  const tasks = run.tasks || [];
  const taskStats = run.task_stats || {};
  const taskTotal = Number(taskStats.total || 0);
  const taskCompleted = Number(taskStats.completed || 0);
  const taskRunning = Number(taskStats.running || 0);
  const taskFailed = Number(taskStats.failed || 0);
  const turns = run.turns || [];
  const elapsedSinceSync = Math.max(0, Math.floor((tick - mountedAt.current) / 1000));
  const displayMachines = machines.map((machine) => ({
    ...machine,
    seen_seconds: Number(machine.seen_seconds || 0) + elapsedSinceSync,
  }));

  const saveScrollPosition = () => {
    const scrollParent = findScrollParent(rootRef.current);
    window.sessionStorage.setItem("chat-orchestrate:harness-dashboard-scroll", String(scrollParent.scrollTop || 0));
  };

  const refreshDashboard = async () => {
    if (refreshing.current) return;
    refreshing.current = true;
    saveScrollPosition();
    setIsRefreshing(true);
    setRefreshError("");
    try {
      await action("refresh_dashboard");
    } catch (error) {
      setRefreshError("Refresh could not reach the local UI server. The launcher will bring localhost back if it crashed.");
    } finally {
      refreshing.current = false;
      setIsRefreshing(false);
    }
  };

  useEffect(() => {
    mountedAt.current = Date.now();
    setTick(Date.now());
  }, [overview.last_refreshed, machines.length]);

  useEffect(() => {
    const interval = window.setInterval(() => setTick(Date.now()), 1000);
    return () => window.clearInterval(interval);
  }, []);

  useEffect(() => {
    const scrollParent = findScrollParent(rootRef.current);
    const storedPosition = Number(window.sessionStorage.getItem("chat-orchestrate:harness-dashboard-scroll") || 0);
    if (storedPosition > 0) {
      window.requestAnimationFrame(() => {
        scrollParent.scrollTop = storedPosition;
      });
    }
    const save = () => {
      window.sessionStorage.setItem("chat-orchestrate:harness-dashboard-scroll", String(scrollParent.scrollTop || 0));
    };
    scrollParent.addEventListener("scroll", save, { passive: true });
    return () => {
      save();
      scrollParent.removeEventListener("scroll", save);
    };
  }, []);

  return (
    <div ref={rootRef} data-harness-dashboard="true" className="space-y-4 p-1">
      <section className="space-y-3">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold">Harness Dashboard</h2>
            <p className="text-xs leading-5 text-muted-foreground">
              Live machine roster, execution policy, and repo convergence while chat remains active.
            </p>
          </div>
          <div className="flex flex-col items-end gap-1">
            <Badge variant="outline">{overview.cluster_id || "local"}</Badge>
            {overview.last_refreshed && (
              <span className="text-[10px] text-muted-foreground">synced {overview.last_refreshed}</span>
            )}
          </div>
        </div>
        {refreshError && (
          <div className="rounded-md border border-amber-500/50 bg-amber-500/10 px-3 py-2 text-xs text-amber-100">
            {refreshError}
          </div>
        )}
        <div className="grid grid-cols-2 gap-2">
          <Stat label="Coordinator" value={overview.orchestrator_id || "unknown"} />
          <Stat label="Local backend" value={overview.local_backend || "simulated"} />
          <Stat label="Workspace" value={workspace.name || "default"} />
          <Stat label="Coordination" value={overview.coordination_backend || "file"} />
        </div>
        <ConnectionPanel overview={overview} workspace={workspace} />
        <ProjectSpaceCard workspace={workspace} />
        <ChatSwitcher chats={chats} />
      </section>

      <section className="space-y-2">
        <h3 className="flex items-center gap-2 text-sm font-semibold">
          <FileCode2 className="h-4 w-4" /> Project Artifacts
        </h3>
        <ArtifactList repo={repo} />
      </section>

      <section className="space-y-2">
        <div className="flex items-center justify-between">
          <h3 className="flex items-center gap-2 text-sm font-semibold">
            <Users className="h-4 w-4" /> Machines
          </h3>
          <Button size="sm" variant="outline" disabled={isRefreshing} onClick={refreshDashboard}>
            <RefreshCw className={`mr-2 h-3.5 w-3.5 ${isRefreshing ? "animate-spin" : ""}`} />
            {isRefreshing ? "Refreshing" : "Refresh"}
          </Button>
        </div>
        <div className="grid gap-2">
          {displayMachines.map((machine) => (
            <MachineCard key={machine.machine_id} machine={machine} orchestratorId={overview.orchestrator_id} />
          ))}
        </div>
      </section>

      <section className="space-y-2">
        <h3 className="flex items-center gap-2 text-sm font-semibold">
          <Cpu className="h-4 w-4" /> Execution Policy
        </h3>
        <div className="rounded-md border bg-card p-3">
          <div className="grid gap-2 text-xs">
            <div className="flex justify-between gap-3">
              <span className="text-muted-foreground">Selected agent</span>
              <strong>
                {policy.selected_backend || "auto"} / {policy.selected_ready ? "ready" : "setup needed"}
              </strong>
            </div>
            <div className="flex justify-between gap-3">
              <span className="text-muted-foreground">Advertised tags</span>
              <strong className="text-right">
                {(policy.capabilities || []).length ? (policy.capabilities || []).slice(0, 5).join(", ") : "waiting for task"}
              </strong>
            </div>
            <div className="flex justify-between gap-3">
              <span className="text-muted-foreground">Goal roles</span>
              <strong className="text-right">
                {goalRoles.length ? goalRoles.slice(0, 5).join(", ") : "waiting for task"}
              </strong>
            </div>
          </div>
          <p className="mt-3 text-xs leading-5 text-muted-foreground">
            Roles are inferred from the latest chat task and available machines; defaults only describe what can run.
          </p>
        </div>
      </section>

      <section className="space-y-2">
        <h3 className="flex items-center gap-2 text-sm font-semibold">
          <Handshake className="h-4 w-4" /> Agent2Agent
        </h3>
        <div className="rounded-md border bg-card p-3">
          <div className="grid gap-2 text-xs">
            <div className="flex justify-between gap-3">
              <span className="text-muted-foreground">Status</span>
              <strong>{overview.a2a_enabled ? `A2A ${overview.a2a_version || "1.0"}` : "available when hosted"}</strong>
            </div>
            <div className="flex justify-between gap-3">
              <span className="text-muted-foreground">Agent card</span>
              <strong className="max-w-[65%] truncate text-right">
                {overview.a2a_agent_card_url || "start or join host"}
              </strong>
            </div>
            <div className="flex justify-between gap-3">
              <span className="text-muted-foreground">RPC</span>
              <strong className="max-w-[65%] truncate text-right">{overview.a2a_rpc_url || "not bound"}</strong>
            </div>
          </div>
          <p className="mt-3 text-xs leading-5 text-muted-foreground">
            A2A is an interoperability lane for external agent harnesses; cluster membership and tokens still come from
            this coordinator session.
          </p>
        </div>
      </section>

      {(run.goal || tasks.length > 0 || turns.length > 0) && (
        <section className="space-y-2">
          <h3 className="flex items-center gap-2 text-sm font-semibold">
            <ListChecks className="h-4 w-4" /> Run Activity
          </h3>
          <div className="rounded-md border bg-card p-3">
            <div className="grid gap-2 text-xs">
              {goalLabel && (
                <div className="grid gap-1">
                  <span className="text-muted-foreground">Goal</span>
                  <strong className="text-left leading-4" title={run.goal || goalLabel}>
                    {goalLabel}
                  </strong>
                </div>
              )}
              {run.orchestrator_machine && (
                <div className="flex justify-between gap-3">
                  <span className="text-muted-foreground">Orchestrator</span>
                  <strong className="text-right">{run.orchestrator_machine}</strong>
                </div>
              )}
              {taskTotal > 0 && (
                <div className="grid gap-1">
                  <div className="flex justify-between gap-3">
                    <span className="text-muted-foreground">Agent tasks</span>
                    <strong className="text-right">
                      {taskCompleted}/{taskTotal} completed
                    </strong>
                  </div>
                  <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                    <div
                      className="h-full rounded-full bg-primary"
                      style={{ width: `${Math.min(100, Math.round((taskCompleted / taskTotal) * 100))}%` }}
                    />
                  </div>
                  {(taskRunning > 0 || taskFailed > 0) && (
                    <div className="text-[11px] text-muted-foreground">
                      {taskRunning > 0 ? `${taskRunning} running` : ""}
                      {taskRunning > 0 && taskFailed > 0 ? " / " : ""}
                      {taskFailed > 0 ? `${taskFailed} failed` : ""}
                    </div>
                  )}
                </div>
              )}
            </div>
            {tasks.length > 0 && (
              <div className="mt-3 grid gap-1.5">
                {tasks.slice(-5).map((task, index) => (
                  <div key={`${task.role}-${task.machine}-${index}`} className="rounded-md bg-muted/60 px-2 py-1.5">
                    <div className="flex items-center justify-between gap-2 text-xs">
                      <strong>{task.role}</strong>
                      <span className="text-muted-foreground">{task.status}</span>
                    </div>
                    <div className="mt-1 truncate text-[11px] text-muted-foreground">
                      {task.machine} / {task.backend}
                    </div>
                  </div>
                ))}
              </div>
            )}
            {tasks.length === 0 && turns.length > 0 && (
              <div className="mt-3 grid gap-1.5">
                {turns.slice(-4).map((turn, index) => (
                  <div key={`${turn.agent}-${index}`} className="rounded-md bg-muted/60 px-2 py-1.5">
                    <div className="flex items-center justify-between gap-2 text-xs">
                      <strong>{turn.agent}</strong>
                      <span className="text-muted-foreground">{turn.backend || "local"}</span>
                    </div>
                    <div className="mt-1 truncate text-[11px] text-muted-foreground">{turn.summary}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </section>
      )}

      <section className="space-y-2">
        <h3 className="flex items-center gap-2 text-sm font-semibold">
          <GitBranch className="h-4 w-4" /> Repo Consolidation
        </h3>
        {repo.has_goal ? (
          <div className="grid gap-2">
            <FlowCard
              title="Worker outputs"
              body={repo.worker_outputs || "branches, patches, preview URLs, screenshots"}
              accent="bg-sky-400"
            />
            <FlowCard
              title="Canonical repo"
              body={repo.canonical || "origin/main in the selected project workspace"}
              accent="bg-emerald-400"
            />
            <FlowCard
              title="Coordinator merge"
              body={repo.merge || "pull, test, resolve conflicts, merge, document"}
              accent="bg-amber-400"
            />
          </div>
        ) : (
          <EmptyPlan repo={repo} />
        )}
      </section>

      <section className="grid grid-cols-2 gap-2">
        <Button variant="outline" onClick={() => action("show_backends")}>
          <Settings className="mr-2 h-4 w-4" /> Agents
        </Button>
        <Button variant="outline" onClick={() => action("set_project_space")}>
          <FileCode2 className="mr-2 h-4 w-4" /> Project
        </Button>
        <Button variant="outline" onClick={() => action("auto_detect_agents")}>
          <Search className="mr-2 h-4 w-4" /> Detect
        </Button>
        <Button variant="outline" onClick={() => action("restart_app")}>
          <RotateCw className="mr-2 h-4 w-4" /> Restart
        </Button>
        {overview.connected_to_http && !overview.hosting_live && (
          <Button variant="outline" onClick={() => action("end_session")}>
            <LogOut className="mr-2 h-4 w-4" /> End Session
          </Button>
        )}
      </section>
    </div>
  );
}
