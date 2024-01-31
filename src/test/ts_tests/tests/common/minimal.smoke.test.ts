// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.

import * as cp from 'child_process';
import * as path from 'path';
import * as vscode from 'vscode';
import * as fsapi from 'fs-extra';
import { EXTENSION_ROOT_DIR } from '../../../../common/constants';
import { assert } from 'chai';

const TEST_PROJECT_DIR = path.join(EXTENSION_ROOT_DIR, 'src', 'test', 'ts_tests', 'test_data', 'project');
const TIMEOUT = 30000; // 30 seconds

suite('Smoke Tests', function () {
    this.timeout(TIMEOUT);

    let disposables: vscode.Disposable[] = [];

    setup(async () => {
        disposables = [];
        await vscode.commands.executeCommand('workbench.action.closeAllEditors');
    });

    teardown(async () => {
        await vscode.commands.executeCommand('workbench.action.closeAllEditors');

        disposables.forEach((d) => d.dispose());
        disposables = [];
    });

    async function ensurePythonExt(activate?: boolean): Promise<void> {
        const pythonExt = vscode.extensions.getExtension('ms-python.python');
        assert.ok(pythonExt, 'Python Extension not found');
        if (activate) {
            await pythonExt?.activate();
        }
    }

    async function ensureBlackExt(activate?: boolean): Promise<void> {
        const extension = vscode.extensions.getExtension('ms-python.black-formatter');
        assert.ok(extension, 'Black Formatter Extension not found');
        if (activate) {
            await extension?.activate();
        }
    }

    test('Ensure Black Formatter Extension loads', async () => {
        await vscode.workspace.openTextDocument(path.join(TEST_PROJECT_DIR, 'myscript.py'));

        await ensurePythonExt(true);
        await ensureBlackExt(false);

        const extension = vscode.extensions.getExtension('ms-python.black-formatter');
        if (extension) {
            let timeout = TIMEOUT;
            while (!extension.isActive && timeout > 0) {
                await new Promise((resolve) => setTimeout(resolve, 100));
                timeout -= 100;
            }
            assert.ok(extension.isActive, `Extension not activated in ${TIMEOUT / 1000} seconds`);
        }
    });

    test('Ensure Black Formatter formats a file on save', async () => {
        await ensurePythonExt(true);

        const unformatted = await fsapi.readFile(path.join(TEST_PROJECT_DIR, 'myscript.unformatted'), {
            encoding: 'utf8',
        });
        const formatted = await fsapi.readFile(path.join(TEST_PROJECT_DIR, 'myscript.formatted'), { encoding: 'utf8' });
        await fsapi.writeFile(path.join(TEST_PROJECT_DIR, 'myscript.py'), unformatted, { encoding: 'utf8' });

        const doc = await vscode.workspace.openTextDocument(path.join(TEST_PROJECT_DIR, 'myscript.py'));
        await vscode.window.showTextDocument(doc);

        await ensureBlackExt();

        const editor = vscode.window.activeTextEditor;
        assert.ok(editor, 'No active editor');
        assert.ok(editor?.document.uri.fsPath.endsWith('myscript.py'), 'Active editor is not myscript.py');

        console.log('Waiting for Black formatter to load...');
        const formatReady = new Promise<void>((resolve, reject) => {
            const disposable = vscode.workspace.onDidChangeTextDocument((e) => {
                if (e.document.uri.fsPath.includes('Black')) {
                    const text = e.document.getText();
                    console.log(text);
                    if (text.includes('FOUND black==')) {
                        console.log('Waiting for Black formatter to finished loading');
                        disposable.dispose();
                        resolve();
                    }
                    if (text.includes('Python interpreter missing')) {
                        console.log('Waiting for Black formatter failed to load');
                        disposable.dispose();
                        reject();
                    }
                }
            });
        });
        await formatReady;

        console.log('Waiting for Black formatter to format...');
        const formatDone = new Promise<void>((resolve) => {
            const disposable = vscode.workspace.onDidSaveTextDocument((e) => {
                if (e.uri.fsPath.endsWith('myscript.py')) {
                    disposable.dispose();
                    resolve();
                }
            });
        });

        console.log('Triggering save to start format-on-save...');
        await vscode.commands.executeCommand('workbench.action.files.save');
        await formatDone;
        console.log('Formatting done');

        const actualText = editor?.document.getText();
        assert.equal(actualText, formatted);
    });
});
