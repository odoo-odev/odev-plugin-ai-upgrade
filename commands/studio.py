import json
import os
import tempfile
from pathlib import Path

from odev.common import args, progress
from odev.common.commands import OdoobinShellCommand
from odev.common.logging import logging

from odev.plugins.odev_plugin_ai.common.mixins import AICommandMixin


logger = logging.getLogger(__name__)


class StudioCommand(OdoobinShellCommand, AICommandMixin):
    """Detects disabled Studio views and generates an upgrade script to fix them."""

    _name = "upgrade-studio"

    path = args.Path(
        aliases=["--path"],
        description="Path where the migration script will be saved (e.g. your custom module).",
        default=Path(".").resolve(),
    )

    view_ids = args.String(
        aliases=["--view-ids"],
        description="Optional comma-separated list of view IDs or XML IDs to strictly filter on.",
        default="",
    )

    def _run_studio_fix(self) -> None:
        logger.info(f"Connecting to database {self._database.name} to extract Studio views...")

        # Prepare the script path
        script_path = Path(__file__).parent.parent / "scripts" / "extract_views.py"
        if not script_path.exists():
            raise self.error(f"Extraction script not found at {script_path}")

        # Temporary file for the output
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            out_file = tmp.name

        # Pass parameters via environment variables
        os.environ["ODEV_STUDIO_OUT_FILE"] = out_file
        os.environ["ODEV_STUDIO_VIEW_IDS"] = self.args.view_ids or ""
        self.args.script = str(script_path)

        try:
            with progress.spinner("Running Odoo ORM extraction..."):
                self.run_script()

            if not os.path.exists(out_file):
                logger.info("No disabled Studio views found (extraction failed to produce output).")
                return

            with open(out_file, "r") as f:
                data = json.load(f)
        finally:
            # Clean up
            if os.path.exists(out_file):
                os.unlink(out_file)
            os.environ.pop("ODEV_STUDIO_OUT_FILE", None)
            os.environ.pop("ODEV_STUDIO_VIEW_IDS", None)

        if isinstance(data, dict) and "error" in data:
            raise self.error(data["error"])

        if not isinstance(data, list):
            raise self.error("Extracted data is not a list.")

        if not data:
            logger.info("No disabled Studio views found.")
            return

        logger.info(f"Found {len(data)} disabled Studio view(s). Preparing AI Agent...")

        # Setup paths
        plugin_root = Path(__file__).parent.parent
        skills_path = (plugin_root / "skills").resolve()

        # Create a temporary directory within the plugin root for the views data.
        # This keeps it within an area we can mark as a 'workspace' for the AI.
        data_tmp_dir = plugin_root / "tmp"
        data_tmp_dir.mkdir(exist_ok=True)

        with tempfile.NamedTemporaryFile(dir=data_tmp_dir, suffix=".json", mode="w", delete=False) as f:
            json.dump(data, f, indent=2)
            views_json_path = str(Path(f.name).resolve())

        target_ver = str(getattr(self._database, "version", ""))
        target_dir = str(self.args.path.resolve())

        # Resolve workspaces (The list of directories the AI is allowed to work in)
        workspaces = [target_dir]
        if skills_path.exists():
            workspaces.append(str(skills_path))
        workspaces.append(str(data_tmp_dir.resolve()))

        extra_bind_dirs = []
        if hasattr(self, "odev") and self.odev.worktrees_path.exists():
            extra_bind_dirs.append(str(self.odev.worktrees_path.resolve()))
            workspaces.append(str(self.odev.worktrees_path.resolve()))

        # Resolve target Odoo source paths
        from odev.common.odoobin import OdoobinProcess
        from odev.common.version import OdooVersion

        target_odoobin = OdoobinProcess(self._database).with_version(OdooVersion(target_ver))

        target_odoopath_host = target_odoobin.odoo_path.resolve()
        target_odoo_path = "/source"

        # We bind the root of the worktree to /source
        # Typically target_odoopath_host is /.../worktrees/19.0/odoo
        # So we bind /.../worktrees/19.0 to /source
        worktree_root = target_odoopath_host.parent
        extra_bind_dirs.append(f"{worktree_root}:/source")

        target_addons_paths = []
        try:
            for p in target_odoobin.addons_paths:
                if p.exists():
                    p_abs = p.resolve()
                    if p_abs.is_relative_to(worktree_root):
                        # Use mapped path
                        rel = p_abs.relative_to(worktree_root)
                        target_addons_paths.append(f"/source/{rel}")
                    else:
                        # Outside worktree, bind separately if needed
                        target_addons_paths.append(str(p_abs))
                        if str(p_abs) not in extra_bind_dirs:
                            extra_bind_dirs.append(str(p_abs))
        except Exception:
            target_addons_paths = []

        prompt = self._build_prompt(data, views_json_path, "/home/odev/skills", target_odoo_path, target_addons_paths)
        # Update extra_bind_dirs for skills mapping which is already handled in upgrade command but let's make it explicit here too
        if skills_path.exists():
            ks_bind = f"{skills_path}:/home/odev/skills"
            if ks_bind not in extra_bind_dirs:
                extra_bind_dirs.append(ks_bind)
        agent = self.get_ai_agent()

        try:
            agent.run(
                prompt,
                workspaces,
                extra_bind_dirs=extra_bind_dirs,
                database=self._database.name,
                version=target_ver,
            )
        finally:
            if os.path.exists(views_json_path):
                os.unlink(views_json_path)
            logger.info("Studio fixing session finished.")

    def run(self) -> None:
        """Run the studio fix command."""
        self._run_studio_fix()

    def _build_prompt(
        self,
        views_data: list[dict],
        views_json_path: str,
        skills_path: str,
        target_odoo_path: str,
        target_addons_paths: list[str],
    ) -> str:
        target_ver = str(getattr(self._database, "version", "unknown"))
        target_dir = str(self.args.path.resolve())

        addons_hint = (
            "\n".join([f"- `{p}` (Addons Path)" for p in target_addons_paths])
            or f"- `{target_odoo_path}/addons` (Default addons)"
        )

        return (
            "You are an expert Odoo Upgrade Engineer and Developer.\n"
            f"Your task is to fix {len(views_data)} deactivated Odoo Studio views for version {target_ver}.\n\n"
            "MANDATORY INSTRUCTIONS:\n"
            "Follow the methodology and confidentiality rules in the following Skill:\n"
            f" - `{skills_path}/studio_upgrade`: Mandatory investigation and fixing workflow.\n\n"
            "RESOURCES:\n"
            f"- VIEW DATA: `{views_json_path}` (Full recursive inheritance chains included).\n"
            f"- TARGET ODOO REPO: `{target_odoo_path}` (Direct git path to source code).\n"
            f"- TARGET ADDONS PATHS:\n{addons_hint}\n"
            f"- TARGET DIRECTORY: `{target_dir}` (Save your migration script here).\n"
        )
