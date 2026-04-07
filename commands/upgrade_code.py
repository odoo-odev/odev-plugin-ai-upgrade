"""Run Odoo's native upgrade_code command."""

from pathlib import Path
from typing import TYPE_CHECKING

from odev.common import args
from odev.common.commands import OdoobinCommand
from odev.common.logging import logging


if TYPE_CHECKING:
    from odev.common.commands.database import DatabaseType
    from odev.common.version import OdooVersion


logger = logging.getLogger(__name__)


class UpgradeCodeCommand(OdoobinCommand):
    """Run Odoo's native upgrade_code tool (Odoo 18.0+).

    This command uses Odoo's internal upgrade_code scripts to automatically
    refactor source code (e.g., renaming <tree> to <list>).
    """

    _name = "upgrade-code"
    _database_arg_required = True

    # Standard Odoo environment arguments
    version_arg = args.String(
        name="version",
        aliases=["-V", "--version"],
        description="The Odoo version to use.",
    )
    venv_arg = args.String(
        name="venv",
        aliases=["--venv"],
        description="Python virtual environment to use.",
    )
    worktree_arg = args.String(
        name="worktree",
        aliases=["-w", "--worktree"],
        description="Git worktree to use.",
    )

    # Upgrade specific flags
    from_version = args.String(
        aliases=["--from"],
        description="Run all scripts starting from this version, inclusive.",
    )
    to_version = args.String(
        aliases=["--to"],
        description="Run all scripts until this version, inclusive.",
    )
    script = args.String(
        aliases=["--script"],
        description="Run this single script name.",
    )
    glob = args.String(
        aliases=["--glob"],
        description="Select the files to rewrite (default: **/*).",
    )
    dry_run = args.Flag(
        aliases=["--dry-run"],
        description="Preview changes without writing.",
    )

    # Re-declare database to ensure its order is correct
    database_arg = args.String(
        name="database",
        description="The database to target (used for version/path resolution).",
    )

    # Catch-all for extra arguments
    odoo_args = args.String(
        nargs="*",
        description="Additional arguments to pass to odoo-bin.",
    )

    def infer_database_instance(self) -> "DatabaseType":
        """Enforce database name and provide helpful error for paths."""
        if self.database_name and (
            "/" in self.database_name or "\\" in self.database_name or "." in self.database_name
        ):
            raise self.error(
                f"Invalid database name: {self.database_name!r}. "
                "odev upgrade-code requires a DATABASE name to resolve the environment context (addons-path, etc.), not a directory path. "
                "Use the designated target database for the upgrade."
            )
        return super().infer_database_instance()

    @property
    def version(self) -> "OdooVersion":
        """Default to target version if specified, as upgrade_code belongs to the target version."""
        if not self.args.version and self.args.to_version:
            from odev.common.version import OdooVersion

            return OdooVersion(self.args.to_version)
        return super().version

    def run(self):
        """Run the odoo-bin upgrade_code process."""
        if self.odoobin is None:
            raise self.error(f"No odoo-bin process could be instantiated for version {self.version!r}")

        # odoo-bin upgrade_code is a special CLI that doesn't support --database or --log-level.
        # We must construct a clean argument list and use venv.run_script().

        # 1. Subcommand
        cmd_args = ["upgrade_code"]

        # 2. Addons path (resolved from OdoobinProcess)
        # We use absolute paths for the scan to ensure reliability within the sandbox.
        # Deduplicate paths (already done in self.odoobin.addons_paths, but we'll be safe)
        custom_paths = list(dict.fromkeys(p.resolve().as_posix() for p in self.odoobin.addons_paths))
        addons_path_str = ",".join(custom_paths)

        cmd_args.extend(["--addons-path", addons_path_str])

        # 3. Upgrade specific flags
        if self.args.script:
            cmd_args.extend(["--script", self.args.script])
        else:
            if self.args.from_version:
                cmd_args.extend(["--from", self.args.from_version])
            if self.args.to_version:
                cmd_args.extend(["--to", self.args.to_version])

        # Handle globbing
        if self.args.glob:
            cmd_args.extend(["--glob", self.args.glob])

        if self.args.dry_run:
            cmd_args.append("--dry-run")

        # 4. Extra arguments & positional path handling
        if self.args.odoo_args:
            for arg in self.args.odoo_args:
                path = Path(arg).resolve()
                if path.exists() and path.is_dir():
                    # If the positional argument is a directory, add it to the scan paths
                    if path.as_posix() not in addons_path_str:
                        addons_path_str += f",{path.as_posix()}"
                        # Update the --addons-path argument we already added to cmd_args
                        idx = cmd_args.index("--addons-path") + 1
                        cmd_args[idx] = addons_path_str
                else:
                    if path.exists() and path.is_file():
                        # This argument looks like a file that resulted from shell expansion.
                        # We'll try to find a common glob if we haven't already.
                        logger.debug(f"Detected likely glob expansion file: {arg}")
                    else:
                        # Append the argument ONLY if it's not an expanded file.
                        # Files will be handled by the glob reconstruction logic below.
                        cmd_args.append(arg)

        # 5. Shell expansion protection: if the glob was expanded, reconstruct it.
        # This is for Odoo's tool which only accepts a single glob pattern string.
        potential_glob_files = []
        if self.args.glob and Path(self.args.glob).exists() and Path(self.args.glob).is_file():
            potential_glob_files.append(Path(self.args.glob).resolve())

        for arg in self.args.odoo_args or []:
            p = Path(arg).resolve()
            if p.exists() and p.is_file():
                potential_glob_files.append(p)

        if len(potential_glob_files) > 1:
            # We have multiple files that likely came from one expanded glob.
            # Reconstruct the most specific common glob pattern.
            import os

            common_parent = Path(os.path.commonpath([f.parent for f in potential_glob_files]))

            # Find which addon_path this common parent belongs to
            for ap in self.odoobin.addons_paths:
                try:
                    rel_parent = common_parent.relative_to(ap.resolve())
                    reconstructed_glob = (rel_parent / "**" / "*").as_posix()

                    # Update the glob in cmd_args
                    try:
                        glob_idx = cmd_args.index("--glob") + 1
                        cmd_args[glob_idx] = reconstructed_glob
                    except ValueError:
                        cmd_args.extend(["--glob", reconstructed_glob])

                    logger.warning(
                        f"Detected shell expansion of your glob pattern. "
                        f'Automatically reconstructed target as --glob "{reconstructed_glob}". '
                        'To avoid this in the future, please quote your glob patterns: --glob "**/*"'
                    )
                    break
                except ValueError:
                    continue

        # Execute using the virtual environment directly.
        # We use a progress callback to capture the output and return code
        # without raising CalledProcessError automatically for exit code 1.
        result = self.odoobin.venv.run_script(
            self.odoobin.odoobin_path,
            cmd_args,
            stream=True,
            progress=lambda line: self.odoobin.console.print(line, end=""),
        )
        if result.returncode not in (0, 1):
            raise self.error(f"Odoo exited with code {result.returncode}")
