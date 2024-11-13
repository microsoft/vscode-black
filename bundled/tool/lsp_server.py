# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
"""Implementation of tool support over LSP."""
from __future__ import annotations

import ast
import copy
import json
import os
import pathlib
import re
import sys
import sysconfig
import traceback
from typing import Any, Dict, List, Optional, Sequence, Tuple


# **********************************************************
# Update sys.path before importing any bundled libraries.
# **********************************************************
def update_sys_path(path_to_add: str, strategy: str) -> None:
    """Add given path to `sys.path`."""
    if path_to_add not in sys.path and os.path.isdir(path_to_add):
        if strategy == "useBundled":
            sys.path.insert(0, path_to_add)
        else:
            sys.path.append(path_to_add)


# **********************************************************
# Update PATH before running anything.
# **********************************************************
def update_environ_path() -> None:
    """Update PATH environment variable with the 'scripts' directory.
    Windows: .venv/Scripts
    Linux/MacOS: .venv/bin
    """
    scripts = sysconfig.get_path("scripts")
    paths_variants = ["Path", "PATH"]

    for var_name in paths_variants:
        if var_name in os.environ:
            paths = os.environ[var_name].split(os.pathsep)
            if scripts not in paths:
                paths.insert(0, scripts)
                os.environ[var_name] = os.pathsep.join(paths)
                break


# Ensure that we can import LSP libraries, and other bundled libraries.
BUNDLE_DIR = pathlib.Path(__file__).parent.parent
# Always use bundled server files.
update_sys_path(os.fspath(BUNDLE_DIR / "tool"), "useBundled")
update_sys_path(
    os.fspath(BUNDLE_DIR / "libs"),
    os.getenv("LS_IMPORT_STRATEGY", "useBundled"),
)
update_environ_path()

# **********************************************************
# Imports needed for the language server goes below this.
# **********************************************************
import lsp_edit_utils as edit_utils
import lsp_io
import lsp_jsonrpc as jsonrpc
import lsp_utils as utils
import lsprotocol.types as lsp
from pygls import server, uris, workspace

WORKSPACE_SETTINGS = {}
GLOBAL_SETTINGS = {}
RUNNER = pathlib.Path(__file__).parent / "lsp_runner.py"

MAX_WORKERS = 5
LSP_SERVER = server.LanguageServer(
    name="black-server", version="v0.1.0", max_workers=MAX_WORKERS
)


# **********************************************************
# Tool specific code goes below this.
# **********************************************************
TOOL_MODULE = "black"
TOOL_DISPLAY = "Black Formatter"

# Default arguments always passed to black.
TOOL_ARGS = []

# Minimum version of black supported.
MIN_VERSION = "22.3.0"

# Minimum version of black that supports the `--line-ranges` CLI option.
LINE_RANGES_MIN_VERSION = (23, 11, 0)

# Versions of black found by workspace
VERSION_LOOKUP: Dict[str, Tuple[int, int, int]] = {}

# **********************************************************
# Formatting features start here
# **********************************************************


@LSP_SERVER.feature(lsp.TEXT_DOCUMENT_FORMATTING)
def formatting(params: lsp.DocumentFormattingParams) -> list[lsp.TextEdit] | None:
    """LSP handler for textDocument/formatting request."""

    document = LSP_SERVER.workspace.get_text_document(params.text_document.uri)
    return _formatting_helper(document)


@LSP_SERVER.feature(
    lsp.TEXT_DOCUMENT_RANGE_FORMATTING,
    lsp.DocumentRangeFormattingOptions(ranges_support=True),
)
def range_formatting(
    params: lsp.DocumentRangeFormattingParams,
) -> list[lsp.TextEdit] | None:
    """LSP handler for textDocument/rangeFormatting request."""
    document = LSP_SERVER.workspace.get_text_document(params.text_document.uri)
    settings = _get_settings_by_document(document)
    version = VERSION_LOOKUP[settings["workspaceFS"]]

    if version >= LINE_RANGES_MIN_VERSION:
        return _formatting_helper(
            document,
            args=[
                "--line-ranges",
                f"{params.range.start.line + 1}-{params.range.end.line + 1}",
            ],
        )
    else:
        log_warning(
            "Black version earlier than 23.11.0 does not support range formatting. Formatting entire document."
        )
        return _formatting_helper(document)


