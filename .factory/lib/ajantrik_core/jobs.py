"""Job state on disk: .factory/jobs/<id>/job.json plus the files agents write next to it.

job.json keeps the shape the dashboards already read (id, feature, base, branch, worktree, stage,
round, agent, builders, gate, reviews, note, started/stageSince/updated) and adds ticket,
ticket_type, round_base, rework, delivery. It is rewritten whole on every change.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .util import FactoryError, now, read_json, write_json


def jobs_dir(root: Path) -> Path:
    return root / ".factory" / "jobs"


def job_dir(root: Path, job_id: str) -> Path:
    if not re.fullmatch(r"\d{3,}-[A-Za-z0-9-]+", job_id):
        raise FactoryError(f"not a job id: {job_id!r}")
    return jobs_dir(root) / job_id


def resolve_id(root: Path, ref: str) -> str:
    """`004`, `4` or the full id."""
    if (jobs_dir(root) / ref / "job.json").is_file():
        return ref
    if ref.isdigit():
        hits = sorted(p.name for p in jobs_dir(root).glob(f"{int(ref):03d}-*") if (p / "job.json").is_file())
        if len(hits) == 1:
            return hits[0]
    raise FactoryError(f"no job {ref!r} in {jobs_dir(root)}")


def read(root: Path, job_id: str) -> dict[str, Any]:
    data = read_json(job_dir(root, job_id) / "job.json")
    if not isinstance(data, dict):
        raise FactoryError(f"job {job_id} has no readable job.json")
    return data


def write(root: Path, job: dict[str, Any]) -> dict[str, Any]:
    job["updated"] = now()
    write_json(job_dir(root, job["id"]) / "job.json", job)
    return job


def update(root: Path, job: dict[str, Any], **fields: Any) -> dict[str, Any]:
    if "stage" in fields and fields["stage"] != job.get("stage"):
        job["stageSince"] = now()
    job.update(fields)
    return write(root, job)


def list_jobs(root: Path) -> list[dict[str, Any]]:
    out = []
    for f in sorted(jobs_dir(root).glob("*/job.json"), reverse=True):
        data = read_json(f)
        if isinstance(data, dict):
            out.append(data)
    return out


def allocate(root: Path, slug: str) -> str:
    """Next NNN-<slug>, claimed with mkdir (no -p) so two sessions never share a number."""
    d = jobs_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    nums = [int(m.group(1)) for p in d.iterdir() if (m := re.match(r"(\d{3,})-", p.name))]
    n = max(nums, default=0) + 1
    for _ in range(100):
        job_id = f"{n:03d}-{slug}"
        try:
            (d / job_id).mkdir()
            return job_id
        except FileExistsError:
            n += 1
    raise FactoryError("could not allocate a job id")


# ---- approvals --------------------------------------------------------------------------------------
# One file per approval stage. `entered` ties it to the moment the job reached that stage, so an
# approval given before a rework does not carry over to the reworked spec and design.
def approval_path(root: Path, job_id: str, stage: str) -> Path:
    return job_dir(root, job_id) / "approvals" / f"{stage}.json"


def record_approval(root: Path, job: dict[str, Any], stage: str, by: str) -> None:
    write_json(approval_path(root, job["id"], stage), {"stage": stage, "by": by, "at": now(),
                                                       "entered": job.get("stageSince")})


def approved(root: Path, job: dict[str, Any], stage: str) -> bool:
    rec = read_json(approval_path(root, job["id"], stage))
    return isinstance(rec, dict) and rec.get("entered") == job.get("stageSince")


def next_rework(root: Path, job_id: str) -> int:
    d = job_dir(root, job_id)
    ks = [int(m.group(1)) for p in d.glob("rework-*.md") if (m := re.match(r"rework-(\d+)\.md", p.name))]
    return max(ks, default=0) + 1
