// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.

import { ConfigurationChangeEvent, ConfigurationScope, Uri, WorkspaceConfiguration, WorkspaceFolder } from 'vscode';
import { getInterpreterDetails } from './python';
import { getConfiguration, getWorkspaceFolders } from './vscodeapi';
import { traceError, traceInfo, traceLog, traceWarn } from './logging';
import { EXTENSION_ID } from './constants';
import { TransportKind } from 'vscode-languageclient/node';
import { trace } from 'console';

export interface ISettings {
    cwd: string;
    check: boolean;
    workspace: string;
    args: string[];
    path: string[];
    interpreter: string[];
    importStrategy: string;
    showNotifications: string;
}

export function getServerTransport(namespace: string, uri: Uri): TransportKind {
    const config = getConfiguration(namespace, uri);
    const value = config.get<string>('serverTransport', 'stdio');
    return value === 'pipe' ? TransportKind.pipe : TransportKind.stdio;
}

export function getExtensionSettings(namespace: string, includeInterpreter?: boolean): Promise<ISettings[]> {
    return Promise.all(getWorkspaceFolders().map((w) => getWorkspaceSettings(namespace, w, includeInterpreter)));
}

function resolveVariables(
    value: string[],
    key: string,
    workspace?: WorkspaceFolder,
    interpreter?: string[],
    env?: NodeJS.ProcessEnv,
): string[] {
    for (const v of value) {
        if (typeof v !== 'string') {
            traceError(`Value [${v}] must be "string" for \`black-formatter.${key}\`: ${value}`);
            throw new Error(`Value [${v}] must be "string" for \`black-formatter.${key}\`: ${value}`);
        }
        if (v.startsWith('--') && v.includes(' ')) {
            traceError(
                `Settings should be in the form ["--line-length=88"] or ["--line-length", "88"] but not ["--line-length 88"]`,
            );
        }
    }

    const substitutions = new Map<string, string>();
    const home = process.env.HOME || process.env.USERPROFILE;
    if (home) {
        substitutions.set('${userHome}', home);
    }
    if (workspace) {
        substitutions.set('${workspaceFolder}', workspace.uri.fsPath);
    }
    substitutions.set('${cwd}', process.cwd());
    getWorkspaceFolders().forEach((w) => {
        substitutions.set('${workspaceFolder:' + w.name + '}', w.uri.fsPath);
    });

    env = env || process.env;
    if (env) {
        for (const [key, value] of Object.entries(env)) {
            if (value) {
                substitutions.set('${env:' + key + '}', value);
            }
        }
    }

    const modifiedValue = [];
    for (const v of value) {
        if (interpreter && v === '${interpreter}') {
            modifiedValue.push(...interpreter);
        } else {
            modifiedValue.push(v);
        }
    }

    return modifiedValue.map((s) => {
        for (const [key, value] of substitutions) {
            s = s.replace(key, value);
        }
        return s;
    });
}

function getCwd(config: WorkspaceConfiguration, workspace: WorkspaceFolder): string {
    const cwd = config.get<string>('cwd', workspace.uri.fsPath);
    return resolveVariables([cwd], 'cwd', workspace)[0];
}

export function getInterpreterFromSetting(namespace: string, scope?: ConfigurationScope) {
    const config = getConfiguration(namespace, scope);
    return config.get<string[]>('interpreter');
}

export async function getWorkspaceSettings(
    namespace: string,
    workspace: WorkspaceFolder,
    includeInterpreter?: boolean,
): Promise<ISettings> {
    const config = getConfiguration(namespace, workspace.uri);

    let interpreter: string[] = [];
    if (includeInterpreter) {
        interpreter = getInterpreterFromSetting(namespace, workspace) ?? [];
        if (interpreter.length === 0) {
            traceLog(`No interpreter found from setting ${namespace}.interpreter`);
            traceLog(`Getting interpreter from ms-python.python extension for workspace ${workspace.uri.fsPath}`);
            interpreter = (await getInterpreterDetails(workspace.uri)).path ?? [];
            if (interpreter.length > 0) {
                traceLog(
                    `Interpreter from ms-python.python extension for ${workspace.uri.fsPath}:`,
                    `${interpreter.join(' ')}`,
                );
            }
        } else {
            traceLog(`Interpreter from setting ${namespace}.interpreter: ${interpreter.join(' ')}`);
        }

        if (interpreter.length === 0) {
            traceLog(`No interpreter found for ${workspace.uri.fsPath} in settings or from ms-python.python extension`);
        }
    }

    const workspaceSetting = {
        cwd: getCwd(config, workspace),
        check: config.get<boolean>('check', false),
        workspace: workspace.uri.toString(),
        args: resolveVariables(config.get<string[]>('args', []), 'args', workspace),
        path: resolveVariables(config.get<string[]>('path', []), 'path', workspace, interpreter),
        interpreter: resolveVariables(interpreter, 'interpreter', workspace),
        importStrategy: config.get<string>('importStrategy', 'useBundled'),
        showNotifications: config.get<string>('showNotifications', 'off'),
    };
    traceInfo(
        `Workspace settings for ${workspace.uri.fsPath} (client side): ${JSON.stringify(workspaceSetting, null, 4)}`,
    );
    return workspaceSetting;
}

