"""Upgrade Odoo modules using AI."""

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import jinja2
import networkx as nx

from odev.common import args, progress, string
from odev.common.commands import DatabaseCommand
from odev.common.connectors import GitConnector
from odev.common.databases import LocalDatabase
from odev.common.logging import logging
from odev.common.mixins.databases.list import ListLocalDatabasesMixin
from odev.common.odoobin import ODOO_UPGRADE_REPOSITORY, OdoobinProcess
from odev.common.utils import EmployeeUtils
from odev.common.version import OdooVersion

from odev.plugins.odev_plugin_ai.common.mixins import AICommandMixin


if TYPE_CHECKING:
    from odev.plugins.odev_plugin_ai_upgrade.common.knowledge import KnowledgeIndex


logger = logging.getLogger(__name__)

REQUIRED_SKILLS = (
    "odev",
    "odoo_upgrade_skill",
    "odoo_upgrade_utils",
    "custom_util",
    "odoo-commit-message-guidelines",
)
"""Skills the AI agent is told to activate, checked before the session starts.

Single source of truth: the prompt template renders this same tuple. Keeping two lists
meant a skill could be mandated but never verified, and the mismatch only surfaced once
the agent was already running. Names must match the ``name`` field of each SKILL.md,
which is what the `skills` CLI reports — not necessarily its directory name.
"""

MANIFEST_CONTRACT_MAX_CHARS = 6000
"""How much of a manifest's documented behaviour to carry into the prompt.

Generous on purpose: a manifest that spells out three features in prose is exactly the case
where truncating loses the ones at the end, which are then never tested. When the text does
exceed this, the excerpt is marked and the reader is pointed at the file.
"""

TEST_METHOD_REGEX = re.compile(r"^\s+def (test_\w+)", re.MULTILINE)
"""Matches test method definitions to inventory a module's test coverage statically."""

TEST_TOTALS_REGEX = re.compile(r"(?P<failures>\d+) failed, (?P<errors>\d+) error\(s\) of (?P<tests>\d+) tests")
"""Matches the aggregate test result Odoo logs on the ``odoo.tests.result`` logger.

Emitted for *post-install* tests, which is the path taken when the suite runs after the
registry is loaded. It carries no per-module breakdown, so it is used for the totals and
as the proof that tests ran at all.
"""

TEST_MODULE_RESULTS_REGEX = re.compile(
    r"Module (?P<module>\S+): (?P<failures>\d+) failures, (?P<errors>\d+) errors of (?P<tests>\d+) tests"
)
"""Matches the per-module test summary from ``odoo/modules/loading.py``.

Only emitted when tests run *at module load time* and the suite is not successful. The
wording is identical from 15.0 to 19.0.
"""

TEST_FAILURE_REGEX = re.compile(r"^(?:FAIL|ERROR):\s+(?P<test>\S+)")
"""Matches an individual test failure, whose logger names the module it belongs to.

This is the only per-module attribution available for post-install tests.
"""


