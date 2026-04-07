# ODEV Commands Skill

This skill provides instructions on how to use `odev` (Odoo Development CLI) during upgrade tasks.

## 🛑 MANDATORY RULES (ODEV CLI USAGE)

1. **ARGUMENT ORDER**: `odev` specific flags (LIKE `-V`, `-f`, `-c`, `-w`, `--venv`) MUST come BEFORE the database name,
   or immediately after it but BEFORE any `odoo-bin` specific arguments.

    - **Correct**: `odev run -V 18.0 my_db -i base --http-port 8069`
    - **Incorrect**: `odev run my_db -i base -V 18.0` (This mixes `odev` and `odoo-bin` args and is confusing).

2. **DATABASE CREATION/OVERWRITE**: You MUST ALWAYS use the `-f` (force) flag when creating a database that might
   already exist to bypass confirmation prompts. "Overwriting" an existing database via `odev create` REQUIRES `-f`.

    - **Example**: `odev create -f -V 18.0 new_db -i base`

3. **ODOO-BIN SYNTAX**: All `odoo-bin` standard arguments must follow Odoo's official CLI syntax.

    - Use `--without-demo` instead of `without-demo=False`.
    - Use `--stop-after-init` instead of `stop-after-init=True`.
    - Use `--http-port 8069` instead of `http-port=8069`.

4. **LOG LEVEL**: Use `--log-level=warn` to keep logs concise and save tokens. Note that `odev` consumes this flag to
   set BOTH its own log level and the Odoo server log level.
5. **GLOB QUOTING**: When using `--glob`, you MUST ALWAYS wrap the pattern in double quotes to prevent the shell from
   expanding it before it reaches `odev`.
    - **Correct**: `odev upgrade-code --glob "my_module/**/*"`
    - **Incorrect**: `odev upgrade-code --glob my_module/**/*`

## Core Commands

-   **`odev run <database> [options]`**:

    -   Starts the Odoo server.
    -   Useful for manual verification and testing.
    -   **Best Practice**: Always specify `--http-port <free_port>` to avoid conflicts.
    -   **Example**: `odev run -V 17.0 db -i mod --http-port 8069 --log-level=warn`

-   **`odev create <database> [options]`**:

    -   Creates a new database and installs modules.
    -   **MANDATORY**: You MUST always specify the Odoo version with `-V <version>`.
    -   **MANDATORY**: Use `-f` to force creation if the DB already exists.
    -   Use `-T` to create a template database (e.g., `odev create -f -T -V 17.0 my_template -i sale`).
    -   Use `-t <template>` to clone from a template (e.g., `odev create -f -t my_template -V 17.0 new_db`).

-   **`odev test <database> -i <modules>`**:

    -   Runs the Odoo test suite for the specified modules.
    -   **Log Verification**: Always inspect the logs even if the command exits with 0. Look for Registry failures,
        TypeErrors, or AttributeErrors.

-   **`odev venv <database> -c "<command>"`**:

    -   Runs a command inside the virtual environment associated with the database.
    -   **Package Installation**: If a python package is missing, use:
        `odev venv <database> -c "pip install <package>"`.

-   **`odev upgrade-code <database> --from <ver> --to <ver>`**:
    -   Automatically migrates source code for common renames (e.g., `<tree>` to `<list>` in Odoo 18.0+).
    -   **MANDATORY**: You MUST specify the **target database name**, not a directory path.

## Best Practices

-   **Speed up iteration**: Use template databases (`-T` and `-t`) to avoid reinstalling standard Odoo modules
    repeatedly.
-   **Strict Versioning**: Always pass the correct `-V` flag to ensure the right Odoo worktree and virtual environment
    are used.
-   **Prompt Bypassing**: Always use `-f` in scripts or automated tasks to ensure no interactive prompts block
    execution.