@LSP_SERVER.feature(lsp.TEXT_DOCUMENT_RANGES_FORMATTING)
def ranges_formatting(
    params: lsp.DocumentRangesFormattingParams,
) -> list[lsp.TextEdit] | None:
    """LSP handler for textDocument/rangesFormatting request."""
    document = LSP_SERVER.workspace.get_text_document(params.text_document.uri)
    settings = _get_settings_by_document(document)
    version = VERSION_LOOKUP[settings["workspaceFS"]]

    if version >= LINE_RANGES_MIN_VERSION:
        args = []
        for r in params.ranges:
            args += ["--line-ranges", f"{r.start.line + 1}-{r.end.line + 1}"]
        return _formatting_helper(document, args=args)
    else:
        log_warning(
            "Black version earlier than 23.11.0 does not support range formatting. Formatting entire document."
        )
        return _formatting_helper(document)


def is_python(code: str, file_path: str) -> bool:
    """Ensures that the code provided is python."""
    try:
        ast.parse(code, file_path)
    except SyntaxError:
        log_error(f"Syntax error in code: {traceback.format_exc()}")
        return False
    return True


def _formatting_helper(
    document: workspace.Document, args: Sequence[str] = None
) -> list[lsp.TextEdit] | None:
    args = [] if args is None else args
    extra_args = args + _get_args_by_file_extension(document)
    extra_args += ["--stdin-filename", _get_filename_for_black(document)]
    result = _run_tool_on_document(document, use_stdin=True, extra_args=extra_args)
    if result and result.stdout:
        if LSP_SERVER.lsp.trace == lsp.TraceValues.Verbose:
            log_to_output(
                f"{document.uri} :\r\n"
                + ("*" * 100)
                + "\r\n"
                + f"{result.stdout}\r\n"
                + ("*" * 100)
                + "\r\n"
            )

        new_source = _match_line_endings(document, result.stdout)

        # Skip last line ending in a notebook cell
        if document.uri.startswith("vscode-notebook-cell"):
            if new_source.endswith("\r\n"):
                new_source = new_source[:-2]
            elif new_source.endswith("\n"):
                new_source = new_source[:-1]

        # If code is already formatted, then no need to send any edits.
        if new_source != document.source:
            edits = edit_utils.get_text_edits(
                document.source, new_source, lsp.PositionEncodingKind.Utf16
            )
            if edits:
                # NOTE: If you provide [] array, VS Code will clear the file of all contents.
                # To indicate no changes to file return None.
                return edits
    return None


def _get_filename_for_black(document: workspace.Document) -> str:
    """Gets or generates a file name to use with black when formatting."""
    if document.uri.startswith("vscode-notebook-cell") and document.path.endswith(
        ".ipynb"
    ):
        # Treat the cell like a python file
        return document.path[:-6] + ".py"
    return document.path


def _get_line_endings(lines: list[str]) -> str:
    """Returns line endings used in the text."""
    try:
        if lines[0][-2:] == "\r\n":
            return "\r\n"
        return "\n"
    except Exception:  # pylint: disable=broad-except
        return None


def _match_line_endings(document: workspace.Document, text: str) -> str:
    """Ensures that the edited text line endings matches the document line endings."""
    expected = _get_line_endings(document.source.splitlines(keepends=True))
    actual = _get_line_endings(text.splitlines(keepends=True))
    if actual == expected or actual is None or expected is None:
        return text
    return text.replace(actual, expected)


def _get_args_by_file_extension(document: workspace.Document) -> List[str]:
    """Returns arguments used by black based on file extensions."""
    if document.uri.startswith("vscode-notebook-cell"):
        return []

    p = document.path.lower()
    if p.endswith(".py"):
        return []
    elif p.endswith(".pyi"):
        return ["--pyi"]
    elif p.endswith(".ipynb"):
        return ["--ipynb"]
    return []


