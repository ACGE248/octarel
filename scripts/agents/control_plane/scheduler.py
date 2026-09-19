"""Dependency-safe task queue and concurrency policy.

The scheduler never launches anything itself (that is ``supervisor.py``'s
job) — it only decides, given the current durable task set, which tasks are
currently runnable: dependencies satisfied and a concurrency slot free for
the task's kind (``write`` / ``read`` / ``heavy``).
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import (
    DEPENDENCY_SATISFIED_STATES,
    KIND_HEAVY,
    KIND_READ,
    KIND_WRITE,
    TASK_PENDING,
    TASK_QUEUED,
    TASK_RUNNING,
    Task,
)

DEFAULT_MAX_WRITE_WORKERS = 2
DEFAULT_MAX_EXTRA_READ_WORKERS = 1
DEFAULT_MAX_HEAVY_JOBS = 1
DEFAULT_MAX_GLOBAL_WORKERS = 4


@dataclass(frozen=True)
class ConcurrencyPolicy:
    max_global_workers: int = DEFAULT_MAX_GLOBAL_WORKERS
    max_write_workers: int = DEFAULT_MAX_WRITE_WORKERS
    max_extra_read_workers: int = DEFAULT_MAX_EXTRA_READ_WORKERS
    max_heavy_jobs: int = DEFAULT_MAX_HEAVY_JOBS
    max_per_provider: int = DEFAULT_MAX_GLOBAL_WORKERS
    provider_limits: dict[str, int] | None = None

    def limit_for(self, kind: str) -> int:
        return {
            KIND_WRITE: self.max_write_workers,
            KIND_READ: self.max_extra_read_workers,
            KIND_HEAVY: self.max_heavy_jobs,
        }[kind]

    def provider_limit(self, provider: str) -> int:
        return (self.provider_limits or {}).get(provider, self.max_per_provider)


@dataclass(frozen=True)
class SlotUsage:
    global_used: int
    global_limit: int
    write_used: int
    write_limit: int
    read_used: int
    read_limit: int
    heavy_used: int
    heavy_limit: int
    provider_used: dict[str, int]

    def as_dict(self) -> dict[str, dict[str, int]]:
        return {
            "global": {"used": self.global_used, "limit": self.global_limit},
            "write": {"used": self.write_used, "limit": self.write_limit},
            "read": {"used": self.read_used, "limit": self.read_limit},
            "heavy": {"used": self.heavy_used, "limit": self.heavy_limit},
            "providers": dict(self.provider_used),
        }


@dataclass(frozen=True)
class AdmissionFact:
    task_id: str
    wave: int | None
    runnable: bool
    reason: str


class Scheduler:
    def __init__(self, policy: ConcurrencyPolicy | None = None) -> None:
        self.policy = policy or ConcurrencyPolicy()

    def slot_usage(self, tasks: list[Task]) -> SlotUsage:
        running = [t for t in tasks if t.state == TASK_RUNNING]
        counts = {KIND_WRITE: 0, KIND_READ: 0, KIND_HEAVY: 0}
        for task in running:
            counts[task.kind] = counts.get(task.kind, 0) + 1
        provider_used: dict[str, int] = {}
        for task in running:
            provider = task.selected_provider or task.worker
            provider_used[provider] = provider_used.get(provider, 0) + 1
        return SlotUsage(
            global_used=len(running),
            global_limit=self.policy.max_global_workers,
            write_used=counts[KIND_WRITE],
            write_limit=self.policy.max_write_workers,
            read_used=counts[KIND_READ],
            read_limit=self.policy.max_extra_read_workers,
            heavy_used=counts[KIND_HEAVY],
            heavy_limit=self.policy.max_heavy_jobs,
            provider_used=provider_used,
        )

    def dependencies_satisfied(self, task: Task, tasks_by_id: dict[str, Task]) -> bool:
        for dep_id in task.dependencies:
            dep = tasks_by_id.get(dep_id)
            if dep is None or dep.state not in DEPENDENCY_SATISFIED_STATES:
                return False
        return True

    def has_free_slot(self, kind: str, usage: SlotUsage) -> bool:
        limit = self.policy.limit_for(kind)
        used = {KIND_WRITE: usage.write_used, KIND_READ: usage.read_used, KIND_HEAVY: usage.heavy_used}[kind]
        return usage.global_used < usage.global_limit and used < limit

    def dependency_wave(self, task: Task, tasks_by_id: dict[str, Task]) -> int | None:
        """Return the deterministic DAG wave, or ``None`` for missing/cyclic dependencies."""

        visiting: set[str] = set()
        memo: dict[str, int | None] = {}

        def visit(current: Task) -> int | None:
            if current.id in memo:
                return memo[current.id]
            if current.id in visiting:
                memo[current.id] = None
                return None
            visiting.add(current.id)
            parent_waves: list[int] = []
            for dep_id in current.dependencies:
                dep = tasks_by_id.get(dep_id)
                if dep is None:
                    visiting.discard(current.id)
                    memo[current.id] = None
                    return None
                wave = visit(dep)
                if wave is None:
                    visiting.discard(current.id)
                    memo[current.id] = None
                    return None
                parent_waves.append(wave)
            visiting.discard(current.id)
            memo[current.id] = 0 if not parent_waves else max(parent_waves) + 1
            return memo[current.id]

        return visit(task)

    @staticmethod
    def _paths_overlap(left: Task, right: Task) -> bool:
        for a in left.changed_paths:
            a = a.strip("/")
            for b in right.changed_paths:
                b = b.strip("/")
                if a and b and (a == b or a.startswith(f"{b}/") or b.startswith(f"{a}/")):
                    return True
        return False

    def block_reason(self, task: Task, tasks: list[Task], *, reserved: list[Task] = ()) -> str | None:
        tasks_by_id = {item.id: item for item in tasks}
        if self.dependency_wave(task, tasks_by_id) is None:
            return "dependency graph is missing a task or contains a cycle"
        unsatisfied = [
            dep_id
            for dep_id in task.dependencies
            if tasks_by_id[dep_id].state not in DEPENDENCY_SATISFIED_STATES
        ]
        if unsatisfied:
            return f"waiting for dependencies: {', '.join(sorted(unsatisfied))}"
        running = [item for item in tasks if item.state == TASK_RUNNING]
        usage = self.slot_usage(tasks)
        if usage.global_used + len(reserved) >= self.policy.max_global_workers:
            return f"global concurrency cap reached ({self.policy.max_global_workers})"
        kind_reserved = sum(1 for item in reserved if item.kind == task.kind)
        kind_used = {KIND_WRITE: usage.write_used, KIND_READ: usage.read_used, KIND_HEAVY: usage.heavy_used}[task.kind]
        if kind_used + kind_reserved >= self.policy.limit_for(task.kind):
            return f"{task.kind} concurrency cap reached ({self.policy.limit_for(task.kind)})"
        provider_name = task.selected_provider or task.worker
        provider_reserved = sum(1 for item in reserved if (item.selected_provider or item.worker) == provider_name)
        provider_used = usage.provider_used.get(provider_name, 0)
        provider_limit = self.policy.provider_limit(provider_name)
        if provider_used + provider_reserved >= provider_limit:
            return f"provider cap reached for {provider_name} ({provider_limit})"
        if task.kind == KIND_WRITE and task.worktree:
            same_tree = [
                item.id
                for item in (*running, *reserved)
                if item.kind == KIND_WRITE and item.worktree == task.worktree and item.id != task.id
            ]
            if same_tree:
                return f"worktree already owned by write task {sorted(same_tree)[0]}"
            overlapping = [
                item.id
                for item in (*running, *reserved)
                if item.kind == KIND_WRITE
                and item.id != task.id
                and self._paths_overlap(task, item)
                and item.id not in task.dependencies
            ]
            if overlapping:
                return (
                    "parallel branch integration overlap requires dependency/reconciliation: "
                    + ", ".join(sorted(overlapping))
                )
        return None

    def admission_facts(self, tasks: list[Task]) -> list[AdmissionFact]:
        tasks_by_id = {task.id: task for task in tasks}
        reserved: list[Task] = []
        facts: list[AdmissionFact] = []
        candidates = [task for task in tasks if task.state in (TASK_PENDING, TASK_QUEUED)]
        candidates.sort(key=lambda task: (-task.priority, task.created_at, task.id))
        for task in candidates:
            reason = self.block_reason(task, tasks, reserved=reserved)
            runnable = reason is None
            if runnable:
                reserved.append(task)
            facts.append(
                AdmissionFact(
                    task_id=task.id,
                    wave=self.dependency_wave(task, tasks_by_id),
                    runnable=runnable,
                    reason=reason or "dependency wave and concurrency gates satisfied",
                )
            )
        return facts

    def next_runnable(self, tasks: list[Task]) -> list[Task]:
        """Tasks that are dependency-clear and have a free concurrency slot now.

        Ordered by priority (desc) then creation time (asc, i.e. FIFO within a
        priority band). Slots are reserved greedily against this same list so
        two heavy tasks are never both returned when only one heavy slot free.
        """

        by_id = {task.id: task for task in tasks}
        return [by_id[fact.task_id] for fact in self.admission_facts(tasks) if fact.runnable]
