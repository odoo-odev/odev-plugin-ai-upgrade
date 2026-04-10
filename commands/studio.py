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

    def _extract_studio_views(self) -> list[dict]:
        """Run Odoo ORM extraction for Studio views."""
        script_path = Path(__file__).parent.parent / "scripts" / "extract_views.py"
        if not script_path.exists():
            raise self.error(f"Extraction script not found at {script_path}")

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            out_file = tmp.name

        os.environ["ODEV_STUDIO_OUT_FILE"] = out_file
        os.environ["ODEV_STUDIO_VIEW_IDS"] = self.args.view_ids or ""
        self.args.script = str(script_path)

        try:
            with progress.spinner("Running Odoo ORM extraction..."):
                self.run_script()

            if not os.path.exists(out_file):
                logger.info("No disabled Studio views found (no output).")
                return []

            with open(out_file, "r") as f:
                data = json.load(f)
        finally:
            if os.path.exists(out_file):
                os.unlink(out_file)
            os.environ.pop("ODEV_STUDIO_OUT_FILE", None)
            os.environ.pop("ODEV_STUDIO_VIEW_IDS", None)

        if isinstance(data, dict) and "error" in data:
            raise self.error(data["error"])

        if not isinstance(data, list):
            raise self.error("Extracted data is not a list.")

        return data

    def _get_studio_agent_config(
        self,
        data: list[dict],
        plugin_root: Path,
    ) -> tuple[str, list[str], list[str]]:
        """Setup paths and environment for the AI agent."""
        skills_path = (plugin_root / "skills").resolve()
        data_tmp_dir = plugin_root / "tmp"
        data_tmp_dir.mkdir(exist_ok=True)

        with tempfile.NamedTemporaryFile(dir=data_tmp_dir, suffix=".json", mode="w", delete=False) as f:
            json.dump(data, f, indent=2)
            views_json_path = str(Path(f.name).resolve())

        target_dir = str(self.args.path.resolve())
        workspaces = [target_dir, str(data_tmp_dir.resolve())]
        if skills_path.exists():
            workspaces.append(str(skills_path))

        extra_bind_dirs = []
        if skills_path.exists():
            extra_bind_dirs.append(f"{skills_path}:/home/odev/skills")

        if hasattr(self, "odev") and self.odev.worktrees_path.exists():
            extra_bind_dirs.append(str(self.odev.worktrees_path.resolve()))
            workspaces.append(str(self.odev.worktrees_path.resolve()))

        return views_json_path, workspaces, extra_bind_dirs

    def _resolve_target_odoo_context(
        self,
        target_ver: str,
        extra_bind_dirs: list[str],
    ) -> tuple[str, list[str]]:
        """Resolve Odoo source paths and addons for the target version."""
        from odev.common.odoobin import OdoobinProcess
        from odev.common.version import OdooVersion

        odoobin = OdoobinProcess(self._database).with_version(OdooVersion(target_ver))
        odoopath_host = odoobin.odoo_path.resolve()
        worktree_root = odoopath_host.parent
        extra_bind_dirs.append(f"{worktree_root}:/source")

        target_addons = []
        try:
            for p in odoobin.addons_paths:
                if not p.exists():
                    continue
                p_abs = p.resolve()
                if p_abs.is_relative_to(worktree_root):
                    target_addons.append(f"/source/{p_abs.relative_to(worktree_root)}")
                else:
                    target_addons.append(str(p_abs))
                    if str(p_abs) not in extra_bind_dirs:
                        extra_bind_dirs.append(str(p_abs))
        except Exception:
            target_addons = []

        return "/source", target_addons

    def _run_studio_fix(self) -> None:
        """Execute the Studio-fix workflow."""
        logger.info(f"Connecting to database {self._database.name}...")
        data = self._extract_studio_views()
        if not data:
            logger.info("No disabled Studio views found.")
            return

        logger.info(f"Found {len(data)} disabled Studio view(s). Starting AI...")

        plugin_root = Path(__file__).parent.parent
        views_json_path, workspaces, extra_bind_dirs = self._get_studio_agent_config(data, plugin_root)
        target_ver = str(getattr(self._database, "version", "unknown"))

        source_path, target_addons = self._resolve_target_odoo_context(target_ver, extra_bind_dirs)
        prompt = self._build_prompt(data, views_json_path, "/home/odev/skills", source_path, target_addons)

        agent = self.get_ai_agent()
        try:
            agent.run(
                prompt, workspaces, extra_bind_dirs=extra_bind_dirs, database=self._database.name, version=target_ver
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