# **********************************************************
# Formatting features ends here
# **********************************************************


# **********************************************************
# Check file features ends here
# **********************************************************


@LSP_SERVER.feature(
    lsp.TEXT_DOCUMENT_DIAGNOSTIC,
    lsp.DiagnosticRegistrationOptions(
        inter_file_dependencies=False,
        workspace_diagnostics=False,
    ),
)
def diagnostics(params: lsp.DocumentDiagnosticParams) -> lsp.DocumentDiagnosticReport:
    """LSP handler for textDocument/diagnostic request."""
    document = LSP_SERVER.workspace.get_text_document(params.text_document.uri)
    settings = _get_settings_by_document(document)
    diagnostics = []
    if settings.get("check", False):
        diagnostics = _check_document(document)
    return lsp.RelatedFullDocumentDiagnosticReport(items=diagnostics)


def _check_document(document: workspace.Document) -> lsp.DocumentDiagnosticReport:
    """Runs black in check mode to get diagnostics."""
    extra_args = ["--check"] + _get_args_by_file_extension(document)
    extra_args += ["--stdin-filename", _get_filename_for_black(document)]
    result = _run_tool_on_document(document, use_stdin=True, extra_args=extra_args)
    diagnostics = []
    if result and result.stderr and "would reformat" in result.stderr:
        log_to_output(result.stderr)
        diagnostics = [
            lsp.Diagnostic(
                range=lsp.Range(
                    start=lsp.Position(line=0, character=0),
                    end=lsp.Position(line=0, character=0),
                ),
                message="Black would reformat this file.",
                severity=lsp.DiagnosticSeverity.Warning,
                source=TOOL_DISPLAY,
                code="black",
            )
        ]
    return diagnostics


# **********************************************************
# Check file features ends here
# **********************************************************


# **********************************************************
# Required Language Server Initialization and Exit handlers.
# **********************************************************
@LSP_SERVER.feature(lsp.INITIALIZE)
def initialize(params: lsp.InitializeParams) -> None:
    """LSP handler for initialize request."""
    log_to_output(f"CWD Server: {os.getcwd()}")

    GLOBAL_SETTINGS.update(**params.initialization_options.get("globalSettings", {}))

    settings = params.initialization_options["settings"]
    _update_workspace_settings(settings)
    log_to_output(
        f"Settings received on server:\r\n{json.dumps(settings, indent=4, ensure_ascii=False)}\r\n"
    )
    log_to_output(
        f"Global settings received on server:\r\n{json.dumps(GLOBAL_SETTINGS, indent=4, ensure_ascii=False)}\r\n"
    )

    paths = "\r\n   ".join(sys.path)
    log_to_output(f"sys.path used to run Server:\r\n   {paths}")

    _update_workspace_settings_with_version_info(WORKSPACE_SETTINGS)


@LSP_SERVER.feature(lsp.EXIT)
def on_exit(_params: Optional[Any] = None) -> None:
    """Handle clean up on exit."""
    jsonrpc.shutdown_json_rpc()


@LSP_SERVER.feature(lsp.SHUTDOWN)
def on_shutdown(_params: Optional[Any] = None) -> None:
    """Handle clean up on shutdown."""
    jsonrpc.shutdown_json_rpc()


