import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Cpu,
  GitBranch,
  ListChecks,
  Network,
  RadioTower,
  RefreshCw,
  RotateCw,
  Search,
  Settings,
  Users,
} from "lucide-react";

function action(name) {
  callAction({ name, payload: {} });
}

function MachineCard({ machine, orchestratorId }) {
  const isOrchestrator = machine.machine_id === orchestratorId;
  const status = machine.status_label || machine.status || "unknown";
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

export default function HarnessDashboard() {
  const machines = props.machines || [];
  const overview = props.overview || {};
  const workspace = props.workspace || {};
  const policy = props.policy || {};
  const repo = props.repo || {};
  const run = props.run || {};
  const goalRoles = policy.goal_roles || [];
  const tasks = run.tasks || [];
  const turns = run.turns || [];

  return (
    <div data-harness-dashboard="true" className="space-y-4 p-1">
      <section className="space-y-3">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold">Harness Dashboard</h2>
            <p className="text-xs leading-5 text-muted-foreground">
              Live machine roster, execution policy, and repo convergence while chat remains active.
            </p>
          </div>
          <Badge variant="outline">{overview.cluster_id || "local"}</Badge>
        </div>
        <div className="grid grid-cols-2 gap-2">
          <Stat label="Coordinator" value={overview.orchestrator_id || "unknown"} />
          <Stat label="Local backend" value={overview.local_backend || "simulated"} />
          <Stat label="Workspace" value={workspace.name || "default"} />
          <Stat label="Coordination" value={overview.coordination_backend || "file"} />
        </div>
      </section>

      <section className="space-y-2">
        <div className="flex items-center justify-between">
          <h3 className="flex items-center gap-2 text-sm font-semibold">
            <Users className="h-4 w-4" /> Machines
          </h3>
          <Button size="sm" variant="outline" onClick={() => action("refresh_dashboard")}>
            <RefreshCw className="mr-2 h-3.5 w-3.5" /> Refresh
          </Button>
        </div>
        <div className="grid gap-2">
          {machines.map((machine) => (
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

      {(run.goal || tasks.length > 0 || turns.length > 0) && (
        <section className="space-y-2">
          <h3 className="flex items-center gap-2 text-sm font-semibold">
            <ListChecks className="h-4 w-4" /> Run Activity
          </h3>
          <div className="rounded-md border bg-card p-3">
            <div className="grid gap-2 text-xs">
              {run.goal && (
                <div className="flex justify-between gap-3">
                  <span className="text-muted-foreground">Goal</span>
                  <strong className="max-w-[65%] truncate text-right">{run.goal}</strong>
                </div>
              )}
              {run.orchestrator_machine && (
                <div className="flex justify-between gap-3">
                  <span className="text-muted-foreground">Orchestrator</span>
                  <strong className="text-right">{run.orchestrator_machine}</strong>
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
        <Button variant="outline" onClick={() => action("host_coordinator")}>
          <RadioTower className="mr-2 h-4 w-4" /> Host
        </Button>
        <Button variant="outline" onClick={() => action("configure_http")}>
          <Network className="mr-2 h-4 w-4" /> Connect
        </Button>
        <Button variant="outline" onClick={() => action("show_backends")}>
          <Settings className="mr-2 h-4 w-4" /> Agents
        </Button>
        <Button variant="outline" onClick={() => action("auto_detect_agents")}>
          <Search className="mr-2 h-4 w-4" /> Detect
        </Button>
        <Button variant="outline" onClick={() => action("restart_app")}>
          <RotateCw className="mr-2 h-4 w-4" /> Restart
        </Button>
      </section>
    </div>
  );
}
