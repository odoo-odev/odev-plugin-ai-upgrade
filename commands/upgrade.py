"""Upgrade Odoo modules using AI."""

from pathlib import Path

import networkx as nx

from odev.common import args
from odev.common.commands import DatabaseCommand
from odev.common.logging import logging
from odev.common.mixins.databases.list import ListLocalDatabasesMixin
from odev.common.odoobin import OdoobinProcess

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

    target_version = args.String(
        aliases=["--target-version"],
        description="The target Odoo version. Defaults to the environment's target version.",
    )

    no_verify = args.Flag(
        aliases=["--no-verify"],
        description="Disable automatic file verification (lxml/ruff) after edits.",
        default=False,
    )

    comment = args.String(
        aliases=["-c", "--comment"],
        description="Add a custom comment/instruction to the AI prompt.",
        default="",
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
        from_ver = self._database.version
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

        target_ver = self.args.target_version or ""
        if not target_ver:
            logger.error("Could not determine target version. Please specify a --target-version.")
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

        modules_info = self._get_sorted_modules(search_paths)

        if not modules_info:
            logger.error(f"No modules found in search paths: {[str(p) for p in search_paths]}")
            return None

        # Load existing report or create a new one
        report_path = self.args.path / "UPGRADE.md"
        report_content = ""
        if report_path.exists():
            report_content = report_path.read_text()
            logger.info(f"Loaded existing upgrade report from {report_path}")

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

        # Prepare environment
        logger.info(f"Preparing environment for Odoo {target_ver}...")
        self.odev.run_command("pull", "-V", target_ver)
        self.odev.run_command("worktree", "-C", target_ver, "-V", target_ver)

        # Prepare module context for AI
        modules_list_str = "\n".join(
            [f"- {m['name']} (Path: {m['path']}, Depends: {', '.join(m['depends'])})" for m in modules_info]
        )

        verify_instructions = ""
        if not self.args.no_verify:
            verify_instructions = "\n- **File Verification**: After editing any Python or XML file, you MUST verify it using `ruff check <file>` (for Python) or `lxml`/`xmllint` (for XML) to ensure no syntax errors."

        repo_name = self.args.path.resolve().name
        is_ps_custom_external = repo_name.startswith("ps") and repo_name.endswith("-custom")

        if is_ps_custom_external:
            fast_verify = "- **Verification (Fast)**: Verify the module using: `odev deploy <module_name>`. (Assume one instance is already running with `odev run`)."
        else:
            fast_verify = f"- **Verification (Fast)**: Verify the module installs cleanly using: `odev run --no-pretty --log-level=warn {target_db} -i <module_name> --stop-after-init`."

        prompt = f"""You are an expert Odoo Upgrade Lead. Your task is to upgrade multiple Odoo modules from version {from_ver} to {target_ver}.

### Context:
- **Source Odoo**: Version {from_ver} at `{from_odoo_path}` (Git repo).
- **Target Odoo**: Version {target_ver} at `{target_odoo_path}` (Git repo).
- **Target Database**: `{target_db}` (Use this for all installations and tests).
- **Modules to Upgrade**:
{modules_list_str}

### Existing Project Status (UPGRADE.md):
{report_content or "No report found. This is a fresh start."}

### Your Process:
1. **Analyze & Plan**: First, analyze the modules and their dependencies. **Describe your plan in the chat**, and then create (or update) a `TASKS.md` file in the root directory with a detailed checklist of your planned steps.
2. **Execution & Fast Verification**: For each module (in dependency order):
   - **Update `TASKS.md`**: Mark items as `[/]` (in progress) or `[x]` (completed).
   - **Upgrade the code**: Upgrade models, views, and data files.
     - **Tip**: You can use `git log`, `git diff`, or `git blame` in the Target or Source Odoo repository to understand API changes and why core code was modified.
   - **Data Migrations**: If your code changes involve structural modifications (e.g., field renames, moving fields to another model, renaming models, or model merges), **you MUST create migration scripts** to prevent data loss.
     - **MANDATORY**: Moving fields/data between models (like `stock.valuation.layer` to `stock.move`) is a destructive operation without a script.
   - **Use Upgrade Utils**: When writing migration scripts, use Odoo's Upgrade Utils (`util.py`). Refer to [Odoo Upgrade Utils](https://www.odoo.com/documentation/18.0/th/developer/reference/upgrades/upgrade_utils.html).
   - **Migration Files**: Place scripts in `<module>/migrations/{target_ver}/` as `pre-migration.py`, `post-migration.py`, or `end-migration.py`.
   {fast_verify}
   - **DO NOT** run `odev test` at this stage as it is too slow for individual iterations.
3. **Global Verification (Final Step)**: Once ALL modules in your plan are upgraded and install cleanly, run the full test suite for the entire project: `odev test --no-pretty --log-level=warn {target_db} -i <comma_separated_modules>`.
4. **Report Progress**: Update the `UPGRADE.md` file after each module and after the final verification.
   - **Structure**: Format the report nicely using well-organized sections, sub-lists, and markdown features for better readability.
   - **Sources**: For every single change, you MUST explicitly include the upstream source of the API/behavior change (e.g., the exact Odoo Community or Enterprise commit hash/reference that forced this adaptation).
   - **IMPORTANT**: In `UPGRADE.md`, document both the local file modified and the *reason* for the change (e.g., "Updated method signature in `models/my_model.py` because commit `abc1234` in `odoo/odoo` changed the API in {target_ver}").
- **Version Upgrades**: The upgraded module version MUST follow the format: `MAJOR_ODOO.MINOR_ODOO.MAJOR_MODULE.MINOR_MODULE.PATCH_MODULE` (e.g., `{target_ver}.1.0.0`).
  - When upgrading to a new major Odoo version, the module part of the version MUST start at `1.0.0`.

### Mandatory Rules:
- **ALWAYS** use the `--no-pretty` flag (placed **before** the database name) with every `odev` command (create, run, test, venv).
- **MANDATORY**: When using `odev create`, you MUST ALWAYS specify the Odoo version with `-V <version>` AND use `--no-pretty` before the database name.
  - **Correct Example**: `odev create -V {target_ver} --no-pretty {target_db}`
- If `TASKS.md` or `UPGRADE.md` already exist, read them first to resume work. Update `task.md` frequently.{verify_instructions}
- **Strictly Upgrade Only**: DO NOT rewrite, refactor, or change functionality of code or views (e.g., do not add new attributes like `invisible` "for a professional touch"). Your sole task is making the module compatible with the target version.
- **Maintain Feature Coverage**: If a feature or piece of code is broken during the upgrade, **DO NOT** simply remove it. You must maintain the same feature coverage by either replacing it with a new implementation compatible with {target_ver} or by implementing a workaround.
- **Delegate Heavy Tasks**: If your CLI supports sub-agents or delegation tools (like `generalist`), use them to parallelize or offload heavy analysis, repetitive editing, or complex research tasks.
- **Package Installation**: If you encounter a `ModuleNotFoundError` or need to install a python package, **ALWAYS** use: `odev venv {target_db} -c "pip install <package>"` to ensure it is installed in the correct virtual environment.
- **Status Prefix**: When providing updates, thinking, or describing your plan in the chat, **ALWAYS** prefix your message with the name of the module you are currently working on in brackets (e.g., `[module_name] Your message here`).
"""
        if self.args.comment:
            prompt += f"\n### Additional User Instructions:\n- {self.args.comment}\n"

        sandbox_dirs = [str(self.args.path.resolve())]
        if from_odoo_path and "Unknown" not in from_odoo_path:
            sandbox_dirs.append(from_odoo_path)
        if target_odoo_path and "Unknown" not in target_odoo_path:
            sandbox_dirs.append(target_odoo_path)

        return prompt, sandbox_dirs, from_ver, target_ver, target_db

    def _run_upgrade(self) -> None:
        """Internal logic for the upgrade process."""
        prepared = self._prepare_upgrade()
        if not prepared:
            return

        prompt, sandbox_dirs, from_ver, target_ver, target_db = prepared

        agent = self.get_ai_agent()

        try:
            logger.info("=" * 60)
            logger.info(f"Starting Project-wide AI Upgrade using {self.args.cli}")
            logger.info(f"From {from_ver} to {target_ver} (Target DB: {target_db})")
            logger.info("=" * 60)

            agent.run(prompt, sandbox_dirs, database=target_db, version=target_ver)

        finally:
            logger.info("Finishing upgrade session.")

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