def _update_workspace_settings_with_version_info(
    workspace_settings: dict[str, Any]
) -> None:
    for settings in workspace_settings.values():
        try:
            from packaging.version import parse as parse_version

            result = _run_tool(["--version"], copy.deepcopy(settings))
            code_workspace = settings["workspaceFS"]
            log_to_output(
                f"Version info for formatter running for {code_workspace}:\r\n{result.stdout}"
            )

            if "The typed_ast package is required but not installed" in result.stdout:
                log_to_output(
                    'Install black in your environment and set "black-formatter.importStrategy": "fromEnvironment"'
                )

            # This is text we get from running `black --version`
            # black, 22.3.0 (compiled: yes) <--- This is the version we want.
            first_line = result.stdout.splitlines(keepends=False)[0]
            parts = [v for v in first_line.split(" ") if re.match(r"\d+\.\d+\S*", v)]
            if len(parts) == 1:
                actual_version = parts[0]
            else:
                actual_version = "0.0.0"

            version = parse_version(actual_version)
            min_version = parse_version(MIN_VERSION)
            VERSION_LOOKUP[code_workspace] = (
                version.major,
                version.minor,
                version.micro,
            )

            if version < min_version:
                log_error(
                    f"Version of formatter running for {code_workspace} is NOT supported:\r\n"
                    f"SUPPORTED {TOOL_MODULE}>={min_version}\r\n"
                    f"FOUND {TOOL_MODULE}=={actual_version}\r\n"
                )
            else:
                log_to_output(
                    f"SUPPORTED {TOOL_MODULE}>={min_version}\r\n"
                    f"FOUND {TOOL_MODULE}=={actual_version}\r\n"
                )

        except Exception:
            log_to_output(
                f"Error while detecting black version:\r\n{traceback.format_exc()}"
            )


# *****************************************************
# Internal functional and settings management APIs.
# *****************************************************
def _get_global_defaults():
    return {
        "check": False,
        "path": GLOBAL_SETTINGS.get("path", []),
        "interpreter": GLOBAL_SETTINGS.get("interpreter", [sys.executable]),
        "args": GLOBAL_SETTINGS.get("args", []),
        "importStrategy": GLOBAL_SETTINGS.get("importStrategy", "useBundled"),
        "showNotifications": GLOBAL_SETTINGS.get("showNotifications", "off"),
    }


def _update_workspace_settings(settings):
    if not settings:
        key = utils.normalize_path(os.getcwd())
        WORKSPACE_SETTINGS[key] = {
            "cwd": key,
            "workspaceFS": key,
            "workspace": uris.from_fs_path(key),
            **_get_global_defaults(),
        }
        return

    for setting in settings:
        key = utils.normalize_path(uris.to_fs_path(setting["workspace"]))
        WORKSPACE_SETTINGS[key] = {
            **setting,
            "workspaceFS": key,
        }


def _get_settings_by_path(file_path: pathlib.Path):
    workspaces = {s["workspaceFS"] for s in WORKSPACE_SETTINGS.values()}

    while file_path != file_path.parent:
        str_file_path = utils.normalize_path(file_path)
        if str_file_path in workspaces:
            return WORKSPACE_SETTINGS[str_file_path]
        file_path = file_path.parent

    setting_values = list(WORKSPACE_SETTINGS.values())
    return setting_values[0]


def _get_document_key(document: workspace.Document):
    if WORKSPACE_SETTINGS:
        document_workspace = pathlib.Path(document.path)
        workspaces = {s["workspaceFS"] for s in WORKSPACE_SETTINGS.values()}

        # Find workspace settings for the given file.
        while document_workspace != document_workspace.parent:
            norm_path = utils.normalize_path(document_workspace)
            if norm_path in workspaces:
                return norm_path
            document_workspace = document_workspace.parent

    return None


def _get_settings_by_document(document: workspace.Document | None):
    if document is None or document.path is None:
        return list(WORKSPACE_SETTINGS.values())[0]

    key = _get_document_key(document)
    if key is None:
        # This is either a non-workspace file or there is no workspace.
        key = utils.normalize_path(pathlib.Path(document.path).parent)
        return {
            "cwd": key,
            "workspaceFS": key,
            "workspace": uris.from_fs_path(key),
            **_get_global_defaults(),
        }

    return WORKSPACE_SETTINGS[str(key)]


# *****************************************************
# Internal execution APIs.
# *****************************************************
def get_cwd(settings: Dict[str, Any], document: Optional[workspace.Document]) -> str:
    """Returns cwd for the given settings and document."""
    if settings["cwd"] == "${workspaceFolder}":
        return settings["workspaceFS"]

    if settings["cwd"] == "${fileDirname}":
        if document is not None:
            return os.fspath(pathlib.Path(document.path).parent)
        return settings["workspaceFS"]

    return settings["cwd"]


