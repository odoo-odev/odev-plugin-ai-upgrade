"""Upgrade Odoo modules using AI."""

import shutil
import socket
import subprocess
import tempfile
from contextlib import contextmanager
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

    def _get_xgram(self) -> str | None:
        """Get the user's xgram from their Odoo email."""
        return EmployeeUtils(self.odev).get_xgram()

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

    def _get_free_port(self) -> int:
        """Find a free port on the host."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            return s.getsockname()[1]

    @staticmethod
    def _resolve_standard_deps(
        modules_info: list[dict],
        from_ver: str,
    ) -> dict[str, str]:
        """Return the transitive standard Odoo dependencies of the given custom modules.

        Walks depends transitively. Only modules found in the community (``odoo/odoo``)
        or enterprise (``odoo/enterprise``) worktree for ``from_ver`` are included.
        Custom repo modules are excluded.

        :returns: ``{module_name: "community" | "enterprise"}``
        """
        import ast

        from odev.common.odoobin import ODOO_COMMUNITY_REPOSITORIES, ODOO_ENTERPRISE_REPOSITORIES

        custom_names: set[str] = {m["name"] for m in modules_info}

        # Build addons-path lookup maps for the source version worktree
        # Community: worktree/<ver>/odoo/addons  +  worktree/<ver>/odoo/odoo/addons (older layout)
        # Enterprise: worktree/<ver>/enterprise/
        community_connector = GitConnector(ODOO_COMMUNITY_REPOSITORIES[0])
        enterprise_connector = GitConnector(ODOO_ENTERPRISE_REPOSITORIES[0])

        def _addons_roots(connector: "GitConnector", version: str) -> list[Path]:
            roots: list[Path] = []
            for wt in connector.worktrees():
                if wt.name == version:
                    for sub in ["", "addons"]:
                        candidate = wt.path / sub if sub else wt.path
                        if OdoobinProcess.check_addons_path(candidate):
                            roots.append(candidate)
            return roots

        community_roots = _addons_roots(community_connector, from_ver)
        enterprise_roots = _addons_roots(enterprise_connector, from_ver)

        def _find_module(name: str) -> tuple[dict, str] | None:
            """Return (manifest_dict, kind) if module is found in an Odoo tree."""
            # Try existing worktrees first (faster)
            for root in community_roots:
                candidate = root / name / "__manifest__.py"
                if candidate.exists():
                    manifest = OdoobinProcess.read_manifest(candidate)
                    if manifest:
                        return manifest, "community"
            for root in enterprise_roots:
                candidate = root / name / "__manifest__.py"
                if candidate.exists():
                    manifest = OdoobinProcess.read_manifest(candidate)
                    if manifest:
                        return manifest, "enterprise"

            # Fallback: research via git (no worktree required)
            for connector, kind in [
                (community_connector, "community"),
                (enterprise_connector, "enterprise"),
            ]:
                for sub in ["", "addons"]:
                    path = f"{sub}/{name}/__manifest__.py" if sub else f"{name}/__manifest__.py"
                    try:
                        content = connector.repository.git.show(f"{from_ver}:{path}")
                        manifest = ast.literal_eval(content)
                        if isinstance(manifest, dict):
                            return manifest, kind
                    except Exception:
                        continue
            return None

        resolved: dict[str, str] = {}
        queue: set[str] = set()

        # Seed the queue with direct depends of every custom module
        for m in modules_info:
            queue.update(m.get("depends", []))

        while queue:
            mod = queue.pop()
            if mod in resolved or mod in custom_names:
                continue
            found = _find_module(mod)
            if found:
                manifest, kind = found
                resolved[mod] = kind
                queue.update(manifest.get("depends", []))
            # third-party / not found → silently skip

        return resolved

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

    def _prepare_upgrade(
        self,
    ) -> (tuple[str, list[str], list[str], str, str, str, "KnowledgeIndex | None", list[dict], dict[str, str],] | None):
        """Prepare the upgrade environment and generate the AI prompt."""
        from_ver = (
            self.args.from_version
            or OdoobinProcess.version_from_manifest(self.args.path)
            or OdoobinProcess.version_from_addons(self.args.path)
            or self._database.version
        )

        if not from_ver:
            logger.error(
                f"Could not determine source version from path '{self.args.path}' or database '{self._database.name}'. "
                "Ensure the path contains valid Odoo modules or use --from <version>."
            )
            return None

        from_ver = str(from_ver)
        logger.info(f"Detected source version: {from_ver}")

        target_ver = self.args.to_version or ""
        if not target_ver:
            logger.error("Could not determine target version. Please specify a --to <version>.")
            return None

        project_path = Path(self.args.path).resolve()
        worktrees_path = self.odev.worktrees_path.resolve()
        venvs_path = self.odev.venvs_path.resolve()
        upgrade_path = self.config.paths.upgrade.resolve()
        skills_path = (Path(__file__).parent.parent / "skills").resolve()

        path_mapping = {
            str(project_path): "/custom",
            str(upgrade_path): "/upgrade",
            str(skills_path): "/skills",
        }

        # Avoid root mapping of repositories to protect privacy of siblings
        from odev.common.odoobin import odoo_repositories

        for repo in odoo_repositories(enterprise=True):
            path_mapping[str(repo.path.resolve())] = f"/repositories/{repo.path.name}"

        def map_path(p: Path | str) -> str:
            p_str = str(p)
            # Sort by length descending to match longest prefix first
            for host, guest in sorted(path_mapping.items(), key=lambda x: len(x[0]), reverse=True):
                if p_str.startswith(host):
                    return p_str.replace(host, guest)
            return p_str

        sandbox_dirs = [
            f"{project_path}:/custom",
        ]
        extra_bind_dirs = [
            f"{worktrees_path}:{worktrees_path}",
            f"{venvs_path}:{venvs_path}",
        ]

        base_db_name = (
            self._database.name
            if self._database.platform.name != "dummy"
            else (self.args.module_name or self.args.path.name or "odoo")
        )
        target_db = f"{base_db_name}_{target_ver.replace('.', '_')}_upgrade"

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
            logger.error(f"No modules found in search paths: {[str(p) for p in search_paths]}")
            return None

        # Manage Odoo Upgrade (migration scripts) repository
        upgrade_path = self.config.paths.upgrade
        upgrade_instructions = ""

        upgrade_connector = GitConnector(ODOO_UPGRADE_REPOSITORY, path=upgrade_path)
        with progress.spinner(f"Managing {ODOO_UPGRADE_REPOSITORY!r} repository"):
            if not upgrade_connector.exists:
                upgrade_connector.clone()
            else:
                upgrade_connector.pull(force=True)
            extra_bind_dirs.append(f"{upgrade_path}:/upgrade")

        upgrade_instructions = (
            "\n- **Migration Scripts (Upgrade Repository)**: You have access to the official Odoo Enterprise migration scripts at `/upgrade`. "
            "This repository contains the logic used by Odoo's upgrade team. "
            "You MUST search this directory to understand how Odoo handles API changes, field renames, and model migrations for the modules you are upgrading. "
            "Use `grep` or `git grep` within this directory to find mentions of your module or specific fields/methods that have changed."
        )

        # --- Resolve source and target paths for prompt context ---------------------
        try:
            # Only resolve path if worktree already exists to avoid auto-triggering creation
            if (worktrees_path / from_ver).exists():
                from_odoo_path = str(worktrees_path / from_ver)
                extra_bind_dirs.append(f"{worktrees_path}/{from_ver}:{worktrees_path}/{from_ver}")
            else:
                from_odoo_path = f"Virtual (Ref: {from_ver} in Target Odoo)"
        except Exception:
            from_odoo_path = "Unknown"

        try:
            target_odoo_path = str(worktrees_path / target_ver)
            extra_bind_dirs.append(f"{worktrees_path}/{target_ver}:{worktrees_path}/{target_ver}")
        except Exception:
            target_odoo_path = str(worktrees_path / target_ver)

        # --- Knowledge Index integration -------------------------------------------
        from odev.common.store.datastore import DataStore

        from odev.plugins.odev_plugin_ai_upgrade.common.knowledge import KnowledgeIndex

        ki: KnowledgeIndex | None = None
        knowledge_context = ""
        knowledge_local_path: str | None = None
        version_pairs: dict = {}
        standard_deps: dict = {}

        try:
            ki = KnowledgeIndex(self.config, DataStore())
            if ki.ensure_setup():
                with progress.spinner("Syncing upgrade knowledge repository"):
                    ki.clone_or_pull()

                # Resolve standard (community/enterprise) deps — NOT the custom modules
                standard_deps = self._resolve_standard_deps(modules_info, from_ver)
                if standard_deps:
                    logger.info(f"Knowledge index: tracking {len(standard_deps)} standard Odoo module dependencies.")
                else:
                    logger.warning(
                        "Knowledge index: no standard Odoo dependencies resolved. "
                        f"Ensure worktree for {from_ver!r} is available."
                    )

                # Discover intermediate steps via KnowledgeIndex fallback (no explicit version discovery)
                version_pairs = ki.get_version_pairs(
                    from_ver,
                    target_ver,
                    upgrade_path=upgrade_path,
                    modules=standard_deps,
                )

                # Load existing knowledge as context — no stubs, no Phase 1
                if version_pairs and standard_deps:
                    knowledge_context = ki.load_knowledge(standard_deps, version_pairs)
                    if knowledge_context:
                        logger.info("Knowledge index: loaded existing upgrade context for AI prompt.")
                    else:
                        logger.info(
                            "Knowledge index: No existing notes found for these modules yet. The AI will discover and record findings during the upgrade."
                        )
                knowledge_local_path = ki.local_path.as_posix()
                path_mapping[knowledge_local_path] = "/knowledge"
            else:
                ki = None  # User skipped setup — proceed without KI
        except Exception as e:
            logger.warning(f"Knowledge index unavailable: {e}. Proceeding without it.")
            ki = None
        # ---------------------------------------------------------------------------

        report_path = self.args.path / "UPGRADE.md"
        report_exists = report_path.exists()
        if report_exists:
            logger.info(f"Existing upgrade report found at {report_path}")

        # Prepare environment for TARGET version.
        # Source Odoo is researched via git history in the target repo to avoid "useless worktree" creation.
        version_list = [target_ver]

        for version in version_list:
            logger.info(f"Preparing environment for Odoo {version}...")
            if not (self.odev.worktrees_path / version).exists():
                self.odev.run_command("worktree", "-C", version, "-V", version)
            self.odev.run_command("pull", "-V", version)

        # Prepare module context for AI

        repo_name = Path(self.args.path).resolve().name
        is_ps_custom_external = repo_name.startswith("ps") and repo_name.endswith("-custom")

        if is_ps_custom_external:
            fast_verify = "- **Verification (Fast)**: Verify the module installs cleanly using: `odev deploy <module_name>`. (Assume one instance is already running with `odev run`). Then, verify the presence of new fields or view rendering."
        else:
            fast_verify = f"- **Verification (Fast)**: Verify the module installs cleanly (including demo data) using: `odev run` (Target DB: `{target_db}`). Then, verify the presence of new fields, view rendering, or basic logic via a minimal manual check."

        # Build the full prompt: existing knowledge context + upgrade instructions
        knowledge_prefix = ""
        if knowledge_context:
            knowledge_prefix = knowledge_context + "\n\n---\n\n"

        prompt = (
            knowledge_prefix
            + f"""You are an expert Odoo Upgrade Lead. Your task is to upgrade multiple Odoo modules from version {from_ver} to {target_ver}.

