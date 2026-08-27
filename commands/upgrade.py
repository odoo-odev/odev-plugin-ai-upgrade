"""Upgrade Odoo modules using AI."""

import re
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import jinja2
import networkx as nx
from git import BadName, GitCommandError, Repo

from odev.common import args, progress
from odev.common.commands import DatabaseCommand
from odev.common.connectors import GitConnector
from odev.common.logging import logging
from odev.common.mixins.databases.list import ListLocalDatabasesMixin
from odev.common.odoobin import (
    ODOO_COMMUNITY_REPOSITORIES,
    ODOO_ENTERPRISE_REPOSITORIES,
    ODOO_UPGRADE_REPOSITORY,
    OdoobinProcess,
)
from odev.common.utils import EmployeeUtils

from odev.plugins.odev_plugin_ai.common.mixins import AICommandMixin
from odev.plugins.odev_plugin_ai_upgrade.common.gates import gutted_overrides, iter_cited_shas


if TYPE_CHECKING:
    from odev.plugins.odev_plugin_ai_upgrade.common.knowledge import KnowledgeIndex


logger = logging.getLogger(__name__)

# Customisation that lives only in the customer database. The run has no access to
# it (no customer database is provisioned), so it is never migrated and must be
# declared as unverified rather than silently skipped.
UNREACHABLE_CUSTOMISATION: tuple[str, ...] = (
    "Studio views (`ir_ui_view.arch_db`, `studio_customization` records)",
    "website views customised on write (cowed)",
    "Studio-authored automations and server actions",
    "`mail.template` records edited in the database",
    "saved filters (`ir_filters`)",
)