@dataclass
class BaselineReport:
    """Outcome of running the modules' existing test suites on the *source* version.

    This has to be established before any code is touched. Its value is precisely that
    it ran on the old version: a test that then passes on the new one **without being
    modified** is evidence that behaviour was preserved. A suite that was already red
    beforehand would otherwise send the upgrade hunting for a regression it did not
    cause, and a suite that never ran at all cannot prove anything.
    """

    database: str
    version: str
    log_path: Path | None = None
    """Full odoo-bin log of the baseline run, kept for inspection."""
    tests_declared: dict[str, list[str]] = field(default_factory=dict)
    """Test method names found by statically scanning each module's ``tests`` directory."""
    tests_run: dict[str, int] = field(default_factory=dict)
    """Test methods actually executed, counted from the ``Starting ...`` log lines."""
    tests_failed: dict[str, int] = field(default_factory=dict)
    """Failed or errored tests per module."""
    totals: tuple[int, int, int] | None = None
    """``(failures, errors, tests_run)`` as reported by Odoo, when it reported them."""
    modules_loaded: bool = False
    """Whether Odoo reached the end of module loading.

    A positive signal, taken from the ``Modules loaded.`` log line (``odoo/modules/loading.py``,
    unchanged from 15.0 to 19.0). The exit code cannot be used for this: ``--test-enable``
    also exits non-zero when tests merely fail, which is a red baseline, not a broken install.
    """
    install_failed: bool = False
    """Whether the modules failed to install on their own source version."""

    @property
    def total_declared(self) -> int:
        return sum(len(names) for names in self.tests_declared.values())

    @property
    def total_run(self) -> int:
        return sum(self.tests_run.values())

    @property
    def total_failed(self) -> int:
        if self.totals is not None:
            failures, errors, _ = self.totals
            return failures + errors
        return sum(self.tests_failed.values())

    @property
    def is_green(self) -> bool:
        return not self.install_failed and not self.total_failed

    @property
    def modules_without_tests(self) -> list[str]:
        """Modules with no test at all.

        Note this is *not* the set of modules needing baseline work: a module that already
        has tests can still leave most of its documented behaviour uncovered. Coverage is
        measured against the manifest contract, which only the agent can read.
        """
        return [module for module, names in self.tests_declared.items() if not names]

    @property
    def modules_not_covered(self) -> list[str]:
        """Modules that declare tests which did not run.

        ``--test-tags '/module'`` only runs tests of modules present in ``--init``, so a
        suite can silently not run at all. Declared-but-not-run means the evidence is
        missing, which is worse than a red suite because nothing signals it.
        """
        return [module for module, names in self.tests_declared.items() if names and not self.tests_run.get(module)]


