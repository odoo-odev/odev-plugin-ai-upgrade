"""Upgrade Odoo modules using AI."""

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
from odev.common.version import OdooVersion as _OV

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

    submodules = args.Flag(
        aliases=["--submodules"],
        description="Look for modules in git submodules (default: False).",
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
        try:
            self._run_upgrade()
        except KeyboardInterrupt:
            logger.warning("\nUpgrade interrupted by user.")
        finally:
            self._cleanup_wizard(stage="post-flight")

    def _prepare_upgrade(
        self,
    ) -> (tuple[str, list[str], list[str], str, str, str, "KnowledgeIndex | None", list[dict],] | None):
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

        sandbox_dirs = [
            str(Path(self.args.path).resolve()),
        ]
        extra_bind_dirs = []

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
            extra_bind_dirs.append(f"{upgrade_path.resolve()}:/upgrade")

        upgrade_instructions = (
            f"\n- **Migration Scripts (Upgrade Repository)**: You have access to the official Odoo Enterprise migration scripts at `{upgrade_path.as_posix()}`. "
            "This repository contains the logic used by Odoo's upgrade team. "
            "You MUST search this directory to understand how Odoo handles API changes, field renames, and model migrations for the modules you are upgrading. "
            "Use `grep` or `git grep` within this directory to find mentions of your module or specific fields/methods that have changed."
        )

        # --- Resolve source and target paths for prompt context ---------------------
        from_odoobin = OdoobinProcess(self._database).with_version(_OV(from_ver))
        try:
            # Only resolve path if worktree already exists to avoid auto-triggering creation
            if (self.odev.worktrees_path / from_ver).exists():
                from_odoo_path_host = from_odoobin.odoo_path.resolve()
                from_odoo_path = "/source/from"
                sandbox_dirs.append(f"{from_odoo_path_host.parent}:/source/from")
            else:
                from_odoo_path = f"Virtual (Ref: {from_ver} in Target Odoo)"
        except Exception:
            from_odoo_path = "Unknown"

        target_odoobin = OdoobinProcess(self._database).with_version(_OV(target_ver))
        try:
            target_odoo_path_host = target_odoobin.odoo_path.resolve()
            target_odoo_path = "/source/target"
            sandbox_dirs.append(f"{target_odoo_path_host.parent}:/source/target")
        except Exception:
            target_odoo_path_host = (self.odev.worktrees_path / target_ver / "odoo").resolve()
            target_odoo_path = "/source/target"
            sandbox_dirs.append(f"{target_odoo_path_host.parent}:/source/target")

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
                    ki.migrate_to_consolidated()

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

        # Re-resolve paths after preparation
        try:
            from_odoo_path = from_odoobin.odoo_path.as_posix()
        except Exception:
            pass

        try:
            target_odoo_path = target_odoobin.odoo_path.as_posix()
        except Exception:
            pass

        # Prepare module context for AI
        modules_list_str = "\n".join(
            [f"- {m['name']} (Path: {m['path']}, Depends: {', '.join(m['depends'])})" for m in modules_info]
        )

        repo_name = Path(self.args.path).resolve().name
        is_ps_custom_external = repo_name.startswith("ps") and repo_name.endswith("-custom")

        free_port = self._get_free_port()

        if is_ps_custom_external:
            fast_verify = f"- **Verification (Fast)**: Verify the module using: `odev deploy <module_name>`. (Assume one instance is already running with `odev run`). Then, verify the presence of new fields or view rendering via a minimal test or manual check. You MUST use `--http-port {free_port}` if you launch Odoo."
        else:
            fast_verify = f"- **Verification (Fast)**: Verify the module installs cleanly (including demo data) using: `odev run -f -V {target_ver} {target_db} -i <module_name> --http-port {free_port} --log-level=warn`. Then, verify the presence of new fields, view rendering, or basic logic via a minimal test or tour."

        # Build the full prompt: existing knowledge context + upgrade instructions
        knowledge_prefix = ""
        if knowledge_context:
            knowledge_prefix = knowledge_context + "\n\n---\n\n"

        prompt = (
            knowledge_prefix
            + f"""You are an expert Odoo Upgrade Lead. Your task is to upgrade multiple Odoo modules from version {from_ver} to {target_ver}.

### Context:
- **Source Odoo**: Version {from_ver} at `{from_odoo_path}` (Git repo).
- **Target Odoo**: Version {target_ver} at `{target_odoo_path}` (Git repo).
- **Project Root**: `{self.args.path.resolve().as_posix()}`
- **Target Database**: `{target_db}` (Use this for all installations and tests).
- **Upgrade Knowledge Base**: `{knowledge_local_path or "<knowledge_repo>"}` (Read/Write access). Use this to find and record upgrade-specific Odoo knowledge.
- **Migration Scripts (Upgrade Repository)**: You have access to official scripts at `/upgrade`.
- **Modules to Upgrade**:
{modules_list_str}

### Project Status:
Check `UPGRADE.md` and `TASKS.md` in the project root to understand the current progress and pending tasks.
{"(Files found)" if report_exists else "(Fresh start: No existing reports found)"}

### Standard Odoo Migration Rules:
For Odoo >= 18.0, you SHOULD first try to use `odev upgrade-code --from {from_ver} --to {target_ver} {target_db} --glob "your_module/**/*"`.
**IMPORTANT**: You MUST wrap the glob pattern in double quotes to prevent your current shell from expanding it before it reaches the `odev` command.
This tool can automate many common renames (e.g., `<tree>` to `<list>`).
You MUST NOT pass a directory path as the first argument; you MUST use the database name `{target_db}` to ensure `odev` correctly resolves your addons environment. You may append the project directory path as a secondary argument if needed.
You MUST also search for version-specific migration scripts in `odoo/odoo/upgrade_code` within the Target Odoo repository to understand how Odoo handles specific API changes.
For information on available migration helpers (`util.rename_field`, `custom_util.custom_rename_field`, etc.), consult the following Skills:\n- **Standard Odoo Helpers**: `/home/odev/skills/odoo_upgrade_utils`
- **PS Custom Helpers**: `/home/odev/skills/custom_util`
- **ODEV CLI Usage**: `/home/odev/skills/odev` (Mandatory rules for `create`, `run`, `test`, `venv`, 'upgrade-code')

### Your Process:
1. **Analyze & Plan**: First, analyze the modules and their dependencies. **Describe your plan in the chat**, and then create (or update) a `TASKS.md` file in the root directory with a detailed checklist of your planned steps.
   - **Verify Against Standard Features**: Before adapting any custom code, check if the existing custom functionality has been implemented as standard features in Odoo {target_ver}. Consult the Odoo documentation and release notes. If a feature can be replaced by a standard one, you can replace the custom code with the standard one.
2. **Execution & Fast Verification**: For each module (in dependency order):
   - **Update `TASKS.md`**: Mark items as `[/]` (in progress) or `[x]` (completed).

   - **Proactive Adaptation & Evidence-Based Action**:
     While you should prioritize finding exact commit hashes or file changes in the Target Odoo repository, you are authorized to act based on explicit error logs, tracebacks, or logical comparisons between the old and new Odoo Core file structures. If a specific commit hash for 100% of the lines isn't found, use your best judgment based on the context of the Target Odoo source code.

   - **Research Workflow**:
     In the Target Odoo repository, you MUST research how logic changed between versions. Use `git log`, `git blame`, `git grep`, and `git show` to trace the history and identify the official "Odoo way" of implementing features in {target_ver}.

   - **Empowered Expert Execution**:
     You are an EXPERT Odoo Lead, not just a researcher. While evidence is preferred, you ARE authorized to act based on your expert understanding of Odoo {target_ver} standards and behavioral consistency.
     If the Target Odoo source code shows a new pattern (e.g., JS concat, Kanban Card, new hooks), apply it proactively to our code even if you don't have a specific commit hash for that exact line.
     Stating "Aligned with Odoo {target_ver} core logic and best practices" is sufficient justification for these technical modernizations.

   - **Modernization & Quality Audit**:
     An upgrade is the best opportunity to reduce technical debt. You MUST prioritize REPLACING old custom patterns with the new, simpler standards seen in the Target Odoo {target_ver} repository.
     If the Target Odoo Core version of a component is less complex or logically restructured, you SHOULD strive to REBASE our custom logic on that new standard instead of patching legacy code.

   - **Upgrade the code**: Upgrade models, views, js, css, and data files.{upgrade_instructions}

   - **Data Migrations**: For structural changes (field renames, model moves), you MUST create migration scripts in `<module>/migrations/{target_ver}/`.
   - **Migration Helpers**: Use `from odoo.upgrade import util`. Refer to the Skill in `/home/odev/skills/odoo_upgrade_utils` for documentation on `rename_field`, `remove_field`, etc.

   {fast_verify}

   - **Log Verification**: When running execution or verification commands, you MUST inspect the full log output. A successful exit status does not guarantee that the Odoo registry loaded correctly. Explicitly check for `Registry` load failures, `TypeError`, or `AttributeError` which often indicate model-level incompatibilities.
   - **Recursive Audit**: You MUST perform a recursive audit of every file in the module, including `static/` JS files, templates (XML), and tours. Compare their implementation logic with equivalent or similar files in Odoo Core to ensure no functional breakage in "invisible" technical layers.
   - **Python API Compatibility & "Broken Hooks"**: For every module, systematically audit all overridden methods (e.g., `read_group`, `search`, `name_get`). **Prioritize core models** like `stock.move`, `sale.order`, and `account.move`. Even if the signature appears unchanged, the internal calling logic in Odoo Core may have shifted, requiring adjustments in your inherited methods to ensure they are still triggered or behave correctly.
   - **Test Intent Preservation**: If the module has existing tests, keep them. Adapt their syntax so they pass in the new version, but **DO NOT change the core business flow** they are designed to test.
   - **DO NOT** run `odev test` at this stage. Focus on making sure the module installs cleanly and passes fast manual verification.

3. **Detailed Reporting**: Update the `UPGRADE.md` file after each module.
   - **Reporting Structure**: For each module, you must provide a detailed list of changes:
     - **Odoo API/Structural Upgrades**: You MUST include the exact file path in the Odoo target repository and the Git commit hash (or core file reference) that dictated the change.
   - **Source Requirement**: Every single Odoo-specific change MUST have a specific "Source". If no commit is found, state: "Verified against Odoo core version {target_ver} file <path/to/core/file>".

- **Version Upgrades**: The upgraded module version MUST follow the format: `MAJOR_ODOO.MINOR_ODOO.MAJOR_MODULE.0.0` (e.g., `{target_ver}.1.0.0`).

- If `TASKS.md` or `UPGRADE.md` already exist, read them first to resume work. Update `TASKS.md` frequently.
- **Pragmatic & Modern Upgrade**: Your goal is to make the module perfectly compatible and aligned with Odoo {target_ver} standards. While you should avoid purely cosmetic refactors, you MUST implement technical modernizations required by the new version. This includes migrating to `kanban.card`, adding necessary attributes like `column_invisible`, and updating JS hooks. If the Target Odoo version shows a "Standard" way of implementing a feature, you MUST follow it.
- **Maintain Feature Coverage**: If a feature or piece of code is broken during the upgrade, **DO NOT** simply remove it. You must maintain the same feature coverage by either replacing it with a new implementation compatible with {target_ver} or by implementing a workaround.
- **Delegate Tasks**: If your CLI supports sub-agents or delegation tools (like `generalist`), use them to parallelize or offload heavy analysis, repetitive editing, or complex research tasks.
- **Status Prefix**: When providing updates, thinking, or describing your plan in the chat, **ALWAYS** prefix your message with the name of the module you are currently working on in brackets (e.g., `[module_name] Your message here`).

### 📓 Knowledge Write-Back (MANDATORY — after each module)

This knowledge base is permanent and reused across all future upgrades. As you upgrade each module, you are responsible for recording what you discover.

**After completing the upgrade of each module:**
1. For each of its standard Odoo dependencies that changed, open `{knowledge_local_path or "<knowledge_repo>/knowledge"}/<dep_module>.md`.
2. Find the `## {from_ver} → {target_ver}` section (or create it if missing, using the format: `## {from_ver} → {target_ver}`).
3. Append your findings under the relevant sub-headers (`### Field Changes`, `### Method / API Changes`, `### Framework / View Changes`, `### Migration Script Notes`, `### Notes / Tips`).
4. Keep entries **compact**: bullet points only, no prose. Include commit hashes or file paths as evidence where possible.
5. Write `_No changes._` in sub-headers that are genuinely empty — **do NOT leave them blank**.

**At the very end of the session**, after all modules are complete and verified only if there is local changes:
```bash
git -C {knowledge_local_path or "<knowledge_repo>"} add -A
git -C {knowledge_local_path or "<knowledge_repo>"} commit -m "knowledge: {from_ver}→{target_ver} upgrade findings"
```
"""
        )
        if self.args.comment:
            prompt += f"\n### Additional User Instructions:\n- {self.args.comment}\n"

        # Add binary skill paths
        skills_path = Path(__file__).parent.parent / "skills"
        if skills_path.exists():
            extra_bind_dirs.append(f"{skills_path.resolve()}:/home/odev/skills")

        extra_bind_dirs.append(f"{self.odev.worktrees_path}:/worktrees_host")

        # Allow the AI to write into the knowledge repo when populating stubs
        if knowledge_local_path:
            sandbox_dirs.append(knowledge_local_path)

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

    @contextmanager
    def _ephemeral_postgresql(self, db_to_clone: str | None = None):
        """Context manager to start and stop an ephemeral PostgreSQL cluster."""
        import shutil

        pg_dir = Path(tempfile.mkdtemp(prefix="odev-pg-"))
        pg_socket = Path(tempfile.mkdtemp(prefix="odev-pg-socket-"))
        pg_log = pg_dir / "postgresql.log"

        try:
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
        connector = GitConnector(str(repo_path))
        if connector.exists:
            if connector.is_protected_branch:
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
        ) = prepared
        agent = self.get_ai_agent()

        db_to_clone = (
            self._database.name
            if getattr(self, "_database", None) and self._database.platform.name != "dummy"
            else None
        )

        with self._ephemeral_postgresql(db_to_clone=db_to_clone) as pg_socket:
            logger.info(
                f"Starting Project-wide AI Upgrade ({agent.cli} - {agent.model}): "
                f"from {from_ver} to {target_ver} (Target DB: {target_db})"
            )

            agent.run(
                prompt,
                sandbox_dirs,
                extra_bind_dirs=extra_bind_dirs,
                database=target_db,
                version=target_ver,
                resume=self.args.resume,
                pg_socket_dir=pg_socket,
            )

            # --- Post-Upgrade Test & Fix Loop ---
            modules_to_test = ",".join([m["name"] for m in modules_info])
            session_id = self.args.resume or agent.get_latest_session_id()

            import os as _os

            # Environment for host-side odev test execution
            test_env = _os.environ.copy()
            if pg_socket:
                test_env["PGHOST"] = str(pg_socket)

            while self.console.confirm(
                f"\nWould you like to run the full test suite for analysis? (Modules: {modules_to_test})",
                default=True,
            ):
                logger.info(f"Running tests for module(s): {modules_to_test}...")
                test_cmd = [
                    "odev",
                    "test",
                    "--log-level=warn",
                    target_db,
                    "-i",
                    modules_to_test,
                ]

                # Run odev test via subprocess to capture output for the AI
                res = subprocess.run(
                    test_cmd,
                    env=test_env,
                    capture_output=True,
                    text=True,
                )

                # Display output to user
                if res.stdout:
                    self.print(res.stdout)
                if res.stderr:
                    self.print(res.stderr)

                if res.returncode == 0:
                    logger.info("Tests passed successfully!")
                    break
                else:
                    logger.warning("Tests failed. Preparing results for AI analysis.")
                    test_failures = res.stdout + "\n" + res.stderr
                    # Truncate if too long (optional, but keep it relevant)
                    if len(test_failures) > 30000:
                        test_failures = test_failures[:15000] + "\n...[TRUNCATED]...\n" + test_failures[-15000:]

                    prompt_test = (
                        f"The full test suite execution failed with the following output:\n\n"
                        f"```\n{test_failures}\n```\n\n"
                        "Please analyze the failures and fix the code accordingly. "
                        "When finished, perform a fast verification (installation) as usual."
                    )

                    # Resume the AI session with the test results
                    agent.run(
                        prompt_test,
                        sandbox_dirs,
                        extra_bind_dirs=extra_bind_dirs,
                        database=target_db,
                        version=target_ver,
                        resume=session_id or "latest",
                        pg_socket_dir=pg_socket,
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
                modules = self.args.module_name or ",".join(
                    [m["name"] for m in self._get_sorted_modules([self.args.path])]
                )
                verify_test_args = f"test --ai {target_db} -V {target_ver} -i {modules}"
                logger.info(f"Launching verification tests: odev {verify_test_args}")
                self.odev.run_command(*verify_test_args.split())

    def _get_upgrade_databases(self) -> list[str]:
        """Return a list of local databases that look like upgrade databases."""
        return [db for db in self.list_databases() if db.endswith("_upgrade")]

    def _cleanup_wizard(self, stage: str = "post-flight") -> None:
        """Prompt the user to clean up leftover upgrade databases."""
        upgrade_dbs = self._get_upgrade_databases()
        if not upgrade_dbs:
            return

        logger.info(f"\n[{stage}] Found {len(upgrade_dbs)} potential upgrade database(s).")
        to_delete = self.console.checkbox(
            "Select databases to delete:",
            choices=[(db, db) for db in upgrade_dbs],
            defaults=upgrade_dbs if stage == "post-flight" else [],
        )

        if not to_delete:
            return

        from odev.common.databases import LocalDatabase

        for db_name in to_delete:
            db = LocalDatabase(db_name)
            if db.exists:
                logger.info(f"Dropping database {db_name!r}...")
                db.drop()
