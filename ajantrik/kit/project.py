"""A configured repo: `.factory/config/` holds the factory's source of truth.

    .factory/config/factory.yaml            the loop, runtime, gate, connections, delivery...
    .factory/config/agents/<name>.md        one agent: YAML frontmatter + its instructions
    .factory/config/skills/<name>/SKILL.md  one skill (plus any files next to it)

Everything else under .factory/ and .claude/ is rendered from these (see render.py) and can be
regenerated at any time. The studio and the CLI edit only these files.
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ajantrik.kit.schema import FactoryConfig, warnings

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)
KNOWN_TOOLS = ["Read", "Grep", "Glob", "Bash", "Edit", "Write", "WebFetch", "WebSearch"]


def data_root() -> Path:
    """presets/ and library/: next to the package in a checkout, under ajantrik/_data when installed."""
    here = Path(__file__).resolve().parents[2]
    if (here / "presets").is_dir():
        return here
    return Path(__file__).resolve().parents[1] / "_data"


def presets_dir() -> Path:
    return data_root() / "presets"


def library_dir() -> Path:
    return data_root() / "library"


def list_presets() -> list[dict[str, str]]:
    out = []
    for d in sorted(p for p in presets_dir().iterdir() if (p / "factory.yaml").is_file()):
        meta = d / "PRESET.md"
        first = meta.read_text().strip().splitlines()[0].lstrip("# ") if meta.is_file() else d.name
        out.append({"name": d.name, "title": first})
    return out


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    m = FRONTMATTER.match(text)
    if not m:
        return {}, text
    meta = yaml.safe_load(m.group(1)) or {}
    if not isinstance(meta, dict):
        raise ValueError("frontmatter must be a YAML mapping")
    return meta, m.group(2)


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return [str(x) for x in v]


@dataclass
class AgentDef:
    name: str
    description: str = ""
    tools: list[str] = field(default_factory=lambda: ["Read", "Grep", "Glob", "Bash"])
    model: str | None = None
    skills: list[str] = field(default_factory=list)
    mcp: list[str] = field(default_factory=list)
    body: str = ""
    path: Path | None = None

    @classmethod
    def parse(cls, text: str, name: str | None = None, path: Path | None = None) -> AgentDef:
        meta, body = split_frontmatter(text)
        n = str(meta.get("name") or name or "")
        if not NAME.match(n):
            raise ValueError(f"agent name {n!r} must be lower-case letters, digits, - or _")
        return cls(name=n, description=str(meta.get("description", "")).strip(),
                   tools=_as_list(meta.get("tools")) or ["Read", "Grep", "Glob", "Bash"],
                   model=meta.get("model"), skills=_as_list(meta.get("skills")), mcp=_as_list(meta.get("mcp")),
                   body=body.lstrip("\n"), path=path)

    def to_markdown(self) -> str:
        meta: dict[str, Any] = {"name": self.name, "description": self.description, "tools": ", ".join(self.tools)}
        if self.model:
            meta["model"] = self.model
        if self.skills:
            meta["skills"] = self.skills
        if self.mcp:
            meta["mcp"] = self.mcp
        head = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=10_000).strip()
        return f"---\n{head}\n---\n\n{self.body.strip()}\n"

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "tools": self.tools, "model": self.model,
                "skills": self.skills, "mcp": self.mcp, "body": self.body}


@dataclass
class SkillDef:
    name: str
    description: str = ""
    body: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None

    @classmethod
    def parse(cls, text: str, name: str, path: Path | None = None) -> SkillDef:
        meta, body = split_frontmatter(text)
        n = str(meta.get("name") or name)
        if not NAME.match(n):
            raise ValueError(f"skill name {n!r} must be lower-case letters, digits, - or _")
        return cls(name=n, description=str(meta.get("description", "")).strip(), body=body.lstrip("\n"),
                   meta=meta, path=path)

    def to_markdown(self) -> str:
        meta = dict(self.meta)
        meta["name"] = self.name
        meta["description"] = self.description
        head = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=10_000).strip()
        return f"---\n{head}\n---\n\n{self.body.strip()}\n"

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "body": self.body}


@dataclass
class Loaded:
    cfg: FactoryConfig | None
    agents: dict[str, AgentDef]
    skills: dict[str, SkillDef]
    errors: list[str]
    warnings: list[str]

    @property
    def ok(self) -> bool:
        return self.cfg is not None and not self.errors


def format_validation_error(e: ValidationError) -> list[str]:
    out = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err["loc"])
        msg = err["msg"].removeprefix("Value error, ")
        out.append(f"{loc}: {msg}" if loc else msg)
    return out


class Project:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.factory_dir = self.root / ".factory"
        self.config_dir = self.factory_dir / "config"
        self.yaml_path = self.config_dir / "factory.yaml"
        self.agents_dir = self.config_dir / "agents"
        self.skills_dir = self.config_dir / "skills"

    @classmethod
    def find(cls, start: Path | None = None) -> Project:
        p = (start or Path.cwd()).resolve()
        for d in [p, *p.parents]:
            if (d / ".factory" / "config" / "factory.yaml").is_file():
                return cls(d)
        raise FileNotFoundError("no .factory/config/factory.yaml here or above; run `factory init` first")

    def exists(self) -> bool:
        return self.yaml_path.is_file()

    # ---- reading ----------------------------------------------------------------------------------
    def raw(self) -> dict[str, Any]:
        data = yaml.safe_load(self.yaml_path.read_text()) or {}
        if not isinstance(data, dict):
            raise ValueError("factory.yaml must be a mapping")
        return data

    def agents(self) -> dict[str, AgentDef]:
        out = {}
        for f in sorted(self.agents_dir.glob("*.md")):
            a = AgentDef.parse(f.read_text(), f.stem, f)
            out[a.name] = a
        return out

    def skills(self) -> dict[str, SkillDef]:
        out = {}
        for f in sorted(self.skills_dir.glob("*/SKILL.md")):
            s = SkillDef.parse(f.read_text(), f.parent.name, f)
            out[s.name] = s
        return out

    def load(self, data: dict[str, Any] | None = None) -> Loaded:
        errors: list[str] = []
        try:
            agents = self.agents()
        except (ValueError, yaml.YAMLError) as e:
            agents, errors = {}, [f"agents: {e}"]
        try:
            skills = self.skills()
        except (ValueError, yaml.YAMLError) as e:
            skills, errors = {}, [*errors, f"skills: {e}"]
        cfg = None
        try:
            cfg = FactoryConfig.model_validate(self.raw() if data is None else data)
        except ValidationError as e:
            errors += format_validation_error(e)
        except (ValueError, yaml.YAMLError) as e:
            errors.append(str(e))
        warns: list[str] = []
        if cfg:
            errors += cross_check(cfg, agents, skills)
            warns = warnings(cfg)
        return Loaded(cfg, agents, skills, errors, warns)

    # ---- writing ----------------------------------------------------------------------------------
    def _backup(self, path: Path) -> None:
        if path.is_file():
            bak = self.config_dir / ".history" / f"{path.relative_to(self.config_dir)}.{time.strftime('%Y%m%dT%H%M%S')}"
            bak.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, bak)

    def save_yaml(self, data: dict[str, Any]) -> Loaded:
        """Validate, then write. An invalid config is never written."""
        loaded = self.load(data)
        # Missing agent or skill files are allowed while editing (add the stage, then the agent).
        hard = [e for e in loaded.errors if "unknown agent" not in e and "unknown skill" not in e]
        if loaded.cfg is None or hard:
            return loaded
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self._backup(self.yaml_path)
        self.yaml_path.write_text(dump_yaml(data))
        return loaded

    def save_agent(self, agent: AgentDef, old_name: str | None = None) -> Path:
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        path = self.agents_dir / f"{agent.name}.md"
        self._backup(path)
        path.write_text(agent.to_markdown())
        if old_name and old_name != agent.name:
            self.delete_agent(old_name)
        return path

    def delete_agent(self, name: str) -> None:
        path = self.agents_dir / f"{name}.md"
        self._backup(path)
        path.unlink(missing_ok=True)

    def save_skill(self, skill: SkillDef, old_name: str | None = None) -> Path:
        path = self.skills_dir / skill.name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._backup(path)
        path.write_text(skill.to_markdown())
        if old_name and old_name != skill.name:
            old = self.skills_dir / old_name
            for f in old.iterdir() if old.is_dir() else []:
                if f.name != "SKILL.md" and not (path.parent / f.name).exists():
                    shutil.move(str(f), path.parent / f.name)
            self.delete_skill(old_name)
        return path

    def delete_skill(self, name: str) -> None:
        d = self.skills_dir / name
        if (d / "SKILL.md").is_file():
            self._backup(d / "SKILL.md")
        shutil.rmtree(d, ignore_errors=True)

    # ---- creating ---------------------------------------------------------------------------------
    def init_from_preset(self, preset: str, name: str | None = None, force: bool = False) -> None:
        src = presets_dir() / preset
        if not (src / "factory.yaml").is_file():
            raise FileNotFoundError(f"no preset {preset!r}; available: {[p['name'] for p in list_presets()]}")
        if self.exists() and not force:
            raise FileExistsError(f"{self.yaml_path} exists; pass --force to replace it")
        self.config_dir.mkdir(parents=True, exist_ok=True)
        data = yaml.safe_load((src / "factory.yaml").read_text())
        if name:
            data.setdefault("repo", {})["name"] = name
            if data["repo"].get("title", "").endswith("Software Factory") and preset == "blank":
                data["repo"]["title"] = ""
        self.yaml_path.write_text(dump_yaml(data))
        for sub in ("agents", "skills"):
            target = self.config_dir / sub
            if (src / sub).is_dir():
                shutil.copytree(src / sub, target, dirs_exist_ok=True)
        # Agents and skills the preset uses but doesn't override come from the library.
        loaded = self.load()
        if loaded.cfg:
            for agent in sorted(loaded.cfg.agent_names() - set(loaded.agents)):
                lib = library_dir() / "agents" / f"{agent}.md"
                generic = library_dir() / "agents" / f"{generic_agent(agent)}.md"
                for cand in (lib, generic):
                    if cand.is_file():
                        a = AgentDef.parse(cand.read_text(), agent)
                        a.name = agent
                        self.save_agent(a)
                        break
            loaded = self.load()
            for skill in sorted({s for a in loaded.agents.values() for s in a.skills} - set(loaded.skills)):
                lib_skill = library_dir() / "skills" / skill
                if lib_skill.is_dir():
                    shutil.copytree(lib_skill, self.skills_dir / skill, dirs_exist_ok=True)
        if (src / "harness").is_dir():
            shutil.copytree(src / "harness", self.factory_dir / "harness", dirs_exist_ok=True)


def generic_agent(name: str) -> str:
    """builder-web -> builder, reviewer-billing -> reviewer: the library's template for that role."""
    for prefix in ("builder", "reviewer"):
        if name.startswith(prefix + "-"):
            return prefix
    return name


def cross_check(cfg: FactoryConfig, agents: dict[str, AgentDef], skills: dict[str, SkillDef]) -> list[str]:
    errs = []
    for name in sorted(cfg.agent_names() - set(agents)):
        errs.append(f"unknown agent {name!r}: add .factory/config/agents/{name}.md")
    for a in agents.values():
        for s in a.skills:
            if s not in skills:
                errs.append(f"agent {a.name}: unknown skill {s!r}")
        unknown = [t for t in a.tools if t not in KNOWN_TOOLS and not t.startswith("mcp__")]
        if unknown:
            errs.append(f"agent {a.name}: unknown tools {unknown}")
    return errs


class _Dumper(yaml.SafeDumper):
    pass


def _str(dumper: yaml.SafeDumper, data: str) -> yaml.Node:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _str)


def dump_yaml(data: dict[str, Any]) -> str:
    return yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=110, default_flow_style=False)