# pylint: disable=too-many-branches
def _run_tool_on_document(
    document: workspace.Document,
    use_stdin: bool = False,
    extra_args: Sequence[str] = [],
) -> utils.RunResult | None:
    """Runs tool on the given document.

    if use_stdin is true then contents of the document is passed to the
    tool via stdin.
    """
    if utils.is_stdlib_file(document.path):
        log_warning(f"Skipping standard library file: {document.path}")
        return None

    if not is_python(document.source, document.path):
        log_warning(
            f"Skipping non python code or code with syntax errors: {document.path}"
        )
        return None

    # deep copy here to prevent accidentally updating global settings.
    settings = copy.deepcopy(_get_settings_by_document(document))

    code_workspace = settings["workspaceFS"]
    cwd = get_cwd(settings, document)

    use_path = False
    use_rpc = False
    if settings["path"]:
        # 'path' setting takes priority over everything.
        use_path = True
        argv = settings["path"]
    elif settings["interpreter"] and not utils.is_current_interpreter(
        settings["interpreter"][0]
    ):
        # If there is a different interpreter set use JSON-RPC to the subprocess
        # running under that interpreter.
        argv = [TOOL_MODULE]
        use_rpc = True
    else:
        # if the interpreter is same as the interpreter running this
        # process then run as module.
        argv = [TOOL_MODULE]

    argv += TOOL_ARGS + settings["args"] + extra_args

    if use_stdin:
        argv += ["-"]

    if use_path:
        # This mode is used when running executables.
        log_to_output(" ".join(argv))
        log_to_output(f"CWD Server: {cwd}")
        result = utils.run_path(
            argv=argv,
            use_stdin=use_stdin,
            cwd=cwd,
            source=document.source.replace("\r\n", "\n"),
        )
        if result.stderr:
            log_to_output(result.stderr)
    elif use_rpc:
        # This mode is used if the interpreter running this server is different from
        # the interpreter used for running this server.
        log_to_output(" ".join(settings["interpreter"] + ["-m"] + argv))
        log_to_output(f"CWD formatter: {cwd}")

        result = jsonrpc.run_over_json_rpc(
            workspace=code_workspace,
            interpreter=settings["interpreter"],
            module=TOOL_MODULE,
            argv=argv,
            use_stdin=use_stdin,
            cwd=cwd,
            source=document.source,
            env={
                "LS_IMPORT_STRATEGY": settings["importStrategy"],
            },
        )
        result = _to_run_result_with_logging(result)
    else:
        # In this mode the tool is run as a module in the same process as the language server.
        log_to_output(" ".join([sys.executable, "-m"] + argv))
        log_to_output(f"CWD formatter: {cwd}")
        # This is needed to preserve sys.path, in cases where the tool modifies
        # sys.path and that might not work for this scenario next time around.
        with utils.substitute_attr(sys, "path", [""] + sys.path[:]):
            try:
                result = utils.run_module(
                    module=TOOL_MODULE,
                    argv=argv,
                    use_stdin=use_stdin,
                    cwd=cwd,
                    source=document.source,
                )
            except Exception:
                log_error(traceback.format_exc(chain=True))
                raise
        if result.stderr:
            log_to_output(result.stderr)

    return result