@dataclass
class UpgradeContext:
    """Everything resolved before handing the upgrade over to the AI agent."""

    from_ver: str
    target_ver: str
    from_odoo_path: str
    target_odoo_path: str
    project_path: Path
    baseline_db: str
    target_db: str
    sandbox_dirs: list[str]
    extra_bind_dirs: list[str]
    upgrade_instructions: str
    knowledge_path: str | None
    knowledge_index: Any
    """The ``KnowledgeIndex``, or None when it is not configured.

    Deliberately untyped: a *string* annotation on a dataclass field crashes when this
    module is imported by odev's plugin loader, because `dataclasses._is_type` resolves it
    with an unguarded ``sys.modules.get(cls.__module__).__dict__`` and the loader does not
    register plugin modules in ``sys.modules``. ``KnowledgeIndex`` is only importable under
    ``TYPE_CHECKING``, so it cannot be referenced unquoted here.
    """
    modules: list[dict]
    standard_deps: dict[str, str]
    baseline: BaselineReport | None = None

    @property
    def module_names(self) -> list[str]:
        return [module["name"] for module in self.modules]

    @property
    def is_enterprise(self) -> bool:
        """Whether any resolved standard dependency lives in enterprise."""
        return "enterprise" in self.standard_deps.values()


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

    no_baseline = args.Flag(
        aliases=["--no-baseline"],
        description="""
        Skip establishing a test baseline on the source version before upgrading.
        Without a baseline the upgrade can only prove that the modules install, never
        that their behaviour was preserved, so only skip this when you already have
        one committed.
        """,
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
                        "contract": self._manifest_contract(manifest),
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
                                "contract": self._manifest_contract(manifest),
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
        from_ver: str,
        target_ver: str,
        project_path: Path,
        worktrees_path: Path,
        venvs_path: Path,
    ) -> tuple[str, str, list[str], list[str]]:
        """Configure baseline and target database names, and sandbox directories."""
        base_db_name = (
            self._database.name
            if getattr(self, "_database", None) and self._database.platform.name != "dummy"
            else (self.args.module_name or self.args.path.name or "odoo")
        )
        baseline_db = f"{base_db_name}_{from_ver.replace('.', '_')}_baseline"
        target_db = f"{base_db_name}_{target_ver.replace('.', '_')}_upgrade"

        sandbox_dirs = [str(project_path)]
        extra_bind_dirs = [
            str(worktrees_path),
            str(venvs_path),
        ]
        return baseline_db, target_db, sandbox_dirs, extra_bind_dirs

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

        # Enumerate what each module actually declares. The manifest cannot be trusted as
        # the behaviour inventory: in custom code it is routinely empty, partial or stale.
        from odev.plugins.odev_plugin_ai_upgrade.common.surface import extract_surface

        for module in modules_info:
            module["surface"] = extract_surface(module["path"])

        return modules_info

    def _prepare_upgrade(self) -> UpgradeContext:
        """Resolve versions, modules, databases and knowledge sources for the upgrade."""
        from_ver, target_ver = self._detect_versions()
        project_path = Path(self.args.path).resolve()
        worktrees_path = self.odev.worktrees_path.resolve()
        venvs_path = self.odev.venvs_path.resolve()
        upgrade_path = self.config.paths.upgrade.resolve()

        baseline_db, target_db, sandbox_dirs, extra_bind_dirs = self._get_sandbox_config(
            from_ver, target_ver, project_path, worktrees_path, venvs_path
        )
        modules_info = self._get_modules_info()

        upgrade_instructions = self._setup_upgrade_instructions(upgrade_path, extra_bind_dirs)

        # Both worktrees are needed: the target to install on, the source to run the
        # baseline suite against (and to research the breaking changes via git history).
        self._prepare_odoo_environment([from_ver, target_ver])

        from_worktree = worktrees_path / from_ver
        if not from_worktree.exists():
            if not self.args.no_baseline:
                raise self.error(
                    f"No Odoo {from_ver} worktree at {from_worktree}, which is required to establish the "
                    f"test baseline. Run `odev worktree -C {from_ver} -V {from_ver}`, or pass --no-baseline "
                    "to upgrade without preservation evidence."
                )
            logger.warning(f"No Odoo {from_ver} worktree available; source code research will be limited.")
        from_odoo_path = str(from_worktree) if from_worktree.exists() else f"Virtual ({from_ver})"
        target_odoo_path = str(worktrees_path / target_ver)

        standard_deps = self._resolve_standard_deps(modules_info, from_ver)
        ki, knowledge_local_path = self._setup_knowledge_index_context(standard_deps, from_ver)
        if knowledge_local_path:
            sandbox_dirs.append(knowledge_local_path)

        if (self.args.path / "UPGRADE.md").exists():
            logger.info(f"Existing upgrade report found at {self.args.path / 'UPGRADE.md'}")

        return UpgradeContext(
            from_ver=from_ver,
            target_ver=target_ver,
            from_odoo_path=from_odoo_path,
            target_odoo_path=target_odoo_path,
            project_path=project_path,
            baseline_db=baseline_db,
            target_db=target_db,
            sandbox_dirs=sandbox_dirs,
            extra_bind_dirs=extra_bind_dirs,
            upgrade_instructions=upgrade_instructions,
            knowledge_path=knowledge_local_path,
            knowledge_index=ki,
            modules=modules_info,
            standard_deps=standard_deps,
        )

    @staticmethod
    def _manifest_contract(manifest: dict) -> str:
        """Return the module's documented behaviour, as declared in its manifest.

        This text is the contract the upgrade must preserve, and it is what the test
        coverage has to be measured against — a module with a `tests` directory can still
        leave most of what it promises untested.
        """
        parts = [str(manifest.get(key, "") or "").strip() for key in ("summary", "description")]
        contract = "\n".join(part for part in parts if part)
        if len(contract) > MANIFEST_CONTRACT_MAX_CHARS:
            contract = (
                contract[:MANIFEST_CONTRACT_MAX_CHARS].strip()
                + "\n\n[… truncated — read `__manifest__.py` in full before listing behaviours]"
            )
        return contract.strip()

    def _inventory_tests(self, modules_info: list[dict]) -> dict[str, list[str]]:
        """Return the test methods each module declares, by name.

        The names matter as much as the count: eleven tests all named ``test_job_number_*``
        show at a glance that one documented feature is covered and the others are not.
        Custom modules usually have no tests, too few, or stale ones.
        """
        declared: dict[str, list[str]] = {}
        for module in modules_info:
            names: list[str] = []
            tests_dir = module["path"] / "tests"
            if tests_dir.is_dir():
                for test_file in sorted(tests_dir.glob("test_*.py")):
                    try:
                        content = test_file.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError) as error:
                        logger.debug(f"Could not read {test_file}: {error}")
                        continue
                    names.extend(TEST_METHOD_REGEX.findall(content))
            declared[module["name"]] = names
        return declared

    def _custom_addons_roots(self, modules_info: list[dict]) -> list[Path]:
        """Return the distinct addons roots holding the modules to upgrade."""
        roots: list[Path] = []
        for module in modules_info:
            root = module["path"].parent
            if root not in roots and OdoobinProcess.check_addons_path(root):
                roots.append(root)
        return roots

    def _run_baseline(self, ctx: UpgradeContext) -> BaselineReport:
        """Install the modules on their source version and run their tests.

        All modules are installed together, as in production: cross-module view modifiers
        and overrides change behaviour, and a test can fail for reasons belonging to a
        sibling module.
        """
        report = BaselineReport(
            database=ctx.baseline_db,
            version=ctx.from_ver,
            tests_declared=self._inventory_tests(ctx.modules),
        )

        if not report.total_declared:
            logger.warning(
                f"None of the {len(ctx.modules)} module(s) declare any test. There is no baseline to inherit, "
                "so the upgrade will have to write one before changing any code."
            )
        else:
            logger.info(
                f"Baseline inventory: {report.total_declared} test method(s) declared across "
                f"{len(ctx.modules) - len(report.modules_without_tests)} of {len(ctx.modules)} module(s)."
            )

        module_names = ctx.module_names
        # `home_path / "tmp"` is gitignored by odev, so the log does not pollute the repo.
        log_dir = self.odev.home_path / "tmp" / "baseline"
        report.log_path = log_dir / f"{ctx.baseline_db}.log"
        log_dir.mkdir(parents=True, exist_ok=True)
        report.log_path.unlink(missing_ok=True)

        # Let the AI agent read the log from inside the sandbox.
        if str(log_dir) not in ctx.extra_bind_dirs:
            ctx.extra_bind_dirs.append(str(log_dir))

        self.odev.run_command("create", "--force", "--bare", "--version", ctx.from_ver, ctx.baseline_db)

        database = LocalDatabase(ctx.baseline_db)
        odoobin = database.process or self.odev.odoobin_process_class(database)
        odoobin.with_version(OdooVersion(ctx.from_ver))
        odoobin.with_edition("enterprise" if ctx.is_enterprise else "community")
        odoobin.additional_addons_paths = self._custom_addons_roots(ctx.modules)

        run_args = [
            # Send the log to a file rather than the console: installing every standard
            # dependency and running the suite produces thousands of lines that would bury
            # the upgrade's own progress. It is parsed below, and kept for inspection.
            #
            # This also has to survive `OdoobinProcess.run` discarding our `stream_filter`,
            # which it does whenever `addons_debuggers()` finds a call to a debugger -- and
            # that scan covers Odoo core, where `ir_qweb.load_debugger` is a false positive
            # on every 17.0 run.
            *["--logfile", report.log_path.as_posix()],
            "--stop-after-init",
            "--test-enable",
            # Restrict to the custom modules' own tests. Odoo only runs tests of modules
            # present in --init, so this cannot pull in the whole standard suite.
            *["--test-tags", ",".join(f"/{name}" for name in module_names)],
            *["--init", ",".join(module_names)],
        ]

        with progress.spinner(f"Installing {len(module_names)} module(s) and running their tests on {ctx.from_ver}"):
            odoobin.run(args=run_args, stream=False)

        self._parse_baseline_log(report)
        report.install_failed = not report.modules_loaded

        self._log_baseline_outcome(report)
        return report

    def _parse_baseline_log(self, report: BaselineReport) -> None:
        """Extract test statistics from the baseline run's log file."""
        if not report.log_path or not report.log_path.exists():
            logger.warning(f"No baseline log written at {report.log_path}; test statistics unavailable.")
            return

        with report.log_path.open(encoding="utf-8", errors="replace") as log_file:
            for line in log_file:
                match = OdoobinProcess.LOG_REGEX.match(string.strip_ansi_colors(line).replace("\r", ""))
                if match is None:
                    continue

                # Odoo's formatter appends a trailing space to every log line, so this is
                # stripped before any comparison.
                description = str(match.group("description")).strip()
                module = match.group("module")

                if description == "Modules loaded.":
                    report.modules_loaded = True
                elif description.startswith("Starting ") and module:
                    report.tests_run[module] = report.tests_run.get(module, 0) + 1
                elif TEST_FAILURE_REGEX.match(description) and module:
                    report.tests_failed[module] = report.tests_failed.get(module, 0) + 1
                elif module_results := TEST_MODULE_RESULTS_REGEX.match(description):
                    # Tests that ran at module load time: authoritative per-module count.
                    failed = int(module_results.group("failures")) + int(module_results.group("errors"))
                    report.tests_failed[module_results.group("module")] = failed
                elif totals := TEST_TOTALS_REGEX.match(description):
                    report.totals = (
                        int(totals.group("failures")),
                        int(totals.group("errors")),
                        int(totals.group("tests")),
                    )

    def _print_baseline_failures(self, report: BaselineReport) -> None:
        """Echo the failing tests from the log, with their tracebacks and nothing else."""
        if not report.log_path or not report.log_path.exists():
            return

        with report.log_path.open(encoding="utf-8", errors="replace") as log_file:
            in_failure = False
            for line in log_file:
                clean_line = string.strip_ansi_colors(line).replace("\r", "").rstrip()
                match = OdoobinProcess.LOG_REGEX.match(clean_line)

                if match is None:
                    # Continuation of the current failure: its traceback.
                    if in_failure:
                        self.print(clean_line, highlight=False, soft_wrap=False)
                    continue

                in_failure = bool(TEST_FAILURE_REGEX.match(str(match.group("description"))))
                if in_failure:
                    self.print(clean_line, highlight=False, soft_wrap=False)

    def _log_baseline_outcome(self, report: BaselineReport) -> None:
        """Report what the baseline proved, and what it could not."""
        if report.install_failed:
            self._print_baseline_failures(report)
            raise self.error(
                f"The modules do not install on their own source version ({report.version}) in database "
                f"{report.database!r}. Fix that before upgrading: otherwise any failure on the target "
                f"version is indistinguishable from a pre-existing one. Full log: {report.log_path}"
            )

        if report.modules_not_covered:
            logger.warning(
                "These modules declare tests that did not run: "
                f"{string.join_and(report.modules_not_covered)}. "
                "A suite that does not run cannot be preservation evidence — check the test tags "
                "and whether the tests are discoverable (missing tests/__init__.py imports)."
            )

        if report.total_failed:
            details = ", ".join(
                f"{module} ({failed} failed)" for module, failed in report.tests_failed.items() if failed
            )
            logger.warning(
                f"The baseline suite is already RED on {report.version}: "
                f"{report.total_failed} of {report.total_run} test(s) failing"
                f"{f' — {details}' if details else ''}."
            )
            self._print_baseline_failures(report)
            logger.warning(
                "These failures pre-date the upgrade: they are NOT regressions you introduced. "
                "Fixing them is a separate decision from the upgrade itself. "
                f"Full log: {report.log_path}"
            )
        elif report.total_run:
            logger.info(f"Baseline is green: {report.total_run} test(s) passed on {report.version}.")

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
        standard_deps: dict[str, str],
        from_ver: str,
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

            if standard_deps:
                logger.info(f"Knowledge index: tracking {len(standard_deps)} standard Odoo module dependencies.")
            else:
                logger.warning(f"Knowledge index: no standard Odoo dependencies found for {from_ver}.")

            local_path = ki.local_path.resolve().as_posix()
            return ki, local_path
        except Exception as e:
            logger.warning(f"Knowledge index unavailable: {e}. Proceeding without it.")
            return None, None

    def _build_final_prompt(self, ctx: UpgradeContext) -> str:
        """Compose the full AI prompt from various components."""
        repo_name = ctx.project_path.name
        is_ps_custom = repo_name.startswith("ps") and repo_name.endswith("-custom")

        template_path = Path(__file__).parent.parent / "templates" / "upgrade_prompt.md.j2"
        with open(template_path, encoding="utf-8") as f:
            template_content = f.read()

        template = jinja2.Template(template_content)
        return template.render(
            from_ver=ctx.from_ver,
            target_ver=ctx.target_ver,
            from_odoo_path=ctx.from_odoo_path,
            target_odoo_path=ctx.target_odoo_path,
            project_path=ctx.project_path,
            upgrade_instructions=ctx.upgrade_instructions,
            k_path=ctx.knowledge_path or "/knowledge",
            no_ruff=self.args.no_ruff,
            is_ps_custom=is_ps_custom,
            task_id=self.args.task_id,
            comment=self.args.comment,
            submodules=self.args.submodules,
            modules=ctx.modules,
            baseline=ctx.baseline,
            baseline_db=ctx.baseline_db,
            target_db=ctx.target_db,
            required_skills=REQUIRED_SKILLS,
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
        ctx = self._prepare_upgrade()
        if not ctx:
            return

        self._target_db = ctx.target_db
        agent = self.get_ai_agent()

        self._cleanup_wizard(stage="pre-flight", exclude=[ctx.target_db, ctx.baseline_db])

        if not self.args.no_baseline:
            ctx.baseline = self._run_baseline(ctx)
        else:
            logger.warning(
                "Skipping the source-version baseline (--no-baseline): this upgrade will be able to prove "
                "that the modules install, but not that their behaviour was preserved."
            )

        prompt = self._build_final_prompt(ctx)

        logger.info(f"Starting Project-wide AI Upgrade: from {ctx.from_ver} to {ctx.target_ver} ({ctx.target_db})")

        # Check for missing upgrade skills via npx skills list -g
        loaded_skills = self._get_loaded_skills()
        missing = [skill for skill in REQUIRED_SKILLS if skill not in loaded_skills]
        if missing:
            logger.warning(
                f"Missing upgrade skills: {', '.join(missing)}. "
                f"The agent is instructed to activate them and will fail to do so. "
                f"To load them, run: {self._skills_install_command(missing)}"
            )

        if not agent.run(
            prompt,
            ctx.sandbox_dirs,
            extra_bind_dirs=ctx.extra_bind_dirs,
            database=ctx.target_db,
            version=ctx.target_ver,
            resume=self.args.resume,
        ):
            return

        self._sync_knowledge(ctx.knowledge_index, ctx.from_ver, ctx.target_ver)

    def _get_upgrade_databases(self) -> list[str]:
        """Return a list of local databases that look like upgrade or baseline databases."""
        return [db for db in self.list_databases() if db.endswith(("_upgrade", "_baseline"))]

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
