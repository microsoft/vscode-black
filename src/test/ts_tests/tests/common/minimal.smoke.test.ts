// Copyright (c) Microsoft Corporation. All rights reserved.
// Licensed under the MIT License.

import * as path from 'path';
import * as vscode from 'vscode';
import * as fsapi from 'fs-extra';
import { EXTENSION_ROOT_DIR } from '../../../../common/constants';
import { assert } from 'chai';

const TEST_PROJECT_DIR = path.join(EXTENSION_ROOT_DIR, 'src', 'test', 'ts_tests', 'test_data', 'project');
const TIMEOUT = 100000;

suite('Smoke Tests', () => {
    let disposables: vscode.Disposable[] = [];
    suiteSetup(async () => {
        const pythonExt = vscode.extensions.getExtension('ms-python.python');
        if (!pythonExt) {
            await vscode.commands.executeCommand('workbench.extensions.installExtension', 'ms-python.python');
        }
    });

    setup(async () => {
        disposables = [];
        await vscode.commands.executeCommand('workbench.action.closeAllEditors');
    });
    teardown(async () => {
        await vscode.commands.executeCommand('workbench.action.closeAllEditors');

        disposables.forEach((d) => d.dispose());
        disposables = [];
    });

    async function ensurePythonExt() {
        const pythonExt = vscode.extensions.getExtension('ms-python.python');
        assert.ok(pythonExt, 'Python Extension not found');
        await pythonExt?.activate();
    }

    async function ensureBlackExt() {
        const extension = vscode.extensions.getExtension('ms-python.black-formatter');
        assert.ok(extension, 'Extension not found');
        await extension?.activate();
    }

    test('Extension loads', async function () {
        this.timeout(TIMEOUT);

        await vscode.workspace.openTextDocument(path.join(TEST_PROJECT_DIR, 'myscript.py'));

        await ensurePythonExt();

        const extension = vscode.extensions.getExtension('ms-python.black-formatter');
        assert.ok(extension, 'Extension not found');

        if (extension) {
            let timeout = TIMEOUT;
            while (!extension.isActive && timeout > 0) {
                await new Promise((resolve) => setTimeout(resolve, 100));
                timeout -= 100;
            }
            assert.ok(extension.isActive, `Extension not activated in ${TIMEOUT / 1000} seconds`);
        }
    });

    test('Black formats a file', async function () {
        this.timeout(TIMEOUT);

        await ensurePythonExt();

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
        await vscode.workspace.saveAll();
        const actualText = editor?.document.getText();
        assert.equal(actualText, formatted);
    });
});
