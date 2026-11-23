import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { createRepository } from '../src/index.js';
import { createHttpApi } from '../src/http-api.js';

test('basic queries remain available and removed analysis APIs are not exposed', async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'cg-basic-api-'));
    try {
        const source = 'class Order:\n    def total(self):\n        return 2\n';
        await fs.writeFile(path.join(root, 'order.py'), source);
        const repository = createRepository(root);
        await repository.initialize({ language: 'python' });
        const app = createHttpApi();
        const call = (method, body = {}) => app.handle(new Request(`http://localhost/${method}`, {
            method: 'POST', headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ projectRoot: root, ...body })
        }));
        const key = { name: 'total', filePath: 'order.py', startLine: 2 };
        const content = await call('getContent', { input: key });
        assert.equal(content.status, 200);
        assert.match((await content.json()).source, /def total/);
        const file = await call('getContent', { input: { kind: 'file', filePath: 'order.py' } });
        assert.equal(file.status, 200);
        assert.equal((await file.json()).source, source);
        const location = await call('getLocation', { key });
        assert.equal(location.status, 200);
        assert.equal((await location.json()).container.name, 'Order');
        const spec = await (await app.handle(new Request('http://localhost/doc/json'))).json();
        for (const method of ['getSourceUnit', 'getTestAnalysis']) {
            assert.equal(repository[method], undefined);
            const response = await call(method, { input: key });
            assert.equal(response.status, 404);
            assert.equal((await response.json()).error.code, 'ROUTE_NOT_FOUND');
            assert.equal(spec.paths[`/${method}`], undefined);
        }
        for (const name of ['SourceUnitResult', 'TestGraphFact', 'TestCaseAnalysis', 'TestAnalysisResult']) {
            assert.equal(spec.components.schemas[name], undefined);
        }
        const checkRefs = value => {
            if (!value || typeof value !== 'object') return;
            if (value.$ref) assert.ok(value.$ref.slice(2).split('/').reduce((part, name) => part?.[name], spec), value.$ref);
            Object.values(value).forEach(checkRefs);
        };
        checkRefs(spec);
    } finally {
        await fs.rm(root, { recursive: true, force: true });
    }
});
