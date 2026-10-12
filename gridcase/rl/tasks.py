"""M15 tasks: spike_hold and resolve tasks from the backfill, split by time and news cluster.

Definitions (FEASIBILITY.md §3, unchanged):
  spike at t   |p(t) - p(t - Y)| >= X points on a 10-minute grid (last price carried forward),
               base p(t - Y) in [0.05, 0.95], t + horizon before the market's close (labels never
               see the resolution); after a spike the market is skipped for max(Y, cooldown).
  retained(H)  (p(t + H) - p(t - Y)) / (p(t) - p(t - Y)); a spike "held" if retained(72h) >= 0.5.
  event cluster  spikes in markets of the same event within 6 h.
  news cluster   event clusters joined when spikes in different events are within 2 h and the
                 events share a specific tag (on <= 2% of events, or fewer than 3).
  resolve      at close - 24 h and close - 7 d, when the price is in [0.05, 0.95] and Gamma's
               winner agrees with /v2/resolutions; the answer is whether outcome 0 won.

Splits are by as_of with purge gaps (train, validation, eval); a task whose label window passes
its split's end is dropped, and so is any cluster that would span two splits. The eval and
validation manifests carry no answers; answers.sha256 pins the answers file.
"""

from __future__ import annotations

import bisect
import hashlib
import json
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import duckdb
from pydantic import BaseModel

from gridcase.backfill.fetch import life

Kind = Literal["spike_hold", "resolve"]
Split = Literal["train", "validation", "eval"]
HOUR = 3600


class Task(BaseModel):
    task_id: str
    kind: Kind
    condition_id: str
    as_of: datetime  # naive UTC
    facts: dict[str, Any]  # what the prompt states; known at as_of
    cluster_id: str
    split: Split
    category: str


class Answer(BaseModel):
    task_id: str
    y: float  # 1.0 / 0.0
    detail: dict[str, Any]


@dataclass(frozen=True)
class SpikeParams:
    x_pts: float = 10.0
    y_hours: int = 6
    horizon_hours: int = 72
    cooldown_hours: int = 24
    base_lo: float = 0.05
    base_hi: float = 0.95
    grid_s: int = 600


DEFAULT_SPIKES = SpikeParams()


@dataclass(frozen=True)
class Spike:
    t: int
    base: float
    now: float
    retained24: float | None
    retained72: float | None

    @property
    def move(self) -> float:
        return self.now - self.base

    @property
    def held(self) -> bool:
        return self.retained72 is not None and self.retained72 >= 0.5


def _at(ts: list[int], ps: list[float], t: float) -> float | None:
    i = bisect.bisect_right(ts, t) - 1
    return ps[i] if i >= 0 else None


