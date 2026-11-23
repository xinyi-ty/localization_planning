import fs from 'node:fs/promises';
import path from 'node:path';
import { DependencyGraph } from './dependency-graph.js';

/** Persistent dependency snapshot; associations are exactly those in the SDK graph. */
export class DependencyCache {
    #graph = null;
    #loading = null;

    constructor(projectRoot) {
        this.filePath = path.join(projectRoot, '.codegraph/dependencies-cache.json');
    }

    clearMemory() { this.#graph = null; }

    async invalidate() {
        this.clearMemory();
        await fs.rm(this.filePath, { force: true });
    }

    async refresh(cg) {
        const temporaryPath = this.filePath + '.tmp';
        try {
            const graph = DependencyGraph.build(cg);
            await fs.writeFile(temporaryPath, JSON.stringify(graph) + '\n');
            await fs.rename(temporaryPath, this.filePath);
            this.#graph = graph;
            return graph;
        } catch (error) {
            await this.invalidate();
            throw error;
        } finally {
            await fs.rm(temporaryPath, { force: true });
        }
    }

    async #load(cg) {
        try {
            this.#graph = new DependencyGraph(JSON.parse(await fs.readFile(this.filePath, 'utf8')));
            return this.#graph;
        } catch (error) {
            // Cache format/JSON errors and absence rebuild; actual IO permission failures propagate.
            if (error.code && error.code !== 'ENOENT') throw error;
            return this.refresh(cg);
        }
    }

    async getGraph(cg) {
        if (this.#graph) return this.#graph;
        if (!this.#loading) this.#loading = this.#load(cg).finally(() => { this.#loading = null; });
        return this.#loading;
    }
}