class UpgradeCommand(DatabaseCommand, ListLocalDatabasesMixin, AICommandMixin):
    """Upgrades an Odoo module from a previous version to a new version using an AI model.

    This command runs in a loop, attempting to fix errors by editing files and re-running tests.
    """

    _name = "upgrade"
    _database_arg_required = False
    _target_db: str | None = None
    _base_sha: str | None = None

    path = args.Path(
        aliases=["--path"],
        description="Path to the module directory. Defaults to the current directory.",
        default=Path(".").resolve(),
    )

    module_name = args.String(
        description="The name of the module to upgrade. If not provided, lists available modules in addons paths.",
        default=None,
        nargs="?",
    )

    to_version = args.String(
        aliases=["--to"],
        description="The target Odoo version. Defaults to the environment's target version.",
    )

    from_version = args.String(
        aliases=["--from"],
        description="The source Odoo version. Overrides automatic detection from database or path.",
        default=None,
    )

    comment = args.String(
        aliases=["-c", "--comment"],
        description="Add a custom comment/instruction to the AI prompt.",
        default="",
    )

    task_id = args.String(
        aliases=["--task-id"],
        description="The task ID for the upgrade (e.g., 12345). Mandatory for commit messages.",
        required=True,
    )

    submodules = args.Flag(
        aliases=["--submodules"],
        description="Look for modules in git submodules (default: False).",
        default=False,
    )

    no_ruff = args.Flag(
        aliases=["--no-ruff"],
        description="Don't instruct the AI to run ruff check --fix after edits.",
        default=False,
    )

    strict_gates = args.Flag(
        aliases=["--strict-gates"],
        description="Fail the run when a post-flight gate reports a finding, instead of warning.",
        default=False,
    )

    @property
    def _database_exists_required(self) -> bool:
        return False

    def _get_sorted_modules(self, search_paths: list[Path]) -> list[dict]:
        """Find modules in search paths and sort them topologically based on dependencies."""
        modules = {}
        for search_path in search_paths:
            if not search_path.exists():
                continue

            # Check if the search_path itself is a module
            potential_manifest = search_path / "__manifest__.py"
            if search_path.is_dir() and potential_manifest.exists():
                manifest = OdoobinProcess.read_manifest(potential_manifest)
                if manifest:
                    modules[search_path.name] = {
                        "path": search_path,
                        "depends": manifest.get("depends", []),
                        "name": search_path.name,
                    }

            # Also check children (for addons paths)
            if search_path.is_dir():
                for child in search_path.iterdir():
                    if child.is_dir() and (child / "__manifest__.py").exists():
                        manifest = OdoobinProcess.read_manifest(child / "__manifest__.py")
                        if manifest:
                            modules[child.name] = {
                                "path": child,
                                "depends": manifest.get("depends", []),
                                "name": child.name,
                            }

        graph = nx.DiGraph()
        for name, info in modules.items():
            graph.add_node(name)
            for dep in info["depends"]:
                if dep in modules:
                    graph.add_edge(dep, name)

        try:
            sorted_names = list(nx.topological_sort(graph))
        except nx.NetworkXUnfeasible:
            # Fallback if there's a cycle
            logger.warning("Dependency cycle detected while sorting modules.")
            sorted_names = list(modules.keys())

        return [modules[name] for name in sorted_names if name in modules]

    def _resolve_standard_deps(
        self,
        modules_info: list[dict],
        from_ver: str,
    ) -> dict[str, str]:
        """Return the transitive standard Odoo dependencies of the given custom modules."""
        from odev.common.odoobin import ODOO_COMMUNITY_REPOSITORIES, ODOO_ENTERPRISE_REPOSITORIES

        custom_names: set[str] = {m["name"] for m in modules_info}
        community_connector = GitConnector(ODOO_COMMUNITY_REPOSITORIES[0])
        enterprise_connector = GitConnector(ODOO_ENTERPRISE_REPOSITORIES[0])

        community_roots = self._get_addons_roots(community_connector, from_ver)
        enterprise_roots = self._get_addons_roots(enterprise_connector, from_ver)

        roots_map = {"community": community_roots, "enterprise": enterprise_roots}
        connectors_map = {"community": community_connector, "enterprise": enterprise_connector}

        resolved: dict[str, str] = {}
        queue: set[str] = set()

        for m in modules_info:
            queue.update(m.get("depends", []))

        while queue:
            mod = queue.pop()
            if mod in resolved or mod in custom_names:
                continue
            found = self._find_standard_module(mod, from_ver, roots_map, connectors_map)
            if found:
                manifest, kind = found
                resolved[mod] = kind
                queue.update(manifest.get("depends", []))

        return resolved

    def _get_addons_roots(self, connector: "GitConnector", version: str) -> list[Path]:
        """Build addons-path lookup maps for the version worktree."""
        roots: list[Path] = []
        for wt in connector.worktrees():
            if wt.name == version:
                for sub in ["", "addons"]:
                    candidate = wt.path / sub if sub else wt.path
                    if OdoobinProcess.check_addons_path(candidate):
                        roots.append(candidate)
        return roots

    def _find_standard_module(
        self,
        name: str,
        version: str,
        roots_map: dict[str, list[Path]],
        connectors_map: dict[str, GitConnector],
    ) -> tuple[dict, str] | None:
        """Return (manifest_dict, kind) if module is found in an Odoo tree."""
        for kind, roots in roots_map.items():
            for root in roots:
                candidate = root / name / "__manifest__.py"
                if candidate.exists():
                    manifest = OdoobinProcess.read_manifest(candidate)
                    if manifest:
                        return manifest, kind

        import ast

        for kind, connector in connectors_map.items():
            for sub in ["", "addons"]:
                path = f"{sub}/{name}/__manifest__.py" if sub else f"{name}/__manifest__.py"
                try:
                    content = connector.repository.git.show(f"{version}:{path}")
                    manifest = ast.literal_eval(content)
                    if isinstance(manifest, dict):
                        return manifest, kind
                except Exception:
                    continue
        return None

    def run(self) -> None:
        """Execute the upgrade command."""
        self._target_db = None
        try:
            self._run_upgrade()
        except KeyboardInterrupt:
            logger.warning("\nUpgrade interrupted by user.")
        finally:
            exclude = [self._target_db] if self._target_db else None
            self._cleanup_wizard(stage="post-flight", exclude=exclude)

    def _detect_versions(self) -> tuple[str, str]:
        """Determine source and target Odoo versions."""
        from_ver = (
            self.args.from_version
            or OdoobinProcess.version_from_manifest(self.args.path)
            or OdoobinProcess.version_from_addons(self.args.path)
            or self._database.version
        )

        if not from_ver:
            raise self.error(
                f"Could not determine source version from path '{self.args.path}' or database '{self._database.name}'. "
                "Ensure the path contains valid Odoo modules or use --from <version>."
            )

        from_ver = str(from_ver)
        logger.info(f"Detected source version: {from_ver}")

        target_ver = self.args.to_version or ""
        if not target_ver:
            raise self.error("Could not determine target version. Please specify a --to <version>.")

        return from_ver, target_ver

    def _get_sandbox_config(
        self,
        target_ver: str,
        project_path: Path,
        worktrees_path: Path,
        venvs_path: Path,
    ) -> tuple[str, list[str], list[str]]:
        """Configure target database name and sandbox directories."""
        base_db_name = (
            self._database.name
            if getattr(self, "_database", None) and self._database.platform.name != "dummy"
            else (self.args.module_name or self.args.path.name or "odoo")
        )
        target_db = f"{base_db_name}_{target_ver.replace('.', '_')}_upgrade"

        sandbox_dirs = [str(project_path)]
        extra_bind_dirs = [
            str(worktrees_path),
            str(venvs_path),
        ]
        return target_db, sandbox_dirs, extra_bind_dirs

    def _get_modules_info(self) -> list[dict]:
        """Resolve list of modules to upgrade, including submodules if requested."""
        search_paths = []
        if getattr(self, "_database", None) and hasattr(self._database, "addons_paths"):
            search_paths.extend(self._database.addons_paths)
        else:
            search_paths.append(self.args.path)

        if self.args.submodules:
            try:
                from git import Repo

                repo = Repo(self.args.path, search_parent_directories=True)
                for submodule in repo.submodules:
                    sm_path = Path(repo.working_dir) / submodule.path
                    if sm_path not in search_paths:
                        search_paths.append(sm_path)
            except Exception as e:
                logger.warning(f"Could not find submodules in {self.args.path}: {e}")

        modules_info = self._get_sorted_modules(search_paths)
        if self.args.module_name:
            modules_info = [m for m in modules_info if m["name"] == self.args.module_name]

        if not modules_info:
            raise self.error(f"No modules found in search paths: {[str(p) for p in search_paths]}")
        return modules_info

    def _prepare_upgrade(
        self,
    ) -> tuple[str, list[str], list[str], str, str, str, "KnowledgeIndex | None", list[dict]]:
        """Prepare the upgrade environment and generate the AI prompt."""
        from_ver, target_ver = self._detect_versions()
        project_path = Path(self.args.path).resolve()
        worktrees_path = self.odev.worktrees_path.resolve()
        venvs_path = self.odev.venvs_path.resolve()
        upgrade_path = self.config.paths.upgrade.resolve()

        target_db, sandbox_dirs, extra_bind_dirs = self._get_sandbox_config(
            target_ver, project_path, worktrees_path, venvs_path
        )
        modules_info = self._get_modules_info()

        upgrade_instructions = self._setup_upgrade_instructions(upgrade_path, extra_bind_dirs)
        from_odoo_path = (
            str(worktrees_path / from_ver) if (worktrees_path / from_ver).exists() else f"Virtual ({from_ver})"
        )
        target_odoo_path = str(worktrees_path / target_ver)

        ki, knowledge_local_path = self._setup_knowledge_index_context(modules_info, from_ver, target_ver, upgrade_path)
        if knowledge_local_path:
            sandbox_dirs.append(knowledge_local_path)

        if (self.args.path / "UPGRADE.md").exists():
            logger.info(f"Existing upgrade report found at {self.args.path / 'UPGRADE.md'}")

        # Prepare environment for required versions.
        # Source Odoo is usually researched via git history, so only target is often needed.
        self._prepare_odoo_environment([from_ver, target_ver])

        prompt = self._build_final_prompt(
            from_ver,
            target_ver,
            from_odoo_path,
            target_odoo_path,
            upgrade_instructions,
            knowledge_local_path,
            modules_info,
        )

        return (
            prompt,
            sandbox_dirs,
            extra_bind_dirs,
            from_ver,
            target_ver,
            target_db,
            ki,
            modules_info,
        )

    def _setup_upgrade_instructions(self, upgrade_path: Path, extra_bind_dirs: list[str]) -> str:
        """Manage Odoo Upgrade repository and return instructions for AI."""
        upgrade_connector = GitConnector(ODOO_UPGRADE_REPOSITORY, path=upgrade_path)
        with progress.spinner(f"Managing {ODOO_UPGRADE_REPOSITORY!r} repository"):
            if not upgrade_connector.exists:
                upgrade_connector.clone()
            else:
                upgrade_connector.pull(force=True)
            extra_bind_dirs.append(str(upgrade_path))

        return (
            f"\n- **Migration Scripts (Upgrade Repository)**: You have access to the official Odoo Enterprise "
            f"migration scripts at `{upgrade_path}`. This repository contains the logic used by Odoo's upgrade team. "
            "You MUST search this directory to understand how Odoo handles API changes, field renames, and model "
            "migrations for the modules you are upgrading. Use `grep` or `git grep` within this directory "
            "to find mentions of your module or specific fields/methods that have changed."
        )

    def _setup_knowledge_index_context(
        self,
        modules_info: list[dict],
        from_ver: str,
        target_ver: str,
        upgrade_path: Path,
    ) -> tuple["KnowledgeIndex | None", str | None]:
        """Setup KnowledgeIndex."""
        from odev.common.store.datastore import DataStore

        from odev.plugins.odev_plugin_ai_upgrade.common.knowledge import KnowledgeIndex

        try:
            ki = KnowledgeIndex(self.config, DataStore())
            if not ki.ensure_setup():
                return None, None

            with progress.spinner("Syncing upgrade knowledge repository"):
                ki.clone_or_pull()

            std_deps = self._resolve_standard_deps(modules_info, from_ver)
            if std_deps:
                logger.info(f"Knowledge index: tracking {len(std_deps)} standard Odoo module dependencies.")
            else:
                logger.warning(f"Knowledge index: no standard Odoo dependencies found for {from_ver}.")

            local_path = ki.local_path.resolve().as_posix()
            return ki, local_path
        except Exception as e:
            logger.warning(f"Knowledge index unavailable: {e}. Proceeding without it.")
            return None, None

    def _build_final_prompt(
        self,
        from_ver: str,
        target_ver: str,
        from_odoo_path: str,
        target_odoo_path: str,
        upgrade_instructions: str,
        knowledge_path: str | None,
        modules_info: list[dict],
    ) -> str:
        """Compose the full AI prompt from various components."""
        project_path = Path(self.args.path).resolve()
        repo_name = project_path.name
        is_ps_custom = repo_name.startswith("ps") and repo_name.endswith("-custom")

        template_path = Path(__file__).parent.parent / "templates" / "upgrade_prompt.md.j2"
        with open(template_path, encoding="utf-8") as f:
            template_content = f.read()

        template = jinja2.Template(template_content)
        return template.render(
            from_ver=from_ver,
            target_ver=target_ver,
            from_odoo_path=from_odoo_path,
            target_odoo_path=target_odoo_path,
            project_path=project_path,
            upgrade_instructions=upgrade_instructions,
            k_path=knowledge_path or "/knowledge",
            no_ruff=self.args.no_ruff,
            is_ps_custom=is_ps_custom,
            task_id=self.args.task_id,
            comment=self.args.comment,
            submodules=self.args.submodules,
            modules=modules_info,
            unreachable_customisation=UNREACHABLE_CUSTOMISATION,
        )

    def _check_git_safety(self, repo_path: Path):
        """Perform git safety checks on the repository."""
        connector = GitConnector(str(repo_path))
        if not connector.exists or not connector.is_protected_branch:
            return

        target_ver = self.args.to_version or ""
        if not target_ver:
            raise self.error(
                f"Repository at {repo_path} is on a protected branch ({connector.branch!r}). "
                "AI upgrades should be performed on a feature branch. "
                "Please specify a target version with --to to allow automatic branch creation."
            )

        branch_name = f"{target_ver}-upgrade"
        if xgram := EmployeeUtils(self.odev).get_xgram():
            branch_name += f"-{xgram}"

        from odev.common.console import console

        if console.confirm(
            f"Repository at {repo_path} is on a protected branch ({connector.branch!r}).\n"
            f"Do you want to create and switch to a new feature branch '{branch_name}'?",
            default=True,
        ):
            logger.info(f"Creating and switching to branch {branch_name!r}...")
            try:
                connector.repository.git.checkout("-b", branch_name)
            except Exception as e:
                raise self.error(f"Failed to create branch {branch_name!r}: {e}")
        else:
            raise self.error(f"Protected branch {connector.branch!r} detected. Feature branch required.")

    def _sync_knowledge(self, ki, from_ver: str, target_ver: str):
        """Sync findings back to the knowledge repo as a PR."""
        if not (ki and ki.is_configured()):
            return

        if not self.console.confirm("Sync findings to knowledge index repo as a PR?", default=True):
            return

        branch_safe = re.sub(r"[^a-zA-Z0-9._-]", "-", f"odev/upgrade-knowledge-{from_ver}-{target_ver}")
        pr_url = ki.commit_and_pr(
            branch_name=branch_safe,
            commit_message=f"feat(knowledge): findings for {from_ver}→{target_ver}",
            pr_title=f"[Knowledge] {from_ver} → {target_ver} upgrade findings",
            pr_body=f"Created by `odev upgrade` ({from_ver} to {target_ver}).",
        )
        if pr_url:
            logger.info(f"Knowledge PR created: {pr_url}")

    def _run_upgrade(self) -> None:
        """Internal logic for the upgrade process."""
        repo_path = Path(self.args.path).resolve()
        if not self.args.no_ruff:
            self._check_ruff_cleanliness(repo_path)

        self._check_git_safety(repo_path)
        prepared = self._prepare_upgrade()
        if not prepared:
            return

        (
            prompt,
            sandbox_dirs,
            extra_bind_dirs,
            from_ver,
            target_ver,
            target_db,
            ki,
            _modules_info,
        ) = prepared
        self._target_db = target_db
        repository = self._repository(repo_path)
        self._base_sha = repository.head.commit.hexsha if repository and repository.head.is_valid() else None
        agent = self.get_ai_agent()

        self._cleanup_wizard(stage="pre-flight", exclude=[target_db])

        logger.info(f"Starting Project-wide AI Upgrade: from {from_ver} to {target_ver} ({target_db})")

        # Check for missing upgrade skills via npx skills list -g
        loaded_skills = self._get_loaded_skills()
        missing = [s for s in ["odoo_upgrade_utils", "custom_util", "odoo_upgrade_skill"] if s not in loaded_skills]
        if missing:
            logger.warning(
                f"Missing upgrade skills: {', '.join(missing)}. "
                "To load them, run: npx skills add odoo-ps/ps-ai-skills --skills odoo_upgrade_utils,custom_util,odoo_upgrade_skill"
            )

        if not agent.run(
            prompt,
            sandbox_dirs,
            extra_bind_dirs=extra_bind_dirs,
            database=target_db,
            version=target_ver,
            resume=self.args.resume,
        ):
            return

        self._post_flight_gates(repo_path, target_ver)
        self._sync_knowledge(ki, from_ver, target_ver)

    def _get_upgrade_databases(self) -> list[str]:
        """Return a list of local databases that look like upgrade databases."""
        return [db for db in self.list_databases() if db.endswith("_upgrade")]

    def _cleanup_wizard(self, stage: str = "post-flight", exclude: list[str] | None = None) -> None:
        """Prompt the user to clean up leftover upgrade databases."""
        upgrade_dbs = self._get_upgrade_databases()
        if exclude:
            upgrade_dbs = [db for db in upgrade_dbs if db not in exclude]

        if not upgrade_dbs:
            return

        logger.info(f"\n[{stage}] Found {len(upgrade_dbs)} potential upgrade database(s).")
        to_delete = self.console.checkbox(
            "Select databases to delete:",
            choices=[(db, db) for db in upgrade_dbs],
            defaults=[],
        )

        if not to_delete:
            return

        from odev.common.databases import LocalDatabase

        for db_name in to_delete:
            db = LocalDatabase(db_name)
            if db.exists:
                logger.info(f"Dropping database {db_name!r}...")
                db.drop()

    def _repository(self, repo_path: Path) -> "Repo | None":
        """The project's git repository, via the connector the command already uses."""
        connector = GitConnector(str(repo_path))
        return connector.repository if connector.exists else None

    def _git_text(self, repository: "Repo", *args: str) -> str | None:
        """Run a read-only git command, returning ``None`` when git failed.

        ``None`` and ``""`` mean different things: the command failed, versus it
        succeeded with empty output. Conflating them hides skipped work.

        Read as bytes and decoded with ``errors="replace"``: a single legacy-encoded
        file must not disable a whole gate.
        """
        try:
            # -c belongs in the argv: Git.__call__ options are only consumed by
            # _call_process, and it mutates the repository's shared Git object.
            out = repository.git.execute(
                ["git", "-c", "core.quotePath=false", *args],
                stdout_as_string=False,
                with_extended_output=False,
            )
        except GitCommandError:
            return None
        return out.decode("utf-8", errors="replace") if isinstance(out, bytes) else str(out)

    def _changed_paths(self, repository: "Repo") -> list[tuple[str, str]]:
        """``(before_path, after_path)`` for each file the run changed.

        The two differ for a rename, which plain ``--name-only`` reports as the
        destination alone - the source would then look like a newly added file and
        be skipped by every gate that compares the two revisions.
        """
        if not self._base_sha:
            return []
        out = self._git_text(repository, "diff", "-z", "--name-status", "-M", self._base_sha, "HEAD")
        if not out:
            return []

        fields = [field for field in out.split("\0") if field]
        paths: list[tuple[str, str]] = []
        index = 0
        while index < len(fields):
            status = fields[index]
            if status.startswith(("R", "C")):  # rename/copy: status, source, destination
                if index + 2 >= len(fields):
                    break
                paths.append((fields[index + 1], fields[index + 2]))
                index += 3
            else:
                if index + 1 >= len(fields):
                    break
                path = fields[index + 1]
                paths.append((path, path))
                index += 2
        return paths

    def _target_worktrees(self, target_ver: str) -> dict[str, "Repo"]:
        """The provisioned Odoo checkouts for ``target_ver``, keyed by repository.

        Uses ``GitConnector.worktrees()`` rather than assuming a directory layout:
        a version directory holds one checkout per repository, not a repository.
        """
        found: dict[str, Repo] = {}
        for repo, repositories in (
            ("odoo", ODOO_COMMUNITY_REPOSITORIES),
            ("enterprise", ODOO_ENTERPRISE_REPOSITORIES),
        ):
            connector = GitConnector(repositories[0])
            for worktree in connector.worktrees():
                if worktree.name == target_ver:
                    found[repo] = worktree.repository
                    break
        return found

    def _gate_source_shas(self, repository: "Repo", target_ver: str) -> list[str]:
        """Every ``Source:`` SHA must resolve in a provisioned worktree.

        A citation that cannot be resolved moves the verification cost to the
        reviewer without saying so.
        """
        worktrees = self._target_worktrees(target_ver)
        # NUL-delimited records: a commit body can contain any other byte.
        log = self._git_text(repository, "log", "-z", f"{self._base_sha}..HEAD", "--format=%H%x1f%B") or ""

        findings: list[str] = []
        for entry in filter(None, (e.strip() for e in log.split("\0"))):
            sha, _, body = entry.partition("\x1f")
            for repo, cited in iter_cited_shas(body):
                # Most citations name no repository, and the ones that do are not
                # always right, so accept the commit from any provisioned checkout:
                # the question is whether it exists in the Odoo source, not where.
                candidates = [worktrees[repo]] if repo in worktrees else list(worktrees.values())
                if not candidates:
                    continue  # reported once by _coverage_notes, not per citation
                if not any(self._commit_exists(target, cited) for target in candidates):
                    # An ambiguous abbreviation also fails to resolve, so no
                    # separate length rule is applied: valid citations are
                    # frequently abbreviated.
                    where = repo or "/".join(sorted(worktrees))
                    findings.append(f"{sha[:8]} cites {cited} ({where}): does not resolve")

        return findings

    @staticmethod
    def _commit_exists(repository: "Repo", sha: str) -> bool:
        try:
            repository.commit(sha)
        except (ValueError, BadName, GitCommandError):
            return False
        return True

    def _gate_gutted_overrides(self, repository: "Repo", _target_ver: str) -> list[str]:
        """Flag overrides reduced to a bare ``super()`` call.

        Deleting an obsolete override is correct; leaving a stub that keeps the
        signature while dropping the body silently removes behaviour. The version
        is unused here - the gates share one signature so they can be dispatched
        in a loop.
        """
        findings: list[str] = []
        for before_path, after_path in self._changed_paths(repository):
            if not after_path.endswith(".py"):
                continue
            before = self._git_text(repository, "show", f"{self._base_sha}:{before_path}")
            after = self._git_text(repository, "show", f"HEAD:{after_path}")
            if before is None or after is None:  # added or deleted by the run
                continue
            findings.extend(f"{after_path}::{finding}" for finding in gutted_overrides(before, after))
        return findings

    def _post_flight_gates(self, repo_path: Path, target_ver: str) -> None:
        """Report on the run's own commits. Reads git only; needs no database."""
        repository = self._repository(repo_path)
        if repository is None or not self._base_sha:
            logger.warning(f"Post-flight checks skipped: could not resolve the pre-run HEAD of {repo_path}.")
            return

        findings: list[str] = []
        notes: list[str] = self._coverage_notes(repository, target_ver)
        with progress.spinner("Running post-flight checks"):
            for gate in (self._gate_source_shas, self._gate_gutted_overrides):
                try:
                    findings.extend(gate(repository, target_ver))
                except Exception as e:  # noqa: BLE001 - a broken check must not abort the run
                    notes.append(f"{gate.__name__} did not complete: {e}")

        if notes:
            # A gap in the environment is not a defect in the run, so it never trips
            # --strict-gates - but it must not read as a clean pass either.
            logger.warning("Not checked:\n" + "\n".join(f"  - {note}" for note in notes))

        if not findings:
            logger.info("Post-flight checks passed." if not notes else "Post-flight checks passed, with gaps above.")
            return

        message = f"{len(findings)} post-flight finding(s):\n" + "\n".join(f"  - {f}" for f in findings)
        if self.args.strict_gates:
            # Before the knowledge sync on purpose: that harvests `Source:` SHAs, and an
            # unresolvable citation must not reach the knowledge base.
            raise self.error(f"{message}\nKnowledge sync skipped. Re-run without --strict-gates to sync anyway.")
        logger.warning(message)

    def _coverage_notes(self, repository: "Repo", target_ver: str) -> list[str]:
        """State what the gates could not look at, so a pass is never mistaken for coverage."""
        notes: list[str] = []

        if repository.head.is_valid() and repository.head.commit.hexsha == self._base_sha:
            notes.append("the run produced no commits, so nothing was inspected")
        if repository.is_dirty(untracked_files=True):
            notes.append("uncommitted changes are present and were not inspected (gates read committed state only)")

        missing = sorted({"odoo", "enterprise"} - set(self._target_worktrees(target_ver)))
        if missing:
            notes.append(f"citations against {', '.join(missing)} could not be checked: no worktree for {target_ver}")
        if self.args.submodules:
            notes.append("submodule contents were not inspected: the parent repository records only a gitlink")
        return notes

    def _check_ruff_cleanliness(self, repo_path: Path):
        """Check if the module has many linting errors before starting."""
        if not shutil.which("ruff"):
            return

        with progress.spinner("Checking module linting cleanliness"):
            try:
                diff_process = subprocess.run(
                    ["ruff", "check", str(repo_path), "--diff", "--exit-zero"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                diff_lines = diff_process.stdout.count("\n")
                if diff_lines > 50:
                    logger.warning(
                        f"\nWARNING: Module at {repo_path} has many linting issues "
                        f"({diff_lines} lines affected by ruff).\n"
                        "Running ruff will result in a very noisy git diff.\n"
                        "Use --no-ruff to disable automatic linting if desired."
                    )
                    if not self.args.yolo and not self.console.confirm(
                        "Continue with automatic ruff instructions anyway?",
                        default=True,
                    ):
                        self.args.no_ruff = True
                        logger.info("Automatic ruff instructions disabled.")
            except Exception as e:
                logger.debug(f"Ruff cleanliness check failed: {e}")