### Core Protocol:
1. **Efficiency Protocol**: Skip narrating routine discovery steps (like `ls` or `view_file`). Only use the chat to describe the overall plan or to justify complex logic changes.
2. **Expert Autonomy**: You are an expert. If the Target Odoo core code shows a new pattern (e.g., a refactored API or UI element), apply it proactively to custom code. "Aligned with Target Odoo {target_ver} core standards" is sufficient justification for technical modernizations.
3. **Atomic Integrity**: Changes must be idiomatically complete. If a field is renamed, you MUST update all Python, XML, and JS references in a single atomic commit.

### Context:
- **Source Odoo**: Version {from_ver} at `{from_odoo_path}`.
- **Target Odoo**: Version {target_ver} at `{target_odoo_path}`.
- **Project Root**: `/custom`
- **Target Database**: `{target_db}` (Use this for all installations and tests).
- **Upgrade Knowledge Base**: `/knowledge` (Read/Write access).{upgrade_instructions}

### Standard Odoo Migration Rules:
- For Odoo >= 18.0, you SHOULD first try to use `odev upgrade-code --from {from_ver} --to {target_ver} {target_db} --glob "your_module/**/*"`.
- You MUST also search for version-specific migration scripts in `odoo/odoo/upgrade_code` within the Target Odoo repository.
- **MANDATORY**: Refer to the following Skills for available migration helpers and CLI usage:
  - **Standard Odoo Upgrade Helpers**: `/skills/odoo_upgrade_utils`
  - **PS Custom Helpers**: `/skills/custom_util`
  - **ODEV CLI Usage**: `/skills/odev` (Includes mandatory rules for `create`, `run`, `test`, `venv`, and `upgrade-code` quoting)