def detect_spikes(series: list[tuple[int, float]], close: int, p: SpikeParams) -> list[Spike]:
    if not series:
        return []
    ts = [a for a, _ in series]
    ps = [b for _, b in series]
    y = p.y_hours * HOUR
    t = (ts[0] // p.grid_s + 1) * p.grid_s + y
    last = min(close, ts[-1]) - p.horizon_hours * HOUR
    out: list[Spike] = []
    while t <= last:
        base, now = _at(ts, ps, t - y), _at(ts, ps, t)
        if (
            base is not None
            and now is not None
            and p.base_lo <= base <= p.base_hi
            and abs(now - base) * 100 >= p.x_pts - 1e-9
        ):
            move = now - base
            r = {}
            for h in (24, 72):
                later = _at(ts, ps, t + h * HOUR)
                r[h] = None if later is None else (later - base) / move
            out.append(Spike(t, base, now, r[24], r[72]))
            t += max(y, p.cooldown_hours * HOUR)
        else:
            t += p.grid_s
    return out


@dataclass(frozen=True)
class ClusterItem:
    t: int
    event: str
    tags: tuple[str, ...]


def news_clusters(items: list[ClusterItem], generic: set[str]) -> list[int]:
    """Cluster id (the index of a representative item) for each item."""
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def join(i: int, j: int) -> None:
        a, b = find(i), find(j)
        if a != b:
            parent[max(a, b)] = min(a, b)

    order = sorted(range(len(items)), key=lambda i: (items[i].t, i))
    for k, i in enumerate(order):  # same event within 6 h
        for j in order[k + 1 :]:
            if items[j].t - items[i].t > 6 * HOUR:
                break
            if items[j].event == items[i].event:
                join(i, j)
    for k, i in enumerate(order):  # a shared specific tag within 2 h
        ti = set(items[i].tags) - generic - {""}
        if not ti:
            continue
        for j in order[k + 1 :]:
            if items[j].t - items[i].t > 2 * HOUR:
                break
            if items[j].event != items[i].event and ti & set(items[j].tags):
                join(i, j)
    return [find(i) for i in range(len(items))]


@dataclass(frozen=True)
class Splits:
    train_end: datetime
    val_start: datetime
    val_end: datetime
    eval_start: datetime
    eval_end: datetime


DEFAULT_SPLITS = Splits(
    train_end=datetime(2026, 5, 8),
    val_start=datetime(2026, 5, 15),
    val_end=datetime(2026, 6, 15),
    eval_start=datetime(2026, 7, 1),  # after the compared models' training cutoffs
    eval_end=datetime(2026, 10, 11),
)


def assign_split(as_of: datetime, label_end: datetime, s: Splits) -> Split | None:
    if label_end <= s.train_end:
        return "train"
    if as_of >= s.val_start and label_end <= s.val_end:
        return "validation"
    if as_of >= s.eval_start and label_end <= s.eval_end:
        return "eval"
    return None


def drop_cross_split_clusters(tasks: list[Task]) -> list[Task]:
    splits: dict[str, set[str]] = defaultdict(set)
    for t in tasks:
        splits[t.cluster_id].add(t.split)
    return [t for t in tasks if len(splits[t.cluster_id]) == 1]


def _order_key(seed: int, task_id: str) -> str:
    return hashlib.sha256(f"{seed}:{task_id}".encode()).hexdigest()


def sample_split(
    pool: list[Task], n: int, per_cluster: int, sports_cap: float, seed: int
) -> list[Task]:
    """Deterministic: at most `per_cluster` tasks per cluster and at most sports_cap * n Sports."""
    max_sports = int(sports_cap * n)
    per: dict[str, int] = defaultdict(int)
    sports = 0
    out: list[Task] = []
    for t in sorted(pool, key=lambda t: _order_key(seed, t.task_id)):
        if len(out) == n:
            break
        if per[t.cluster_id] >= per_cluster:
            continue
        if t.category == "Sports":
            if sports >= max_sports:
                continue
            sports += 1
        per[t.cluster_id] += 1
        out.append(t)
    return sorted(out, key=lambda t: t.task_id)


def answers_sha(answers: dict[str, Answer]) -> str:
    canon = json.dumps(sorted((k, a.y) for k, a in answers.items()))
    return hashlib.sha256(canon.encode()).hexdigest()


def write_manifest(tasks: list[Task], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(tasks, key=lambda t: t.task_id)
    path.write_text("".join(t.model_dump_json() + "\n" for t in rows))


def freeze(tasks: list[Task], answers: dict[str, Answer], evals_dir: Path, rl_dir: Path) -> str:
    """Write the answer-free manifest (committed) and the answers (data/rl, not committed)."""
    write_manifest(tasks, evals_dir / "manifest.jsonl")
    mine = {t.task_id: answers[t.task_id] for t in tasks}
    rl_dir.mkdir(parents=True, exist_ok=True)
    (rl_dir / "answers.jsonl").write_text(
        "".join(a.model_dump_json() + "\n" for _, a in sorted(mine.items()))
    )
    sha = answers_sha(mine)
    (evals_dir / "answers.sha256").write_text(sha + "\n")
    return sha


def load_manifest(path: Path) -> list[Task]:
    return [Task.model_validate_json(ln) for ln in path.read_text().splitlines() if ln.strip()]


def load_answers(path: Path) -> dict[str, Answer]:
    out = {}
    for ln in path.read_text().splitlines():
        if ln.strip():
            a = Answer.model_validate_json(ln)
            out[a.task_id] = a
    return out


# ---------------------------------------------------------------- the real build


def _naive(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, UTC).replace(tzinfo=None)


def _epoch(d: datetime | None) -> int | None:
    return None if d is None else int(d.replace(tzinfo=UTC).timestamp())


def generic_tags(con: duckdb.DuckDBPyConnection) -> set[str]:
    rows = con.execute(
        """
        WITH e AS (SELECT event_slug, any_value(tags) AS tags FROM hist_markets GROUP BY 1),
        t AS (SELECT DISTINCT event_slug, unnest(string_split(tags, '|')) AS tag FROM e)
        SELECT tag FROM t WHERE tag <> '' GROUP BY tag
        HAVING count(*) > greatest(2, 0.02 * (SELECT count(*) FROM e))
        """
    ).fetchall()
    return {r[0] for r in rows}


def _series(con: duckdb.DuckDBPyConnection) -> Iterator[tuple[str, list[tuple[int, float]]]]:
    """Each market's series, streamed once over hist_prices ordered by market."""
    cur = con.execute(
        "SELECT condition_id, CAST(epoch(ts) AS BIGINT), p FROM hist_prices "
        "ORDER BY condition_id, ts"
    )
    cid: str | None = None
    pts: list[tuple[int, float]] = []
    while rows := cur.fetchmany(500_000):
        for c, t, p in rows:
            if c != cid:
                if cid is not None:
                    yield cid, pts
                cid, pts = c, []
            pts.append((int(t), float(p)))
    if cid is not None:
        yield cid, pts


def build_tasks(
    con: duckdb.DuckDBPyConnection,
    params: SpikeParams = DEFAULT_SPIKES,
    splits: Splits = DEFAULT_SPLITS,
    resolve_hours: tuple[int, ...] = (24, 168),
) -> tuple[list[Task], dict[str, Answer]]:
    meta = {
        r[0]: r[1:]
        for r in con.execute(
            "SELECT m.condition_id, m.event_slug, m.tags, m.category, m.start_at, m.end_at, "
            "m.closed_at, o.y, o.agrees "
            "FROM hist_markets m JOIN hist_outcomes o USING (condition_id)"
        ).fetchall()
    }
    generic = generic_tags(con)
    spikes: list[tuple[str, Spike]] = []
    resolve: list[tuple[Task, Answer]] = []
    for cid, series in _series(con):
        if cid not in meta:
            continue
        ev, tags, cat, start, end, closed, y, agrees = meta[cid]
        span = life(_epoch(start), _epoch(end), _epoch(closed))
        if span is None:
            continue
        close = span[1]
        spikes += [(cid, s) for s in detect_spikes(series, close, params)]
        if not agrees:
            continue
        ts = [a for a, _ in series]
        ps = [b for _, b in series]
        for h in resolve_hours:
            at = close - h * HOUR
            i = bisect.bisect_right(ts, at) - 1
            if at <= span[0] or i < 0 or at - ts[i] > 24 * HOUR:
                continue
            price = ps[i]
            if not params.base_lo <= price <= params.base_hi:
                continue
            split = assign_split(_naive(at), _naive(close), splits)
            if split is None:
                continue
            tid = f"resolve:{cid}:{at}"
            resolve.append(
                (
                    Task(
                        task_id=tid,
                        kind="resolve",
                        condition_id=cid,
                        as_of=_naive(at),
                        facts={"horizon_hours": h, "price_now": round(price, 4)},
                        cluster_id=f"event:{ev}",
                        split=split,
                        category=cat,
                    ),  # fmt: skip
                    Answer(task_id=tid, y=float(y), detail={"horizon_hours": h}),
                )
            )
    items = [
        ClusterItem(s.t, str(meta[c][0]), tuple((meta[c][1] or "").split("|"))) for c, s in spikes
    ]
    ids = news_clusters(items, generic)
    tasks: list[Task] = [t for t, _ in resolve]
    answers: dict[str, Answer] = {a.task_id: a for _, a in resolve}
    for (cid, s), cl in zip(spikes, ids, strict=True):
        as_of = _naive(s.t)
        split = assign_split(as_of, _naive(s.t + params.horizon_hours * HOUR), splits)
        if split is None or s.retained72 is None:
            continue
        tid = f"spike_hold:{cid}:{s.t}"
        tasks.append(
            Task(
                task_id=tid,
                kind="spike_hold",
                condition_id=cid,
                as_of=as_of,
                facts={
                    "move_pts": round(s.move * 100, 1),
                    "window_hours": params.y_hours,
                    "price_before": round(s.base, 4),
                    "price_now": round(s.now, 4),
                    "horizon_hours": params.horizon_hours,
                },
                cluster_id=f"news:{spikes[cl][0]}:{spikes[cl][1].t}",
                split=split,
                category=str(meta[cid][2]),
            )  # fmt: skip
        )
        answers[tid] = Answer(
            task_id=tid,
            y=1.0 if s.held else 0.0,
            detail={"retained24": s.retained24, "retained72": s.retained72},
        )
    kept = drop_cross_split_clusters(tasks)
    return kept, {t.task_id: answers[t.task_id] for t in kept}