def _run_tool(extra_args: Sequence[str], settings: Dict[str, Any]) -> utils.RunResult:
    """Runs tool."""
    code_workspace = settings["workspaceFS"]
    cwd = get_cwd(settings, None)

    use_path = False
    use_rpc = False
    if len(settings["path"]) > 0:
        # 'path' setting takes priority over everything.
        use_path = True
        argv = settings["path"]
    elif len(settings["interpreter"]) > 0 and not utils.is_current_interpreter(
        settings["interpreter"][0]
    ):
        # If there is a different interpreter set use JSON-RPC to the subprocess
        # running under that interpreter.
        argv = [TOOL_MODULE]
        use_rpc = True
    else:
        # if the interpreter is same as the interpreter running this
        # process then run as module.
        argv = [TOOL_MODULE]

    argv += extra_args

    if use_path:
        # This mode is used when running executables.
        log_to_output(" ".join(argv))
        log_to_output(f"CWD Server: {cwd}")
        result = utils.run_path(argv=argv, use_stdin=True, cwd=cwd)
        if result.stderr:
            log_to_output(result.stderr)
    elif use_rpc:
        # This mode is used if the interpreter running this server is different from
        # the interpreter used for running this server.
        log_to_output(" ".join(settings["interpreter"] + ["-m"] + argv))
        log_to_output(f"CWD formatter: {cwd}")
        result = jsonrpc.run_over_json_rpc(
            workspace=code_workspace,
            interpreter=settings["interpreter"],
            module=TOOL_MODULE,
            argv=argv,
            use_stdin=True,
            cwd=cwd,
            env={
                "LS_IMPORT_STRATEGY": settings["importStrategy"],
            },
        )
        result = _to_run_result_with_logging(result)
    else:
        # In this mode the tool is run as a module in the same process as the language server.
        log_to_output(" ".join([sys.executable, "-m"] + argv))
        log_to_output(f"CWD formatter: {cwd}")
        # This is needed to preserve sys.path, in cases where the tool modifies
        # sys.path and that might not work for this scenario next time around.
        with utils.substitute_attr(sys, "path", [""] + sys.path[:]):
            try:
                result = utils.run_module(
                    module=TOOL_MODULE, argv=argv, use_stdin=True, cwd=cwd
                )
            except Exception:
                log_error(traceback.format_exc(chain=True))
                raise
        if result.stderr:
            log_to_output(result.stderr)

    if LSP_SERVER.lsp.trace == lsp.TraceValues.Verbose:
        log_to_output(f"\r\n{result.stdout}\r\n")

    return result


def _to_run_result_with_logging(rpc_result: jsonrpc.RpcRunResult) -> utils.RunResult:
    error = ""
    if rpc_result.exception:
        log_error(rpc_result.exception)
        error = rpc_result.exception
    elif rpc_result.stderr:
        log_to_output(rpc_result.stderr)
        error = rpc_result.stderr
    return utils.RunResult(rpc_result.stdout, error)


# *****************************************************
# Logging and notification.
# *****************************************************
def log_to_output(
    message: str, msg_type: lsp.MessageType = lsp.MessageType.Log
) -> None:
    """Logs messages to Output > Black Formatter channel only."""
    LSP_SERVER.show_message_log(message, msg_type)


def log_error(message: str) -> None:
    """Logs messages with notification on error."""
    LSP_SERVER.show_message_log(message, lsp.MessageType.Error)
    if os.getenv("LS_SHOW_NOTIFICATION", "off") in ["onError", "onWarning", "always"]:
        LSP_SERVER.show_message(message, lsp.MessageType.Error)


def log_warning(message: str) -> None:
    """Logs messages with notification on warning."""
    LSP_SERVER.show_message_log(message, lsp.MessageType.Warning)
    if os.getenv("LS_SHOW_NOTIFICATION", "off") in ["onWarning", "always"]:
        LSP_SERVER.show_message(message, lsp.MessageType.Warning)


def log_always(message: str) -> None:
    """Logs messages with notification."""
    LSP_SERVER.show_message_log(message, lsp.MessageType.Info)
    if os.getenv("LS_SHOW_NOTIFICATION", "off") in ["always"]:
        LSP_SERVER.show_message(message, lsp.MessageType.Info)


# *****************************************************
# Start the server.
# *****************************************************
if __name__ == "__main__":
    args = lsp_io.parse_args()
    if args.pipe:
        with lsp_io.use_pipe(args.pipe) as (rpipe, wpipe):
            LSP_SERVER.start_io(rpipe, wpipe)
    else:
        # default is always the stdio option.
        LSP_SERVER.start_io()