### Your Process:
1. **Analyze & Plan**: Analyze modules and dependencies. Maintain a `TASKS.md` in the root with a detailed checklist.
   - **Verify Against Standard**: If custom logic is now a standard feature in Odoo {target_ver}, replace custom code with the standard implementation.
2. **Research & Execution**:
   - **Deep Git Research**: In the Target Odoo repository, research how logic changed between versions (`git log -S`, `git show`, `gh pr view`).
   - **Modernization & Quality Audit**: An upgrade is the best opportunity to reduce technical debt. Prioritize REPLACING old custom patterns with new Odoo {target_ver} standards.{" You MUST run `ruff check --fix <file>` after editing any Python file." if not self.args.no_ruff else ""}
   - **Data Migrations**: Create scripts in `<module>/migrations/{target_ver}/` for structural changes (field renames, model moves) using `from odoo.upgrade import util`.
   - **Comprehensive Impact Analysis**: When a symbol changes in Core, scan the entire module (grep) to update all Python, XML, and JS occurrences simultaneously.
   - **Systematic Audit**: Recursively audit all files (JS, XML, Tours, Python hooks). Compare overrides (`read_group`, `search`, etc.) with Target Core to catch silent behavioral shifts.
3. **Verification**:
   {fast_verify}
   - **Log Audit**: Inspect the full log. Successful exit status != correct registry load. Explicitly check for `Registry` load failures, `TypeError`, or `AttributeError`.
   - **Test Intent Preservation**: Keep existing tests but adapt syntax for the new version. **DO NOT** change the core business flow they test.
   - **Standard Core Failures**: If Core fails BECAUSE of your overrides, you MUST fix/monkey-patch it. If UNRELATED to your changes, DO NOT attempt to fix it.

