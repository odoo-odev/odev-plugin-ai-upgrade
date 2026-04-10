"""Upgrade Odoo modules using AI."""

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import networkx as nx

from odev.common import args, progress
from odev.common.commands import DatabaseCommand
from odev.common.connectors import GitConnector
from odev.common.logging import logging
from odev.common.mixins.databases.list import ListLocalDatabasesMixin
from odev.common.odoobin import ODOO_UPGRADE_REPOSITORY, OdoobinProcess
from odev.common.utils import EmployeeUtils

from odev.plugins.odev_plugin_ai.common.mixins import AICommandMixin


if TYPE_CHECKING:
    from odev.plugins.odev_plugin_ai_upgrade.common.knowledge import KnowledgeIndex


logger = logging.getLogger(__name__)


class UpgradeCommand(DatabaseCommand, ListLocalDatabasesMixin, AICommandMixin):
    """Upgrades an Odoo module from a previous version to a new version using an AI model.

    This command runs in a loop, attempting to fix errors by editing files and re-running tests.
    """

    _name = "upgrade"
    _database_arg_required = False
    _target_db: str | None = None

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

    def _setup_path_mapping(
        self,
        project_path: Path,
        upgrade_path: Path,
        skills_path: Path,
    ) -> dict[str, str]:
        """Map host paths to guest paths for the AI sandbox."""
        return {
            str(project_path): "/custom",
            str(upgrade_path): "/upgrade",
            str(skills_path): "/skills",
        }

    def _get_map_path_func(self, path_mapping: dict[str, str]):
        """Return a function that maps host paths to guest paths."""

        def map_path(p: Path | str) -> str:
            p_str = str(p)
            # Sort by length descending to match longest prefix first
            sorted_mappings = sorted(path_mapping.items(), key=lambda x: len(x[0]), reverse=True)
            for host, guest in sorted_mappings:
                if p_str.startswith(host):
                    return p_str.replace(host, guest)
            return p_str

        return map_path

    def _get_sandbox_config(
        self,
        target_ver: str,
        project_path: Path,
        worktrees_path: Path,
        venvs_path: Path,
    ) -> tuple[str, list[str], list[str]]:
        """Configure target database and sandbox directories."""
        base_db_name = (
            self._database.name
            if getattr(self, "_database", None) and self._database.platform.name != "dummy"
            else (self.args.module_name or self.args.path.name or "odoo")
        )
        target_db = f"{base_db_name}_{target_ver.replace('.', '_')}_upgrade"

        sandbox_dirs = [f"{project_path}:/custom"]
        extra_bind_dirs = [
            f"{worktrees_path}:{worktrees_path}",
            f"{venvs_path}:{venvs_path}",
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
        if not modules_info:
            raise self.error(f"No modules found in search paths: {[str(p) for p in search_paths]}")
        return modules_info

    def _prepare_upgrade(
        self,
    ) -> (tuple[str, list[str], list[str], str, str, str, "KnowledgeIndex | None", list[dict], dict[str, str]] | None):
        """Prepare the upgrade environment and generate the AI prompt."""
        from_ver, target_ver = self._detect_versions()
        project_path = Path(self.args.path).resolve()
        worktrees_path = self.odev.worktrees_path.resolve()
        venvs_path = self.odev.venvs_path.resolve()
        upgrade_path = self.config.paths.upgrade.resolve()
        skills_path = (Path(__file__).parent.parent / "skills").resolve()

        path_mapping = self._setup_path_mapping(project_path, upgrade_path, skills_path)
        self._get_map_path_func(path_mapping)

        target_db, sandbox_dirs, extra_bind_dirs = self._get_sandbox_config(
            target_ver, project_path, worktrees_path, venvs_path
        )
        modules_info = self._get_modules_info()

        upgrade_instructions = self._setup_upgrade_instructions(upgrade_path, extra_bind_dirs)
        from_odoo_path = (
            str(worktrees_path / from_ver) if (worktrees_path / from_ver).exists() else f"Virtual ({from_ver})"
        )
        target_odoo_path = str(worktrees_path / target_ver)

        ki, knowledge_context, knowledge_local_path = self._setup_knowledge_index_context(
            modules_info, from_ver, target_ver, upgrade_path, path_mapping
        )
        if knowledge_local_path:
            sandbox_dirs.append(f"{knowledge_local_path}:/knowledge")

        if (self.args.path / "UPGRADE.md").exists():
            logger.info(f"Existing upgrade report found at {self.args.path / 'UPGRADE.md'}")

        # Prepare environment for required versions.
        # Source Odoo is usually researched via git history, so only target is often needed.
        self._prepare_worktrees([target_ver])

        prompt = self._build_final_prompt(
            from_ver, target_ver, from_odoo_path, target_odoo_path, target_db, upgrade_instructions, knowledge_context
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
            path_mapping,
        )

    def _setup_upgrade_instructions(self, upgrade_path: Path, extra_bind_dirs: list[str]) -> str:
        """Manage Odoo Upgrade repository and return instructions for AI."""
        upgrade_connector = GitConnector(ODOO_UPGRADE_REPOSITORY, path=upgrade_path)
        with progress.spinner(f"Managing {ODOO_UPGRADE_REPOSITORY!r} repository"):
            if not upgrade_connector.exists:
                upgrade_connector.clone()
            else:
                upgrade_connector.pull(force=True)
            extra_bind_dirs.append(f"{upgrade_path}:/upgrade")

        return (
            "\n- **Migration Scripts (Upgrade Repository)**: You have access to the official Odoo Enterprise "
            "migration scripts at `/upgrade`. This repository contains the logic used by Odoo's upgrade team. "
            "You MUST search this directory to understand how Odoo handles API changes, field renames, and model "
            "migrations for the modules you are upgrading. Use `grep` or `git grep` within this directory "
            "to find mentions of your module or specific fields/methods that have changed."
        )

    def _prepare_worktrees(self, versions: list[str]) -> None:
        """Ensure all required Odoo worktrees are present and up to date."""
        for version in versions:
            logger.info(f"Preparing environment for Odoo {version}...")
            if not (self.odev.worktrees_path / version).exists():
                self.odev.run_command("worktree", "-C", version, "-V", version)
            self.odev.run_command("pull", "-V", version)

    def _setup_knowledge_index_context(
        self,
        modules_info: list[dict],
        from_ver: str,
        target_ver: str,
        upgrade_path: Path,
        path_mapping: dict[str, str],
    ) -> tuple["KnowledgeIndex | None", str, str | None]:
        """Setup KnowledgeIndex and load relevant context."""
        from odev.common.store.datastore import DataStore

        from odev.plugins.odev_plugin_ai_upgrade.common.knowledge import KnowledgeIndex

        try:
            ki = KnowledgeIndex(self.config, DataStore())
            if not ki.ensure_setup():
                return None, "", None

            with progress.spinner("Syncing upgrade knowledge repository"):
                ki.clone_or_pull()

            std_deps = self._resolve_standard_deps(modules_info, from_ver)
            if std_deps:
                logger.info(f"Knowledge index: tracking {len(std_deps)} standard Odoo module dependencies.")
            else:
                logger.warning(f"Knowledge index: no standard Odoo dependencies found for {from_ver}.")

            pairs = ki.get_version_pairs(from_ver, target_ver, upgrade_path=upgrade_path, modules=std_deps)
            context = ki.load_knowledge(std_deps, pairs) if pairs and std_deps else ""

            if context:
                logger.info("Knowledge index: loaded existing upgrade context for AI prompt.")
            else:
                logger.info("Knowledge index: No existing notes found. AI will discover findings.")

            local_path = ki.local_path.as_posix()
            path_mapping[local_path] = "/knowledge"
            return ki, context, local_path
        except Exception as e:
            logger.warning(f"Knowledge index unavailable: {e}. Proceeding without it.")
            return None, "", None

    def _build_final_prompt(
        self,
        from_ver: str,
        target_ver: str,
        from_odoo_path: str,
        target_odoo_path: str,
        target_db: str,
        upgrade_instructions: str,
        knowledge_context: str,
    ) -> str:
        """Compose the full AI prompt from various components."""
        repo_name = Path(self.args.path).resolve().name
        is_ps_custom = repo_name.startswith("ps") and repo_name.endswith("-custom")

        if is_ps_custom:
            fast_verify = (
                "- **Verification (Fast)**: Verify the module installs cleanly using: "
                "`odev deploy <module_name>`. (Assume one instance is already running with `odev run`). "
                "Then, verify the presence of new fields or view rendering."
            )
        else:
            fast_verify = (
                f"- **Verification (Fast)**: Verify the module installs cleanly (including demo data) "
                f"using: `odev run` (Target DB: `{target_db}`). "
                "Then, verify the presence of new fields, view rendering, or basic logic via a minimal manual check."
            )

        instructions = ""
        if not self.args.no_ruff:
            instructions = " You MUST run `ruff check --fix <file>` after editing any Python file."

        knowledge_prefix = f"{knowledge_context}\n\n---\n\n" if knowledge_context else ""

        full_prompt = f"""{knowledge_prefix}You are an expert Odoo Upgrade Lead.
Your task is to upgrade multiple Odoo modules from version {from_ver} to {target_ver}.

### Core Protocol:
1. **Efficiency Protocol**: Skip narrating routine discovery steps.
2. **Expert Autonomy**: Align with Target Odoo {target_ver} core standards.
3. **Atomic Integrity**: Update all Python, XML, and JS references in a single atomic commit.

### Context:
- **Source Odoo**: Version {from_ver} at `{from_odoo_path}`.
- **Target Odoo**: Version {target_ver} at `{target_odoo_path}`.
- **Project Root**: `/custom`
- **Target Database**: `{target_db}` (Use this for all installations and tests).
- **Upgrade Knowledge Base**: `/knowledge` (Read/Write access).{upgrade_instructions}

### Standard Odoo Migration Rules:
- For Odoo >= 18.0, use `odev upgrade-code --from {from_ver} --to {target_ver} {target_db}`.
- Search for version-specific migration scripts in `odoo/odoo/upgrade_code` within Core.
- **MANDATORY**: Refer to the following Skills for available migration helpers and CLI usage:
  - **Standard Odoo Upgrade Helpers**: `/skills/odoo_upgrade_utils`
  - **PS Custom Helpers**: `/skills/custom_util`
  - **ODEV CLI Usage**: `/skills/odev`

### Your Process:
1. **Analyze & Plan**: Maintain a `TASKS.md` in the root with a detailed checklist.
2. **Research & Execution**:
   - **Deep Git Research**: In Target Odoo, research changes (`git log -S`, `git show`).
   - **Quality Audit**: Prioritize REPLACING old custom patterns with {target_ver} standards.{instructions}
   - **Data Migrations**: Create scripts in `migrations/{target_ver}/` using `from odoo.upgrade import util`.
   - **Comprehensive Impact Analysis**: Group related changes (model + view) into atomic commits.
3. **Verification**:
   {fast_verify}
   - **Log Audit**: Check for `Registry` load failures, `TypeError`, or `AttributeError`.
   - **Test Intent Preservation**: Keep existing tests but adapt syntax for the new version.

### Committing Rules:
1. **One commit per discrete change or fix**.
2. **Format**: `[UPG][{self.args.task_id}] module_name: Concise description`
   - **Body**: Detailed description. Cite "Source" (Commit ID, PR, or Core file path).

### 📓 Knowledge Write-Back & Reporting (MANDATORY)
1. **Knowledge Base**: Update `/knowledge/<dep_module>.md` with technical findings.
2. **Upgrade Log**: Update `UPGRADE.md`. Cite Source (Commit ID, PR, or Core file path).
3. **Task Tracking**: Mark items as `[x]` in `TASKS.md`.
"""
        if self.args.comment:
            full_prompt += f"\n### Additional User Instructions:\n- {self.args.comment}\n"

        if self.args.submodules:
            full_prompt += (
                "\n- **Submodules Usage**: You are authorized to upgrade modules found in git submodules. "
                "Ensure you commit changes within the respective submodule repositories.\n"
            )

        return full_prompt

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

    def _ensure_target_db(self, target_db: str):
        """Ensure target database exists on host before letting the AI work on it."""
        from odev.common.databases import LocalDatabase

        target_db_obj = LocalDatabase(target_db)
        if target_db_obj.exists:
            return

        db_to_clone = (
            self._database.name
            if getattr(self, "_database", None) and self._database.platform.name != "dummy"
            else None
        )

        if db_to_clone:
            logger.info(f"Cloning host database {db_to_clone!r} to {target_db!r} for upgrade...")
            target_db_obj.create(template=db_to_clone)
        else:
            logger.info(f"Creating empty host database {target_db!r} for upgrade...")
            target_db_obj.create()

    def _verification_loop(self, agent, modules_to_test: str, target_db: str, target_ver: str):
        """Run verification tests and offer AI-fixes in a loop."""
        session_id = (self.args.resume or agent.get_latest_session_id()) if agent else None
        while self.console.confirm(
            f"Would you like to run the full test suite for analysis? (Modules: {modules_to_test})",
            default=True,
        ):
            test_args = ["test", "--ai", target_db, "-V", target_ver, "-i", modules_to_test]
            if session_id:
                test_args.extend(["--resume", session_id])

            logger.info(f"Launching verification tests: odev {' '.join(test_args)}")
            self.odev.run_command(*test_args)
            if agent:
                session_id = agent.get_latest_session_id() or session_id

    def _sync_knowledge(self, ki, from_ver: str, target_ver: str):
        """Sync findings back to the knowledge repo as a PR."""
        if not (ki and ki.is_configured()):
            return

        if not self.console.confirm("Sync findings to knowledge index repo as a PR?", default=True):
            return

        import re as _re

        branch_safe = _re.sub(r"[^a-zA-Z0-9._-]", "-", f"odev/upgrade-knowledge-{from_ver}-{target_ver}")
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
            modules_info,
            path_mapping,
        ) = prepared
        self._target_db = target_db
        agent = self.get_ai_agent()

        self._cleanup_wizard(stage="pre-flight", exclude=[target_db])
        self._ensure_target_db(target_db)

        logger.info(f"Starting Project-wide AI Upgrade: from {from_ver} to {target_ver} ({target_db})")
        if not agent.run(
            prompt,
            sandbox_dirs,
            extra_bind_dirs=extra_bind_dirs,
            database=target_db,
            version=target_ver,
            resume=self.args.resume,
            path_mapping=path_mapping,
            ephemeral_pg=True,
        ):
            return

        modules_to_test = ",".join([m["name"] for m in modules_info])
        self._verification_loop(agent, modules_to_test, target_db, target_ver)
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

    def _check_ruff_cleanliness(self, repo_path: Path):
        """Check if the module has many linting errors before starting."""
        if not shutil.which("ruff"):
            return

        with progress.spinner("Checking module linting cleanliness"):
            try:
                diff_process = subprocess.run(
                    ["ruff", "check", str(repo_path), "--diff", "--exit-zero"],
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
