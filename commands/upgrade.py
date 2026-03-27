"""Upgrade Odoo modules using AI."""

import socket
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

import networkx as nx

from odev.common import args, progress
from odev.common.commands import DatabaseCommand
from odev.common.connectors import GitConnector
from odev.common.logging import logging
from odev.common.mixins.databases.list import ListLocalDatabasesMixin
from odev.common.odoobin import ODOO_UPGRADE_REPOSITORY, OdoobinProcess

from odev.plugins.odev_plugin_ai.common.mixins import AICommandMixin


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
        aliases=["--to-version"],
        description="The target Odoo version. Defaults to the environment's target version.",
    )

    from_version = args.String(
        aliases=["--from-version"],
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

    resume = args.String(
        aliases=["--resume"],
        description="Resume a previous AI session by ID or 'latest'.",
        default=None,
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

    def run(self) -> None:
        """Execute the upgrade command."""
        try:
            self._run_upgrade()
        except KeyboardInterrupt:
            logger.warning("\nUpgrade interrupted by user.")
        finally:
            self._cleanup_wizard(stage="post-flight")

    def _prepare_upgrade(self) -> tuple[str, list[str], str, str, str] | None:
        """Prepare the upgrade environment and generate the AI prompt."""
        from_ver = self.args.from_version or self._database.version
        if not from_ver:
            # Try to detect version from the path if no database version is available
            from_ver = OdoobinProcess.version_from_manifest(self.args.path) or OdoobinProcess.version_from_addons(
                self.args.path
            )
            if from_ver:
                logger.info(f"Detected source version {from_ver} from path {self.args.path}")
            else:
                logger.error(
                    f"Could not determine source version from database '{self._database.name}' or path '{self.args.path}'. "
                    "Ensure the database exists or the path contains valid Odoo modules."
                )
                return None

        from_ver = str(from_ver)

        target_ver = self.args.to_version or ""
        if not target_ver:
            logger.error("Could not determine target version. Please specify a --to-version.")
            return None

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
        upgrade_repo_added = False
        upgrade_path = self.config.paths.upgrade
        upgrade_instructions = ""

        upgrade_connector = GitConnector(ODOO_UPGRADE_REPOSITORY, path=upgrade_path)
        with progress.spinner(f"Managing {ODOO_UPGRADE_REPOSITORY!r} repository"):
            if not upgrade_connector.exists:
                upgrade_connector.clone()
            else:
                upgrade_connector.pull(force=True)
            upgrade_repo_added = True

        upgrade_instructions = (
            f"\n- **Migration Scripts (Upgrade Repository)**: You have access to the official Odoo Enterprise migration scripts at `{upgrade_path.as_posix()}`. "
            "This repository contains the logic used by Odoo's upgrade team. "
            "You MUST search this directory to understand how Odoo handles API changes, field renames, and model migrations for the modules you are upgrading. "
            "Use `grep` or `git grep` within this directory to find mentions of your module or specific fields/methods that have changed."
        )

        # Load existing report or create a new one
        report_path = self.args.path / "UPGRADE.md"
        report_exists = report_path.exists()
        if report_exists:
            logger.info(f"Existing upgrade report found at {report_path}")

        from odev.common.version import OdooVersion

        # Resolve source and target paths
        from_odoobin = OdoobinProcess(self._database)
        try:
            from_odoo_path = from_odoobin.odoo_path.as_posix()
        except Exception:
            from_odoo_path = "Unknown (not yet created/cloned)"

        target_odoobin = OdoobinProcess(self._database).with_version(OdooVersion(target_ver))
        try:
            target_odoo_path = target_odoobin.odoo_path.as_posix()
        except Exception:
            target_odoo_path = "Unknown (not yet created/cloned)"

        # Prepare environments
        for version in sorted({from_ver, target_ver}):
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
            fast_verify = f"- **Verification (Fast)**: Verify the module installs cleanly (including demo data) using: `odev run --http-port {free_port} --log-level=warn {target_db} -i <module_name> without-demo=False`. Then, verify the presence of new fields, view rendering, or basic logic via a minimal test or tour."

        prompt = f"""You are an expert Odoo Upgrade Lead. Your task is to upgrade multiple Odoo modules from version {from_ver} to {target_ver}.

### Context:
- **Source Odoo**: Version {from_ver} at `{from_odoo_path}` (Git repo).
- **Target Odoo**: Version {target_ver} at `{target_odoo_path}` (Git repo).
- **Project Root**: `{self.args.path.resolve().as_posix()}`
- **Target Database**: `{target_db}` (Use this for all installations and tests).
- **Modules to Upgrade**:
{modules_list_str}

### Project Status:
Check `UPGRADE.md` and `TASKS.md` in the project root to understand the current progress and pending tasks.
{"(Files found)" if report_exists else "(Fresh start: No existing reports found)"}

### Your Process:
1. **Analyze & Plan**: First, analyze the modules and their dependencies. **Describe your plan in the chat**, and then create (or update) a `TASKS.md` file in the root directory with a detailed checklist of your planned steps.
2. **Execution & Fast Verification**: For each module (in dependency order):
   - **Update `TASKS.md`**: Mark items as `[/]` (in progress) or `[x]` (completed).

   - **Proactive Adaptation & Evidence-Based Action**:
     While you should prioritize finding exact commit hashes or file changes in the Target Odoo repository, you are authorized to act based on explicit error logs, tracebacks, or logical comparisons between the old and new Odoo Core file structures. If a specific commit hash for 100% of the lines isn't found, use your best judgment based on the context of the Target Odoo source code.

   - **Safe Git Research Workflow**:
     Before modifying Odoo core logic, research how it changed between {from_ver} and {target_ver}:
     - **Trace Changes**: `git log {from_ver}..{target_ver} -- <file>` (List all commits touching a file between versions).
     - **Identify Change**: `git blame -L <start>,<end> <file>` (Find which commit last modified specific lines).
     - **Find Removed Code**: `git log -p -G "regex" -- <file>` (Search the history for the addition or removal of a specific code pattern).
     - **Pickaxe Search**: `git log -S "term" --oneline -n 5` (Find commits where "term" appeared or disappeared).
     - **Inspect Content**: `git show <commit_hash>` (See the full diff and message of a specific commit).
     - **API usage**: `git grep -C 3 "term" odoo/addons/base`
     Always run these inside the Target Odoo repository (`{target_odoobin.odoo_path.as_posix()}`).

   - **Empowered Expert Execution**:
     You are an EXPERT Odoo Lead, not just a researcher. While evidence is preferred, you ARE authorized to act based on your expert understanding of Odoo {target_ver} standards and behavioral consistency.
     If the Target Odoo source code shows a new pattern (e.g., JS concat, Kanban Card, new hooks), apply it proactively to our code even if you don't have a specific commit hash for that exact line.
     Stating "Aligned with Odoo {target_ver} core logic and best practices" is sufficient justification for these technical modernizations.

   - **Modernization & Quality Audit**:
     An upgrade is the best opportunity to reduce technical debt. You MUST prioritize REPLACING old custom patterns with the new, simpler standards seen in the Target Odoo {target_ver} repository.
     If the Target Odoo Core version of a component is less complex or logically restructured, you SHOULD strive to REBASE our custom logic on that new standard instead of patching legacy code.

   - **Upgrade the code**: Upgrade models, views, js, css, and data files.{upgrade_instructions}

   - **Data Migrations**: If your code changes involve structural modifications (e.g., field renames, moving fields to another model, renaming models, or model merges), **you MUST create migration scripts** to prevent data loss.
     - **MANDATORY**: Moving fields/data between models (like `stock.valuation.layer` to `stock.move`) is a destructive operation without a script.

   - **Use Upgrade Utils**: When writing migration scripts, use Odoo's Upgrade Utils (`util.py`). Refer to [Odoo Upgrade Utils](https://www.odoo.com/documentation/18.0/th/developer/reference/upgrades/upgrade_utils.html).

   - **Migration Files**: Place scripts in `<module>/migrations/{target_ver}/` as `pre-migration.py`, `post-migration.py`, or `end-migration.py`.

   {fast_verify}

   - **Log Verification**: When running execution or verification commands, you MUST inspect the full log output. A successful exit status does not guarantee that the Odoo registry loaded correctly. Explicitly check for `Registry` load failures, `TypeError`, or `AttributeError` which often indicate model-level incompatibilities.
   - **Recursive Audit**: You MUST perform a recursive audit of every file in the module, including `static/` JS files, templates (XML), and tours. Compare their implementation logic with equivalent or similar files in Odoo Core to ensure no functional breakage in "invisible" technical layers.
   - **Python API Compatibility & "Broken Hooks"**: For every module, systematically audit all overridden methods (e.g., `read_group`, `search`, `name_get`). **Prioritize core models** like `stock.move`, `sale.order`, and `account.move`. Even if the signature appears unchanged, the internal calling logic in Odoo Core may have shifted, requiring adjustments in your inherited methods to ensure they are still triggered or behave correctly.
   - **DO NOT** run `odev test` at this stage as it is too slow for individual iterations.

3. **Global Verification (Final Step)**: Once ALL modules in your plan are upgraded and install cleanly, run the full test suite for the entire project: `odev test --log-level=warn {target_db} -i <comma_separated_modules>`.
   - **Tour Tests**: To verify that the UI works correctly in the browser, you SHOULD add or run Odoo tours. If the module is complex, create a new tour in `static/tests/tours/` and a corresponding Python test in `tests/` to trigger it. Run tours using: `odev test --log-level=warn {target_db} -t /<module_name>`.

4. **Detailed Reporting**: Update the `UPGRADE.md` file after each module and after the final verification.
   - **Reporting Structure**: For each module, you must provide a detailed list of changes:
     - **Odoo API/Structural Upgrades**: You MUST include the exact file path in the Odoo target repository and the Git commit hash (or core file reference) that dictated the change.
   - **Source Requirement**: Every single Odoo-specific change MUST have a specific "Source". If no commit is found, state: "Verified against Odoo core version {target_ver} file <path/to/core/file>".

- **Version Upgrades**: The upgraded module version MUST follow the format: `MAJOR_ODOO.MINOR_ODOO.MAJOR_MODULE.0.0` (e.g., `{target_ver}.1.0.0`).

### Mandatory Rules:
- **MANDATORY**: When using `odev create`, you MUST ALWAYS specify the Odoo version with `-V <version>`.
  - **Correct Example**: `odev create -V {target_ver} {target_db}`
- If `TASKS.md` or `UPGRADE.md` already exist, read them first to resume work. Update `TASKS.md` frequently.
- **Pragmatic & Modern Upgrade**: Your goal is to make the module perfectly compatible and aligned with Odoo {target_ver} standards. While you should avoid purely cosmetic refactors, you MUST implement technical modernizations required by the new version. This includes migrating to `kanban.card`, adding necessary attributes like `column_invisible`, and updating JS hooks. If the Target Odoo version shows a "Standard" way of implementing a feature, you MUST follow it.
- **Maintain Feature Coverage**: If a feature or piece of code is broken during the upgrade, **DO NOT** simply remove it. You must maintain the same feature coverage by either replacing it with a new implementation compatible with {target_ver} or by implementing a workaround.
- **Delegate Heavy Tasks**: If your CLI supports sub-agents or delegation tools (like `generalist`), use them to parallelize or offload heavy analysis, repetitive editing, or complex research tasks.
- **Package Installation**: If you encounter a `ModuleNotFoundError` or need to install a python package, **ALWAYS** use: `odev venv {target_db} -c "pip install <package>"` to ensure it is installed in the correct virtual environment.
- **Status Prefix**: When providing updates, thinking, or describing your plan in the chat, **ALWAYS** prefix your message with the name of the module you are currently working on in brackets (e.g., `[module_name] Your message here`).
"""
        if self.args.comment:
            prompt += f"\n### Additional User Instructions:\n- {self.args.comment}\n"

        sandbox_dirs = [str(Path(self.args.path).resolve())]
        if from_odoo_path and "Unknown" not in from_odoo_path:
            sandbox_dirs.append(from_odoo_path)
        if target_odoo_path and "Unknown" not in target_odoo_path:
            sandbox_dirs.append(target_odoo_path)
        if upgrade_repo_added:
            sandbox_dirs.append(str(Path(upgrade_path).resolve()))

        return prompt, sandbox_dirs, from_ver, target_ver, target_db

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
                        "-D", str(pg_dir),
                        "-l", str(pg_log),
                        "-o", f"-c listen_addresses='' -k {pg_socket}",
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
                    res = subprocess.run(
                        ["pg_isready", "-h", str(pg_socket)],
                        capture_output=True
                    )
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
                logger.error(f"Failed to start ephemeral PostgreSQL cluster: {e.stderr or e.stdout}\nLog:\n{log_content}")
                raise

            # Get existing databases to clone
            res = subprocess.run(["psql", "-ltq"], capture_output=True, text=True)
            existing_dbs = [line.split("|")[0].strip() for line in res.stdout.splitlines() if line.strip()]

            # Clone 'odev' if it exists
            if "odev" in existing_dbs:
                logger.info("Cloning 'odev' database into ephemeral cluster (Sandbox isolation)...")
                subprocess.run(["createdb", "-h", str(pg_socket), "odev"], check=True)
                subprocess.run(
                    f"pg_dump odev | psql -h {pg_socket} -d odev",
                    shell=True,
                    check=True,
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

        prompt, sandbox_dirs, from_ver, target_ver, target_db = prepared
        agent = self.get_ai_agent()

        db_to_clone = (
            self._database.name
            if getattr(self, "_database", None) and self._database.platform.name != "dummy"
            else None
        )

        with self._ephemeral_postgresql(db_to_clone=db_to_clone) as pg_socket:
            logger.info(
                f"Starting Project-wide AI Upgrade ({self.args.cli or self.config.ai.favorite_cli}): "
                f"from {from_ver} to {target_ver} (Target DB: {target_db})"
            )

            agent.run(
                prompt,
                sandbox_dirs,
                database=target_db,
                version=target_ver,
                resume=self.args.resume,
                pg_socket_dir=pg_socket,
            )

            # Ask the user if they want to run the tests after leaving the upgrade
            if self.console.confirm(
                "Upgrade session finished. Would you like to launch 'odev test --ai' to verify the upgrade?",
                default=False,
            ):
                modules = self.args.module_name or ",".join(
                    [m["name"] for m in self._get_sorted_modules([self.args.path])]
                )
                test_cmd = f"test --ai {target_db} -V {target_ver} -i {modules}"
                logger.info(f"Launching verification tests: odev {test_cmd}")
                self.odev.run_command(*test_cmd.split())

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
