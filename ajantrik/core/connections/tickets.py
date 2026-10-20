"""Tickets: Jira over REST (email + API token references), and the backlog file.

Reads (get, search) happen whenever a job starts or the backlog syncs. Writes (comment, transition)
happen only from `factory deliver`, after the configured approval and the outgoing secret scan,
and only when connections.tickets.write_back asks for them. Agents never write to Jira.
"""

from __future__ import annotations

import fcntl
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ..util import FactoryError, run
from .git_host import basic_auth
from .secrets import resolve


class Jira:
    def __init__(self, conn: dict[str, Any]):
        if not conn.get("site"):
            raise FactoryError("connections.tickets.site is not set (https://<org>.atlassian.net)")
        self.conn = conn
        self.site = conn["site"].rstrip("/")

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None,
                 query: dict[str, str] | None = None) -> Any:
        email = resolve(self.conn.get("email_ref"), "Jira email")
        token = resolve(self.conn.get("token_ref"), "Jira API token")
        if not email or not token:
            raise FactoryError("Jira REST needs connections.tickets.email_ref and token_ref")
        url = self.site + path + ("?" + urllib.parse.urlencode(query) if query else "")
        req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": basic_auth(email, token), "Accept": "application/json",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise FactoryError(f"Jira {method} {path}: HTTP {e.code}") from e
        except urllib.error.URLError as e:
            raise FactoryError(f"Jira {method} {path}: {e.reason}") from e

    @staticmethod
    def _issue(data: dict[str, Any]) -> dict[str, Any]:
        f = data.get("fields") or {}
        parent = f.get("parent") or {}
        ptype = ((parent.get("fields") or {}).get("issuetype") or {}).get("name", "")
        return {"key": data.get("key"), "summary": f.get("summary", ""),
                "type": (f.get("issuetype") or {}).get("name", ""),
                "status": (f.get("status") or {}).get("name", ""),
                "status_category": ((f.get("status") or {}).get("statusCategory") or {}).get("name", ""),
                "labels": f.get("labels") or [],
                "epic": parent.get("key") if ptype == "Epic" or (parent and not ptype) else None}

    def get(self, key: str) -> dict[str, Any]:
        return self._issue(self._request("GET", f"/rest/api/3/issue/{key}",
                                         query={"fields": "summary,issuetype,status,parent,labels"}))

    def search(self, jql: str, limit: int = 50) -> list[dict[str, Any]]:
        data = self._request("POST", "/rest/api/3/search/jql",
                             {"jql": jql, "maxResults": limit,
                              "fields": ["summary", "issuetype", "status", "parent", "labels"]})
        return [self._issue(i) for i in data.get("issues") or []]

    def comment(self, key: str, text: str) -> None:
        paras = [{"type": "paragraph", "content": [{"type": "text", "text": p}]}
                 for p in text.split("\n\n") if p.strip()]
        self._request("POST", f"/rest/api/3/issue/{key}/comment",
                      {"body": {"type": "doc", "version": 1, "content": paras}})

    def transition(self, key: str, to: str) -> None:
        options = self._request("GET", f"/rest/api/3/issue/{key}/transitions").get("transitions") or []
        match = next((t for t in options if t["name"].lower() == to.lower()
                      or (t.get("to") or {}).get("name", "").lower() == to.lower()), None)
        if not match:
            raise FactoryError(f"Jira {key}: no transition to {to!r} (have {[t['name'] for t in options]})")
        self._request("POST", f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": match["id"]}})

    def myself(self) -> str:
        return self._request("GET", "/rest/api/3/myself").get("displayName", "?")


def check(conn: dict[str, Any]) -> tuple[bool, str]:
    if conn.get("provider", "none") == "none":
        return True, "no ticket system configured"
    if conn.get("mode") == "mcp":
        return True, "Jira through the Claude Code session's Atlassian MCP (checked when the session uses it)"
    try:
        who = Jira(conn).myself()
        detail = f"Jira REST ok as {who}"
        if conn.get("jql"):
            n = len(Jira(conn).search(conn["jql"], limit=5))
            detail += f"; backlog query returns {n}{'+' if n == 5 else ''} issue(s)"
        return True, detail
    except FactoryError as e:
        return False, str(e)


def resolve_epic_base(repo: Path, conn: dict[str, Any], epic: str | None, default: str,
                      remote: str = "origin") -> tuple[str | None, str]:
    """(base, reason). base None means blocked. Exactly one remote branch for the epic wins."""
    if not epic:
        return default, "no epic"
    run(["git", "fetch", remote, "--prune"], cwd=repo, check=False, timeout=120)
    glob = conn.get("epic_branch_glob", "*{epic}_*").replace("{epic}", epic)
    out = run(["git", "branch", "-r", "--list", f"{remote}/{glob}"], cwd=repo, check=False).stdout
    branches = [b.strip().removeprefix(f"{remote}/") for b in out.splitlines() if b.strip() and "->" not in b]
    if len(branches) == 1:
        return branches[0], f"epic {epic} branch"
    if not branches:
        if conn.get("no_epic_branch") == "fallback":
            return default, f"epic {epic} has no branch on {remote}; using {default}"
        return None, f"epic {epic} has no branch on {remote}"
    return None, f"epic {epic} has several branches: {', '.join(branches)}"


# ---- backlog ----------------------------------------------------------------------------------------
# .factory/backlog.md, under `## Queue`:
#   - [ ] GEN-781 · base: feature/GEN-750_Projects · Add project rename      (ticket)
#   - [ ] base: main · Escape plant names in tooltips                          (free text)
#   - [!] GEN-795 · epic GEN-802 has no branch on origin · Actor profile export
#   - [x] GEN-781 · base: ... · Add project rename → 004-GEN-781-project-rename
BACKLOG_HEADER = """# Backlog

`factory sync` (or `/factory sync`) adds tickets here; you can also add lines by hand:
`- [ ] base: <branch> · <request>`. `next` builds the first `- [ ]` item and checks it off with its
job id. `- [!]` items are blocked until a human resolves them.

## Queue

"""
ITEM = re.compile(r"^- \[(?P<state>[ x!])\] (?P<rest>.*)$")


def backlog_path(root: Path) -> Path:
    return root / ".factory" / "backlog.md"


def parse_item(rest: str, key_pattern: str) -> dict[str, Any]:
    parts = [p.strip() for p in rest.split(" · ")]
    item: dict[str, Any] = {"key": None, "base": None, "text": parts[-1] if parts else ""}
    for p in parts[:-1]:
        if p.startswith("base:"):
            item["base"] = p.split(":", 1)[1].strip()
        elif re.fullmatch(key_pattern, p):
            item["key"] = p
    return item


def sync(root: Path, cfg: dict[str, Any], jobs: list[dict[str, Any]]) -> list[str]:
    conn = (cfg.get("connections") or {}).get("tickets") or {}
    if conn.get("provider") != "jira":
        raise FactoryError("sync needs connections.tickets.provider: jira")
    if not conn.get("jql"):
        raise FactoryError("connections.tickets needs a project or a jql")
    jira = Jira(conn)
    path = backlog_path(root)
    text = path.read_text() if path.is_file() else BACKLOG_HEADER
    known = set(re.findall(conn.get("key_pattern", r"[A-Z][A-Z0-9]+-\d+"), text))
    known |= {j.get("ticket") or j.get("jira") for j in jobs if j.get("ticket") or j.get("jira")}
    default = (cfg.get("repo") or {}).get("default_base", "main")
    remote = ((cfg.get("connections") or {}).get("git") or {}).get("remote", "origin")
    report, new_lines = [], []
    for issue in jira.search(conn["jql"]):
        key = issue["key"]
        if key in known:
            report.append(f"{key}: skipped (already in the backlog or a job)")
            continue
        if not issue["epic"]:
            try:
                issue["epic"] = jira.get(key)["epic"]
            except FactoryError:
                pass
        base, reason = resolve_epic_base(root, conn, issue["epic"], default, remote)
        if base:
            new_lines.append(f"- [ ] {key} · base: {base} · {issue['summary']}")
            report.append(f"{key}: added (base {base})")
        else:
            new_lines.append(f"- [!] {key} · {reason} · {issue['summary']}")
            report.append(f"{key}: blocked ({reason})")
    # unblock items whose epic branch now resolves
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = ITEM.match(line)
        if m and m.group("state") == "!":
            parts = [p.strip() for p in m.group("rest").split(" · ")]
            em = re.search(r"epic (\S+)", parts[1] if len(parts) > 2 else "")
            if em:
                base, _ = resolve_epic_base(root, conn, em.group(1), default, remote)
                if base:
                    lines[i] = f"- [ ] {parts[0]} · base: {base} · {parts[-1]}"
                    report.append(f"{parts[0]}: unblocked (base {base})")
    text = "\n".join(lines).rstrip("\n") + "\n" + "".join(line + "\n" for line in new_lines)
    if "## Queue" not in text:
        text = BACKLOG_HEADER + text
    path.write_text(text)
    return report


def claim_next(root: Path, cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Claim the first `- [ ]` item: rewrite it to `- [x] ... → claimed` under a file lock."""
    path = backlog_path(root)
    if not path.is_file():
        return None
    conn = (cfg.get("connections") or {}).get("tickets") or {}
    key_pattern = conn.get("key_pattern", r"[A-Z][A-Z0-9]+-\d+")
    jira = Jira(conn) if conn.get("provider") == "jira" and conn.get("mode") == "rest" else None
    with path.open("r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        lines = f.read().splitlines()
        claimed = None
        for i, line in enumerate(lines):
            m = ITEM.match(line)
            if not m or m.group("state") != " ":
                continue
            item = parse_item(m.group("rest"), key_pattern)
            if item["key"] and jira:
                issue = jira.get(item["key"])
                if issue["status_category"] and issue["status_category"] != "To Do":
                    lines[i] = f"- [x] {m.group('rest')} → {issue['status']}"
                    continue
                if conn.get("label") and conn["label"] not in issue["labels"]:
                    lines[i] = f"- [x] {m.group('rest')} → unlabelled"
                    continue
            lines[i] = f"- [x] {m.group('rest')} → claimed"
            claimed = {**item, "line": lines[i]}
            break
        f.seek(0)
        f.write("\n".join(lines) + "\n")
        f.truncate()
    return claimed


def mark_claimed(root: Path, line: str, job_id: str) -> None:
    path = backlog_path(root)
    with path.open("r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        text = f.read().replace(line, line.replace("→ claimed", f"→ {job_id}"), 1)
        f.seek(0)
        f.write(text)
        f.truncate()