### Committing Rules:
1. **One commit per discrete change or fix**.
2. **Format**: `[UPG][{self.args.task_id}] module_name: Concise description`
   - **Body**: Detailed description of the logic change. You MUST include a **Source Commit ID**, **PR Number**, or **Core File Reference** (e.g. "Verified against Odoo core version {target_ver} file <path>").
3. **Atomic Upgrades**: Group related changes (e.g., model + view for a field rename) into a single atomic commit.

### 📓 Knowledge Write-Back & Reporting (MANDATORY — after each module)
1. **Knowledge Base**: Update `/knowledge/<dep_module>.md` with technical findings (bullet points only). Highlight major architectural shifts.
2. **Upgrade Log**: Update `UPGRADE.md` in the project root. Every Odoo-specific change MUST cite a "Source" (Commit ID, PR, or Core file path).
3. **Task Tracking**: Mark items as `[x]` in `TASKS.md`.
"""
        )
        if self.args.comment:
            prompt += f"\n### Additional User Instructions:\n- {self.args.comment}\n"

        if self.args.submodules:
            prompt += "\n- **Submodules Usage**: You are authorized to upgrade modules found in git submodules. Ensure you commit changes within the respective submodule repositories.\n"

        # Allow the AI to write into the knowledge repo when populating stubs
        if knowledge_local_path:
            sandbox_dirs.append(f"{knowledge_local_path}:/knowledge")

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

    @contextmanager
    def _ephemeral_postgresql(self, db_to_clone: str | None = None):
        """Context manager to start and stop an ephemeral PostgreSQL cluster."""
        pg_dir = Path(tempfile.mkdtemp(prefix="odev-pg-"))
        pg_socket = Path(tempfile.mkdtemp(prefix="odev-pg-socket-"))
        pg_log = pg_dir / "postgresql.log"

        try:
            if not self.args.headless:
                logger.info("Initializing ephemeral PostgreSQL cluster...")
            subprocess.run(["initdb", "-D", str(pg_dir)], check=True, capture_output=True)

            logger.info("Starting ephemeral PostgreSQL cluster...")
            try:
                subprocess.run(
                    [
                        "pg_ctl",
                        "-D",
                        str(pg_dir),
                        "-l",
                        str(pg_log),
                        "-o",
                        f"-c listen_addresses='' -k {pg_socket}",
                        "start",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )

                # Manual wait for ready state
                import time

                ready = False
                for _ in range(30):  # 15 seconds max
                    res = subprocess.run(["pg_isready", "-h", str(pg_socket)], capture_output=True)
                    if res.returncode == 0:
                        ready = True
                        break
                    time.sleep(0.5)

                if not ready:
                    log_content = pg_log.read_text() if pg_log.exists() else "No log file found."
                    logger.error(f"Ephemeral PostgreSQL failed to start in time. Log:\n{log_content}")
                    raise RuntimeError("PostgreSQL cluster failed to become ready.")

            except subprocess.CalledProcessError as e:
                log_content = pg_log.read_text() if pg_log.exists() else "No log file found."
                logger.error(
                    f"Failed to start ephemeral PostgreSQL cluster: {e.stderr or e.stdout}\nLog:\n{log_content}"
                )
                raise

            # Get existing databases to clone
            res_db = subprocess.run(["psql", "-ltq"], capture_output=True, text=True)
            existing_dbs = [line.split("|")[0].strip() for line in res_db.stdout.splitlines() if line.strip()]

            # Clone 'odev' if it exists
            if "odev" in existing_dbs:
                logger.info("Cloning 'odev' database into ephemeral cluster (Sandbox isolation)...")
                subprocess.run(["createdb", "-h", str(pg_socket), "odev"], check=True)
                subprocess.run(
                    f"pg_dump odev | psql -h {pg_socket} -d odev",
                    shell=True,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

            # Clone target database if specified
            if db_to_clone and db_to_clone in existing_dbs and db_to_clone != "odev":
                logger.info(f"Cloning target database {db_to_clone!r} into ephemeral cluster...")
                subprocess.run(["createdb", "-h", str(pg_socket), db_to_clone], check=False)
                subprocess.run(
                    f"pg_dump {db_to_clone} | psql -h {pg_socket} -d {db_to_clone}",
                    shell=True,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

            yield pg_socket

        finally:
            logger.info("Stopping ephemeral PostgreSQL cluster...")
            subprocess.run(["pg_ctl", "-D", str(pg_dir), "stop"], check=False, capture_output=True)
            shutil.rmtree(pg_dir, ignore_errors=True)
            shutil.rmtree(pg_socket, ignore_errors=True)

    def _run_upgrade(self) -> None:
        """Internal logic for the upgrade process."""
        # Git safety checks
        repo_path = Path(self.args.path).resolve()

        if not self.args.no_ruff:
            self._check_ruff_cleanliness(repo_path)

        connector = GitConnector(str(repo_path))
        if connector.exists:
            if connector.is_protected_branch:
                target_ver = (
                    self.args.to_version or ""
                )  # target_ver is determined in _prepare_upgrade, but we need it here
                if not target_ver:
                    # Try to get target_ver if not provided, though usually it is mandatory or defaulted
                    # We can't easily call _prepare_upgrade yet as it has side effects (knowledge repo sync)
                    raise self.error(
                        f"Repository at {repo_path} is on a protected branch ({connector.branch!r}). "
                        "AI upgrades should be performed on a feature branch. "
                        "Please specify a target version with --to to allow automatic branch creation."
                    )

                xgram = self._get_xgram()
                branch_name = f"{target_ver}-upgrade"
                if xgram:
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
                    raise self.error(
                        f"Repository at {repo_path} is on a protected branch ({connector.branch!r}). "
                        "AI upgrades should be performed on a feature branch."
                    )

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

        # Prompt for cleanup of OLD upgrade databases before starting the new one
        self._cleanup_wizard(stage="pre-flight", exclude=[target_db])

        db_to_clone = (
            self._database.name
            if getattr(self, "_database", None) and self._database.platform.name != "dummy"
            else None
        )

        # Ensure target database exists on host before letting the AI work on it.
        # This gives persistence so that host-side tests work after the AI session.
        from odev.common.databases import LocalDatabase

        target_db_obj = LocalDatabase(target_db)
        if not target_db_obj.exists:
            if db_to_clone:
                logger.info(f"Cloning host database {db_to_clone!r} to {target_db!r} for upgrade...")
                self.clone_database(db_to_clone, target_db)
            else:
                logger.info(f"Creating empty host database {target_db!r} for upgrade...")
                target_db_obj.create()

        logger.info(
            f"Starting Project-wide AI Upgrade ({agent.cli} - {agent.model}): "
            f"from {from_ver} to {target_ver} (Target DB: {target_db})"
        )

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

        # --- Post-Upgrade Test & Fix Loop ---
        modules_to_test = ",".join([m["name"] for m in modules_info])
        session_id = self.args.resume or agent.get_latest_session_id()

        while self.console.confirm(
            f"Would you like to run the full test suite for analysis? (Modules: {modules_to_test})",
            default=True,
        ):
            logger.info(f"Running tests for module(s): {modules_to_test}...")
            # We remove --log-level=warn so the user sees progress (INFO logs),
            # but we'll filter it for the AI later.
            test_cmd = [
                "odev",
                "test",
                target_db,
                "-i",
                modules_to_test,
            ]

            # Run odev test via subprocess to stream and capture output for the AI
            logger.info("Streaming test output... (This may take a few minutes)")

            # We'll use these to build the AI context (filtered)
            filtered_output = []
            import re as _re

            # Pattern to match Start of Error/Warning or Traceback
            error_start_re = _re.compile(r"^(\d{4}-\d{2}-\d{2}\s)?\d{2}:\d{2}:\d{2},\d{3}\s(ERROR|WARNING)")
            traceback_start_re = _re.compile(r"^(Traceback \(most recent call last\):|AssertionError:|FAIL:)")
            indent_re = _re.compile(r"^[\s\t]+")

            p = subprocess.Popen(
                test_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True,
            )

            import threading

            def stream_pipe(pipe, pipe_name):
                is_collecting = False
                for line in pipe:
                    # 1. Real-time display for the user (raw)
                    self.console.print(line.rstrip("\r\n"), soft_wrap=True)

                    # 2. Filtered capture for the AI
                    # Strip ANSI colors to avoid confusing the LLM
                    clean_line = _re.sub(r"\x1b[^m]*m", "", line)

                    if error_start_re.search(clean_line) or traceback_start_re.search(clean_line):
                        is_collecting = True
                        filtered_output.append(clean_line)
                    elif is_collecting and indent_re.search(clean_line):
                        filtered_output.append(clean_line)
                    else:
                        is_collecting = False
                        if " ERROR " in clean_line or " WARNING " in clean_line or "Traceback" in clean_line:
                            filtered_output.append(clean_line)

            t1 = threading.Thread(target=stream_pipe, args=(p.stdout, "stdout"))
            t2 = threading.Thread(target=stream_pipe, args=(p.stderr, "stderr"))

            t1.start()
            t2.start()

            p.wait()
            t1.join()
            t2.join()

            returncode = p.returncode

            if returncode == 0:
                logger.info("Tests passed successfully!")
                break
            else:
                logger.warning("Tests failed. Preparing results for AI analysis...")
                test_failures = "".join(filtered_output)

                if not test_failures.strip():
                    logger.info("No explicit ERROR/WARNING caught by filter. Including full output summary instead.")
                    test_failures = "No tracebacks captured, but exit code was non-zero."

                # Prioritize bottom of the log if still too large
                if len(test_failures) > 30000:
                    test_failures = test_failures[:5000] + "\n...[TRUNCATED MID-LOG]...\n" + test_failures[-25000:]

                prompt_test = (
                    f"The full test suite execution failed with the following output:\n\n"
                    f"```\n{test_failures}\n```\n\n"
                    "Please analyze the failures and fix the code accordingly. "
                    "Focus first on any failures related to the upgraded modules. "
                    "When finished, perform a fast verification (installation) as usual."
                )

                logger.info(
                    f"Sending {len(test_failures)} characters of filtered failure data to AI agent ({agent.cli})..."
                )
                # Resume the AI session with the test results
                agent.run(
                    prompt_test,
                    sandbox_dirs,
                    extra_bind_dirs=extra_bind_dirs,
                    database=target_db,
                    version=target_ver,
                    resume=session_id or "latest",
                    path_mapping=path_mapping,
                    ephemeral_pg=True,
                )
                # Update session_id in case it changed (for some CLIs)
                session_id = agent.get_latest_session_id() or session_id

        # Offer to sync new knowledge findings to the knowledge repo as a PR
        if ki and ki.is_configured():
            if self.console.confirm(
                "Would you like to sync new knowledge findings to the knowledge index repo as a PR?",
                default=True,
            ):
                import re as _re

                branch_safe = _re.sub(
                    r"[^a-zA-Z0-9._-]",
                    "-",
                    f"odev/upgrade-knowledge-{from_ver}-{target_ver}",
                )
                pr_url = ki.commit_and_pr(
                    branch_name=branch_safe,
                    commit_message=f"feat(knowledge): add/update entries for {from_ver}→{target_ver} upgrade",
                    pr_title=f"[Knowledge] {from_ver} → {target_ver} upgrade findings",
                    pr_body=(
                        f"This PR was automatically created by `odev upgrade` after upgrading "
                        f"from Odoo **{from_ver}** to **{target_ver}**.\n\n"
                        "Please review the knowledge entries and merge when satisfied."
                    ),
                )
                if pr_url:
                    logger.info(f"Knowledge PR created: {pr_url}")
                else:
                    logger.info("No new knowledge changes to sync (nothing to commit).")

        # Ask the user if they want to run the tests after leaving the upgrade
        if self.console.confirm(
            "Upgrade session finished. Would you like to launch 'odev test --ai' to verify the upgrade?",
            default=False,
        ):
            modules = self.args.module_name or ",".join([m["name"] for m in self._get_sorted_modules([self.args.path])])
            verify_test_args = f"test --ai {target_db} -V {target_ver} -i {modules}"
            logger.info(f"Launching verification tests: odev {verify_test_args}")
            self.odev.run_command(*verify_test_args.split())

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
                # Run ruff check on the module directory
                # We use --select=E,W,F,I to catch common issues without being too pedantic
                # Run ruff check on the module directory
                # We use --select=E,W,F,I to catch common issues without being too pedantic
                # If we want to see how many lines would change, we'd need --diff
                diff_process = subprocess.run(
                    ["ruff", "check", str(repo_path), "--diff", "--exit-zero"],
                    capture_output=True,
                    text=True,
                )

                diff_lines = diff_process.stdout.count("\n")
                if diff_lines > 50:  # Threshold for "many" issues
                    logger.warning(
                        f"\n[bold color.yellow]WARNING:[/bold color.yellow] Module at {repo_path} has many linting issues ({diff_lines} lines would be changed by ruff).\n"
                        "Instructing the AI to run ruff will result in a very large and noisy git diff.\n"
                        "Consider running `pre-commit run --all-files` first, or use [bold]--no-ruff[/bold] to disable automatic linting."
                    )
                    if not self.args.yolo and not self.console.confirm(
                        "Do you want to continue with automatic ruff instructions anyway?",
                        default=True,
                    ):
                        self.args.no_ruff = True
                        logger.info("Automatic ruff instructions disabled for this session.")

            except Exception as e:
                logger.debug(f"Ruff cleanliness check failed: {e}")

    def clone_database(self, source: str, target: str):
        """Clone a local database using a template."""
        with self.psql() as psql:
            psql.create_database(target, template=source)