function getGlobalValue<T>(config: WorkspaceConfiguration, key: string): T | undefined {
    const inspect = config.inspect<T>(key);
    return inspect?.globalValue ?? inspect?.defaultValue;
}

export async function getGlobalSettings(namespace: string, includeInterpreter?: boolean): Promise<ISettings> {
    const config = getConfiguration(namespace);

    let interpreter: string[] = [];
    if (includeInterpreter) {
        interpreter = getGlobalValue<string[]>(config, 'interpreter') ?? [];
        if (interpreter === undefined || interpreter.length === 0) {
            interpreter = (await getInterpreterDetails()).path ?? [];
        }
    }

    const setting = {
        cwd: process.cwd(),
        check: config.get<boolean>('check', false),
        workspace: process.cwd(),
        args: getGlobalValue<string[]>(config, 'args') ?? [],
        path: getGlobalValue<string[]>(config, 'path') ?? [],
        interpreter: interpreter ?? [],
        importStrategy: getGlobalValue<string>(config, 'importStrategy') ?? 'useBundled',
        showNotifications: getGlobalValue<string>(config, 'showNotifications') ?? 'off',
    };
    traceInfo(`Global settings (client side): ${JSON.stringify(setting, null, 4)}`);
    return setting;
}

export function checkIfConfigurationChanged(e: ConfigurationChangeEvent, namespace: string): boolean {
    const settings = [
        `${namespace}.args`,
        `${namespace}.path`,
        `${namespace}.interpreter`,
        `${namespace}.importStrategy`,
        `${namespace}.showNotifications`,
        `${namespace}.serverTransport`,
    ];
    const changed = settings.map((s) => e.affectsConfiguration(s));
    return changed.includes(true);
}

export function logDefaultFormatter(): void {
    getWorkspaceFolders().forEach((workspace) => {
        let config = getConfiguration('editor', { uri: workspace.uri, languageId: 'python' });
        if (!config) {
            config = getConfiguration('editor', workspace.uri);
            if (!config) {
                traceInfo('Unable to get editor configuration');
            }
        }
        const formatter = config.get<string>('defaultFormatter', '');
        traceInfo(`Default formatter is set to ${formatter} for workspace ${workspace.uri.fsPath}`);
        if (formatter !== EXTENSION_ID) {
            traceWarn(`Black Formatter is NOT set as the default formatter for workspace ${workspace.uri.fsPath}`);
            traceWarn('To set Black Formatter as the default formatter, add the following to your settings.json file:');
            traceWarn(`\n"[python]": {\n    "editor.defaultFormatter": "${EXTENSION_ID}"\n}`);
        }
    });
}

export function logLegacySettings(): void {
    getWorkspaceFolders().forEach((workspace) => {
        try {
            const legacyConfig = getConfiguration('python', workspace.uri);
            const legacyArgs = legacyConfig.get<string[]>('formatting.blackArgs', []);
            const legacyPath = legacyConfig.get<string>('formatting.blackPath', '');
            if (legacyArgs.length > 0) {
                traceWarn(`"python.formatting.blackArgs" is deprecated. Use "black-formatter.args" instead.`);
                traceWarn(`"python.formatting.blackArgs" for workspace ${workspace.uri.fsPath}:`);
                traceWarn(`\n${JSON.stringify(legacyArgs, null, 4)}`);
            }

            if (legacyPath.length > 0 && legacyPath !== 'black') {
                traceWarn(`"python.formatting.blackPath" is deprecated. Use "black-formatter.path" instead.`);
                traceWarn(`"python.formatting.blackPath" for workspace ${workspace.uri.fsPath}:`);
                traceWarn(`\n${JSON.stringify(legacyPath, null, 4)}`);
            }
        } catch (err) {
            traceWarn(`Error while logging legacy settings: ${err}`);
        }
    });
}
