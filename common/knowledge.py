"""Upgrade Knowledge Index — core class for querying, loading and publishing upgrade knowledge."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from odev.common.connectors import GitConnector
from odev.common.logging import logging


if TYPE_CHECKING:
    from odev.common.config import Config
    from odev.common.store.datastore import DataStore


logger = logging.getLogger(__name__)

KNOWLEDGE_SECRETS_KEY = "upgrade-knowledge"
KNOWLEDGE_SECRETS_SCOPE = "api"

KNOWLEDGE_MODULE_HEADER = """\
# Module: {module}
Type: {type}

"""

KNOWLEDGE_SECTION_TEMPLATE = """\
## {from_ver} → {to_ver}
Reviewed: false
Last Updated: {date}

### Field Changes
<!-- List renamed, removed, or moved fields. Include model name and migration util hint. -->

### Method / API Changes
<!-- List removed/renamed methods, changed signatures, or new required overrides. -->

### Framework / View Changes
<!-- List structural changes: QWeb, Kanban card rewrite, JS hooks, OWL components, etc. -->

### Migration Script Notes
<!-- Specific `util.*` calls recommended for this module's data migration, if any. -->

### Notes / Tips
<!-- Any other upgrade-relevant observations discovered during real upgrades. -->

---

"""


class KnowledgeIndex:
    """Manages a private GitHub repository used as a persistent upgrade knowledge base.

    The repository is cloned to the standard odev location
    ``config.paths.repositories / <org> / <repo>``, exactly as ``odev pull`` does
    for every other repository — no separate path configuration required.

    Repository structure::

        index.json              # machine-readable registry of all entries
        knowledge/
            <module>.md         # one file per module, sections for versions

    Usage::

        ki = KnowledgeIndex(config, store)
        ki.ensure_setup()          # runs first-time wizard if not configured
        ki.clone_or_pull()         # sync from remote via GitConnector
        pairs = ki.get_version_pairs("17.0", "19.0")  # [("17.0","18.0"),("18.0","19.0")]
        missing = ki.get_missing_entries(["sale","stock"], pairs)
        ki.create_stub_entries(missing)
        context = ki.load_knowledge(["sale"], pairs)  # compact markdown for AI prompt
        ki.commit_and_pr("odev/upgrade-knowledge-17-19", "feat: add sale knowledge")
    """

    def __init__(self, config: "Config", store: "DataStore"):
        self._config = config
        self._store = store

    # ------------------------------------------------------------------
    # Config shortcut
    # ------------------------------------------------------------------

    @property
    def _knowledge_config(self):
        """Return the KnowledgeSection config object."""
        return self._config.knowledge  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # GitConnector — derives local path from repo_url automatically
    # ------------------------------------------------------------------

    def _get_connector(self) -> GitConnector:
        """Return a GitConnector for the knowledge repository.

        The local clone path follows odev's standard convention:
        ``config.paths.repositories / <org> / <repo>``
        — no extra configuration needed beyond ``repo_url``.
        """
        repo_url = self._knowledge_config.repo_url
        if not repo_url:
            raise RuntimeError("Knowledge repo URL is not configured. Run 'odev upgrade' to set it up.")
        return GitConnector(repo_url)

    @property
    def local_path(self) -> Path:
        """Local filesystem path of the knowledge repository clone.

        Derived from ``repo_url`` via odev's standard ``GitConnector`` path convention:
        ``config.paths.repositories / <org> / <repo>``
        """
        return self._get_connector().path

    # ------------------------------------------------------------------
    # Secrets helpers
    # ------------------------------------------------------------------

    def _get_token(self, ask: bool = False) -> str | None:
        """Retrieve the fine-grained GitHub token from the secrets store."""
        try:
            secret = self._store.secrets.get(
                KNOWLEDGE_SECRETS_KEY,
                scope=KNOWLEDGE_SECRETS_SCOPE,
                fields=["password"],
                prompt_format="Fine-grained GitHub token:",
                ask_missing=ask,
            )
            return secret.password or None
        except Exception:
            return None

    def _save_token(self, token: str) -> None:
        """Persist the fine-grained token to the secrets store."""
        self._store.secrets.set(
            KNOWLEDGE_SECRETS_KEY,
            login="",
            password=token,
            scope=KNOWLEDGE_SECRETS_SCOPE,
        )

    # ------------------------------------------------------------------
    # Setup / first-run wizard
    # ------------------------------------------------------------------

    def is_configured(self) -> bool:
        """Return True if the knowledge index has been fully configured."""
        return bool(self._knowledge_config.repo_url and self._get_token())

    def ensure_setup(self) -> bool:
        """Run the first-time setup wizard if the knowledge index is not yet configured.

        Returns True if setup is complete (already was, or just completed).
        Returns False if the user skipped setup.
        """
        if self.is_configured():
            return True

        from odev.common.console import console

        console.rule("[bold color.cyan]⚡ Upgrade Knowledge Index — First-time Setup[/bold color.cyan]")
        console.print(
            "\nTo use the knowledge index, you need a [bold]private GitHub repository[/bold]\n"
            "and a [bold]fine-grained personal access token[/bold] with these permissions:\n"
        )
        console.print("  [bold color.cyan]Repository permissions[/bold color.cyan] (on your knowledge repo only):")
        console.print("    • [bold]Contents[/bold]:       Read and Write  [dim](to push knowledge branches)[/dim]")
        console.print("    • [bold]Pull requests[/bold]:  Read and Write  [dim](to open review PRs)[/dim]")
        console.print("    • [bold]Metadata[/bold]:       Read            [dim](required by GitHub)[/dim]")
        console.print(
            "\n  Create at: [link=https://github.com/settings/tokens?type=beta]"
            "https://github.com/settings/tokens?type=beta[/link]"
        )
        console.print('  → "Only select repositories" → pick your knowledge repo.\n')

        if not console.confirm("Set up the knowledge index now?", default=True):
            logger.warning("Knowledge index setup skipped. You will be prompted again on the next upgrade.")
            return False

        console.print(
            "\n[dim]Enter the repository URL. odev will clone it to the standard location "
            f"({self._config.paths.repositories}/<org>/<repo>).[/dim]"
        )
        repo_url = console.text("Knowledge repository URL (SSH or HTTPS)")
        if not repo_url:
            logger.error("No repository URL provided. Setup aborted.")
            return False

        # Let the SecretStore prompt and save the token in one step.
        token = self._get_token(ask=True)
        if not token:
            logger.error("No token provided. Setup aborted.")
            return False

        self._knowledge_config.repo_url = repo_url
        logger.info(f"Knowledge index configured. Repository will be cloned to: " f"{GitConnector(repo_url).path}")
        return True

    # ------------------------------------------------------------------
    # Clone / pull via GitConnector
    # ------------------------------------------------------------------

    def clone_or_pull(self) -> None:
        """Clone the knowledge repository if it doesn't exist locally, or pull latest.

        Uses odev's standard ``GitConnector`` — the repository is stored at
        ``config.paths.repositories / <org> / <repo>``, same as any other odev repo.
        """
        connector = self._get_connector()
        if connector.exists:
            connector.pull(force=True)
        else:
            connector.clone()

    # ------------------------------------------------------------------
    # Version pair helpers
    # ------------------------------------------------------------------

    @staticmethod
    def get_version_pairs(
        from_ver: str,
        to_ver: str,
        available_versions: list[str] | None = None,
        upgrade_path: Path | None = None,
        modules: dict[str, str] | None = None,
    ) -> dict[str, list[tuple[str, str]]]:
        """Generate a list of consecutive version pairs between from_ver and to_ver.

        If ``available_versions`` and ``upgrade_path`` are provided, it generates
        per-module pairs based on the presence of migration scripts in the
        ``odoo/upgrade`` repository.

        :returns: ``{module_name: [(from, to), ...]}``
        """
        from odev.common.version import OdooVersion

        start_ov = OdooVersion(from_ver)
        end_ov = OdooVersion(to_ver)

        if start_ov >= end_ov:
            return {}

        # 1. Base consecutive pairs (major only) as fallback
        major_pairs = []
        for v in range(start_ov.major, end_ov.major):
            major_pairs.append((f"{v}.0", f"{v + 1}.0"))

        if not available_versions or not upgrade_path or not modules:
            return {mod: major_pairs for mod in (modules or {"_": ""})}

        # 2. Advanced discovery: include SaaS versions if scripts exist
        ov_all = sorted(OdooVersion(v) for v in available_versions)
        # Filter versions in range [from, to]
        in_range = [ov for ov in ov_all if start_ov <= ov <= end_ov]

        # Ensure from_ver and to_ver are in the list if not already
        if start_ov not in in_range:
            in_range.insert(0, start_ov)
        if end_ov not in in_range:
            in_range.append(end_ov)
        in_range = sorted(list(set(in_range)))

        # Global sequence of consecutive release pairs
        global_steps = []
        for i in range(len(in_range) - 1):
            global_steps.append((str(in_range[i]), str(in_range[i + 1])))

        module_pairs = {}
        for mod in modules:
            mod_steps = []
            current_from = str(start_ov)

            for step_from, step_to in global_steps:
                # Check if this module has a migration script for this jump
                # Odoo upgrade paths: migrations/<module>/<src_version>.*
                # We check for directories matching the version pattern
                if "saas" in step_from:
                    # saas-17.1 -> saas~17.1 (matches 10.saas~17.1.x or saas~17.1.x)
                    ov = OdooVersion(step_from)
                    pattern = f"saas~{ov.major}.{ov.minor}"
                else:
                    # 17.0 -> 17.0.*
                    pattern = f"{step_from}"

                mod_mig_dir = upgrade_path / "migrations" / mod
                has_script = False
                if mod_mig_dir.exists():
                    for entry in mod_mig_dir.iterdir():
                        if entry.is_dir() and (
                            pattern in entry.name if "saas" in step_from else entry.name.startswith(pattern)
                        ):
                            has_script = True
                            break

                # We always include major version jumps to ensure continuity
                is_major_jump = OdooVersion(step_to).major > OdooVersion(step_from).major

                if has_script or is_major_jump:
                    mod_steps.append((current_from, step_to))
                    current_from = step_to

            # If current_from hasn't reached target yet (unlikely if global_steps ends at target),
            # add one last jump.
            if OdooVersion(current_from) < end_ov:
                mod_steps.append((current_from, str(end_ov)))

            module_pairs[mod] = mod_steps

        return module_pairs

    # ------------------------------------------------------------------
    # Index (index.json) operations
    # ------------------------------------------------------------------

    @property
    def _index_path(self) -> Path:
        return self.local_path / "index.json"

    @property
    def _knowledge_dir(self) -> Path:
        return self.local_path / "knowledge"

    def _load_index(self) -> dict:
        """Load index.json or return an empty structure."""
        if self._index_path.exists():
            try:
                return json.loads(self._index_path.read_text())
            except json.JSONDecodeError:
                logger.warning("index.json is malformed, starting fresh.")
        return {"schema_version": "1", "entries": {}}

    def _save_index(self, data: dict) -> None:
        """Write index.json."""
        self._index_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")

    def _update_index_json(self) -> None:
        """Regenerate index.json from filesystem state."""
        data = self._load_index()
        entries: dict[str, list[str]] = {}

        if self._knowledge_dir.exists():
            for module_file in sorted(self._knowledge_dir.iterdir()):
                if not module_file.is_file() or module_file.suffix != ".md":
                    continue
                module = module_file.stem
                content = module_file.read_text()
                # Find all "## from -> to" headers
                pairs = re.findall(r"^##\s+([\w.]+)\s+→\s+([\w.]+)", content, re.MULTILINE)
                if pairs:
                    entries[module] = [f"{f}-{t}" for f, t in pairs]

        data["entries"] = entries
        self._save_index(data)

    def get_missing_entries(
        self,
        modules: dict[str, str],
        version_pairs: dict[str, list[tuple[str, str]]],
    ) -> dict[str, list[tuple[str, str]]]:
        """Return missing knowledge entries for the given standard modules and version pairs.

        :param modules: ``{module_name: "community" | "enterprise"}``
        :param version_pairs: ``{module_name: [(from, to), ...]}``
        :returns: ``{module_name: [(from_ver, to_ver), ...]}`` for entries not yet in the index.
        """
        missing: dict[str, list[tuple[str, str]]] = {}
        for module in modules:
            file_path = self._knowledge_dir / f"{module}.md"
            content = file_path.read_text() if file_path.exists() else ""

            for from_ver, to_ver in version_pairs.get(module, []):
                # Search for the specific version jump header in the consolidated file
                header_pattern = rf"^##\s+{re.escape(from_ver)}\s+→\s+{re.escape(to_ver)}"
                if not re.search(header_pattern, content, re.MULTILINE):
                    missing.setdefault(module, []).append((from_ver, to_ver))
                    continue

                # If the header exists, check for meaningful content in that section
                # Section ends at next "##" or "---" or EOF
                section_match = re.search(rf"{header_pattern}.*?(?=\n##|\n---|$)", content, re.DOTALL | re.MULTILINE)
                if section_match:
                    section_content = section_match.group(0)
                    meaningful_lines = [
                        ln
                        for ln in section_content.splitlines()
                        if ln.strip()
                        and not ln.startswith("#")
                        and not ln.startswith("---")
                        and not ln.startswith("<!--")
                        and not ln.strip().lower().startswith("reviewed:")
                        and not ln.strip().lower().startswith("last updated:")
                    ]
                    if not meaningful_lines:
                        missing.setdefault(module, []).append((from_ver, to_ver))

        return missing

    def create_stub_entries(
        self,
        missing: dict[str, list[tuple[str, str]]],
        modules_types: dict[str, str] | None = None,
    ) -> list[Path]:
        """Add stub sections to module markdown files for all missing entries + update index.json.

        :param missing: Output of :meth:`get_missing_entries`.
        :param modules_types: ``{module_name: "community" | "enterprise"}`` — used to
            populate the ``type`` field in the module header.
        Returns a list of updated file paths.
        """
        updated_files: list[Path] = []
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

        for module, pairs in missing.items():
            file_path = self._knowledge_dir / f"{module}.md"
            self._knowledge_dir.mkdir(parents=True, exist_ok=True)

            if not file_path.exists():
                module_type = (modules_types or {}).get(module, "community")
                file_path.write_text(KNOWLEDGE_MODULE_HEADER.format(module=module, type=module_type))

            content = file_path.read_text()
            new_sections = []
            for from_ver, to_ver in pairs:
                # Double-check we are not duplicating.
                header_pattern = rf"^##\s+{re.escape(from_ver)}\s+→\s+{re.escape(to_ver)}"
                if not re.search(header_pattern, content, re.MULTILINE):
                    new_sections.append(
                        KNOWLEDGE_SECTION_TEMPLATE.format(
                            from_ver=from_ver,
                            to_ver=to_ver,
                            date=today,
                        )
                    )

            if new_sections:
                with file_path.open("a") as f:
                    f.write("\n".join(new_sections))
                logger.debug(f"Added {len(new_sections)} knowledge sections to: {file_path}")
                updated_files.append(file_path)

        if updated_files:
            self._update_index_json()

        return updated_files

    def load_knowledge(
        self,
        modules: dict[str, str],
        version_pairs: dict[str, list[tuple[str, str]]],
        max_chars_per_entry: int = 5000,
    ) -> str:
        """Return a compact markdown string with all available knowledge for the given
        standard modules and version pairs, extracted from consolidated module files.

        :param modules: ``{module_name: "community" | "enterprise"}``
        :param version_pairs: ``{module_name: [(from, to), ...]}``
        """
        sections: dict[tuple[str, str], list[str]] = {}

        for module in modules:
            file_path = self._knowledge_dir / f"{module}.md"
            if not file_path.exists():
                continue

            content = file_path.read_text()
            for from_ver, to_ver in version_pairs.get(module, []):
                header_pattern = rf"^##\s+{re.escape(from_ver)}\s+→\s+{re.escape(to_ver)}"
                # Extract section: from header up to next "##" or "---"
                section_match = re.search(rf"({header_pattern}.*?)(?=\n##|\n---|$)", content, re.DOTALL | re.MULTILINE)
                if not section_match:
                    continue

                section_text = section_match.group(1).strip()

                # Check if it has any real notes besides the header/stub metadata
                meaningful_lines = [
                    ln
                    for ln in section_text.splitlines()
                    if ln.strip()
                    and not ln.startswith("#")
                    and not ln.startswith("---")
                    and not ln.startswith("<!--")
                    and not ln.strip().lower().startswith("reviewed:")
                    and not ln.strip().lower().startswith("last updated:")
                ]
                if not meaningful_lines:
                    continue

                if len(section_text) > max_chars_per_entry:
                    section_text = section_text[:max_chars_per_entry] + "\n\n_[truncated]_"

                sections.setdefault((from_ver, to_ver), []).append(f"#### `{module}` Upgrade Context\n\n{section_text}")

        if not sections:
            return ""

        from odev.common.version import OdooVersion

        sorted_pairs = sorted(sections.keys(), key=lambda x: OdooVersion(x[0]))

        formatted_sections = []
        for pair in sorted_pairs:
            from_ver, to_ver = pair
            formatted_sections.append(
                f"### Knowledge: Odoo {from_ver} → {to_ver}\n\n" + "\n\n---\n\n".join(sections[pair])
            )

        return (
            "### 📚 Pre-Compiled Upgrade Knowledge\n\n"
            "The following knowledge was extracted from your consolidated module files. "
            "**Use it as your primary reference before researching core Odoo repositories.**\n\n"
            + "\n\n".join(formatted_sections)
        )

    def migrate_to_consolidated(self) -> None:
        """One-time migration helper to move from multi-file structure to single-file-per-module.

        Moves knowledge/<module>/<from>-<to>.md -> knowledge/<module>.md (sections).
        """
        import shutil

        if not self._knowledge_dir.exists():
            return

        for entry in self._knowledge_dir.iterdir():
            if entry.is_dir():
                logger.info("Migrating knowledge repository to consolidated structure...")
                module = entry.name
                module_file = self._knowledge_dir / f"{module}.md"
                # Seed module file if it doesn't exist
                if not module_file.exists():
                    # We might not know the type yet, default to community
                    module_file.write_text(KNOWLEDGE_MODULE_HEADER.format(module=module, type="community"))

                sections_added = 0
                # Process each version-pair file in the subdirectory
                for ver_file in sorted(entry.iterdir()):
                    if ver_file.suffix == ".md":
                        # Match name: from-to.md
                        ver_match = re.match(r"^([\w.]+)-([\w.]+)\.md$", ver_file.name)
                        if ver_match:
                            fv, tv = ver_match.groups()
                            content = ver_file.read_text()
                            # Strip frontmatter
                            content = re.sub(r"^---\n.*?\n---\n", "", content, flags=re.DOTALL).strip()

                            # Prepare section
                            section = f"## {fv} → {tv}\nReviewed: false\nLast Updated: migrated\n\n{content}\n\n---\n\n"
                            with module_file.open("a") as f:
                                f.write(section)
                            sections_added += 1

                if sections_added:
                    logger.info(f"Consolidated {sections_added} sections into {module}.md")

                # Remove the now-empty (or fully migrated) directory
                shutil.rmtree(entry)

        self._update_index_json()

    # ------------------------------------------------------------------
    # Publishing: commit + PR via GitConnector + PyGitHub
    # ------------------------------------------------------------------

    def commit_and_pr(
        self,
        branch_name: str,
        commit_message: str,
        pr_title: str | None = None,
        pr_body: str | None = None,
    ) -> str | None:
        """Commit all local changes to a new branch and open a Pull Request.

        Uses the fine-grained token stored in odev secrets to authenticate the push.
        Returns the PR URL on success, or None if there was nothing to commit.
        """
        from git import GitCommandError, InvalidGitRepositoryError, Repo

        local_path = self.local_path
        token = self._get_token()
        repo_url = self._knowledge_config.repo_url

        if not repo_url:
            raise RuntimeError("Knowledge repo URL is not configured.")

        try:
            repo = Repo(local_path)
        except InvalidGitRepositoryError as e:
            raise RuntimeError(f"Knowledge repo at {local_path} is not a valid git repo.") from e

        if not repo.is_dirty(untracked_files=True):
            logger.info("No changes in the knowledge repository to commit.")
            return None

        # Create/checkout branch
        try:
            repo.git.checkout("-b", branch_name)
        except GitCommandError:
            repo.git.checkout(branch_name)

        repo.git.add("--all")
        repo.git.commit("-m", commit_message)

        # Push using token-authenticated HTTPS URL (SSH falls back naturally)
        auth_push_url = self._authenticated_push_url(repo_url, token)
        try:
            repo.git.push("--set-upstream", auth_push_url, branch_name)
        except GitCommandError as e:
            raise RuntimeError(f"Failed to push knowledge branch {branch_name!r}: {e}") from e

        return self._open_pr(repo_url, token, branch_name, pr_title or commit_message, pr_body or "")

    @staticmethod
    def _authenticated_push_url(repo_url: str, token: str | None) -> str:
        """Inject a fine-grained token into an HTTPS GitHub URL for push authentication.
        SSH URLs are returned unchanged (SSH agent handles auth).
        """
        if not token or repo_url.startswith("git@"):
            return repo_url
        return re.sub(r"^https://", f"https://{token}@", repo_url)

    def _open_pr(
        self,
        repo_url: str,
        token: str | None,
        branch_name: str,
        title: str,
        body: str,
    ) -> str | None:
        """Open a Pull Request using PyGitHub."""
        try:
            from github import Auth as GithubAuth, Github

            # Extract org/repo slug from URL
            slug = re.sub(r"(https://[^/]+/|git@[^:]+:)", "", repo_url).removesuffix(".git")

            g = Github(auth=GithubAuth.Token(token))  # type: ignore[arg-type]
            gh_repo = g.get_repo(slug)

            pr = gh_repo.create_pull(
                title=title,
                body=body
                or (
                    "This PR was automatically created by `odev upgrade` to submit new upgrade knowledge entries.\n\n"
                    "Please review the entries and merge when ready."
                ),
                head=branch_name,
                base=gh_repo.default_branch,
            )
            logger.info(f"Knowledge PR created: {pr.html_url}")
            return pr.html_url
        except Exception as e:
            logger.error(f"Failed to open knowledge PR: {e}")
            return None
