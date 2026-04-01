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

        if not data:
            logger.info("No disabled Studio views found.")
            return

        logger.info(f"Found {len(data)} disabled Studio view(s). Preparing AI Agent...")

        prompt = self._build_prompt(data)
        agent = self.get_ai_agent()

        try:
            target_dir = str(self.args.path.resolve())
            agent.run(
                prompt, [target_dir], database=self._database.name, version=str(getattr(self._database, "version", ""))
            )
        finally:
            logger.info("Studio fixing session finished.")

    def run(self) -> None:
        """Run the studio fix command."""
        self._run_studio_fix()

    def _build_prompt(self, views_data: list[dict]) -> str:
        prompt = (
            "You are an expert Odoo Upgrade Engineer and Developer.\n"
            "Your task is to fix Odoo Studio views that were deactivated (active=False) "
            "during a database upgrade due to architecture validation errors.\n\n"
            "CRITICAL REQUIREMENT:\n"
            "You MUST NOT try to fix the views in the database directly. Instead, you MUST "
            "generate a Python migration script (e.g. `16.0.1.0/post-migration.py`) and save it "
            "into the provided target module directory.\n"
            "You MUST use the `util` library (from https://github.com/odoo/upgrade-util) AND "
            "`custom_util` (from https://github.com/odoo-ps/custom-util) in your script to correct these views.\n"
            "For example, using `custom_util.update_view` or similar utilities.\n"
            "If your migration scripts require external Python libraries (such as `custom-util`), "
            "you MUST add them to the `requirements.txt` file in the root of the module you are upgrading.\n"
            "Common PS utility: `git+https://github.com/odoo-ps/custom-util.git#egg=custom-util`\n\n"
            "Example pattern:\n```python\n"
            "from odoo.upgrade import util\n"
            "from odoo.addons.base.maintenance.custom_util import update_view\n\n"
            "def migrate(cr, version):\n"
            "    # Code to update view arch here using custom_util\n"
            "```\n"
            "Here are the views that encountered an error during the upgrade:\n\n"
        )
        for v in views_data:
            arch = v.get("arch") or ""
            if isinstance(arch, dict):
                arch = arch.get("en_US", next(iter(arch.values())) if arch else "")

            parent_arch = v.get("parent_arch") or ""
            if isinstance(parent_arch, dict):
                parent_arch = parent_arch.get("en_US", next(iter(parent_arch.values())) if parent_arch else "")

            prompt += f"--- VIEW XML_ID: {v['xml_id']} (ID: {v['id']}) ---\n"
            prompt += f"Name: {v['name']}\nModel: {v['model']}\n"
            prompt += f"Parent View: {v['parent_xml_id']} (Name: {v['parent_name']})\n"

            if v.get("error"):
                prompt += f"SPECIFIC ERROR FROM ODOO:\n{v['error']}\n"

            prompt += "Broken Architecture:\n```xml\n" + str(arch) + "\n```\n"
            prompt += (
                "Parent Architecture Context (Look here to see why XPaths might be failing):\n```xml\n"
                + str(parent_arch)
                + "\n```\n"
            )
            prompt += "-" * 50 + "\n\n"

        prompt += (
            "Analyse the broken architecture against the parent view's architecture and the provided error message to figure out "
            "which XPath expression is no longer valid. Then, produce the migration script "
            "using `util` and `custom_util` that properly updates the architecture so the view "
            "can be validated and reactivated. Save the file in the designated path.\n"
        )
        return prompt
