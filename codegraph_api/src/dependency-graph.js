import codegraph from '@colbymchenry/codegraph';

const TYPES = new Set(['class', 'struct', 'interface']);
const FUNCTIONS = new Set(['function', 'method']);
const CLASS_RELATIONS = new Set(['calls', 'extends', 'implements', 'instantiates']);
const RELATIONS = new Set([...CLASS_RELATIONS, 'imports', 'contains']);
const toKey = n => ({ name: n.name, filePath: n.filePath, startLine: n.startLine });
const compare = (a, b) => a.filePath.localeCompare(b.filePath) || a.startLine - b.startLine || a.id.localeCompare(b.id);
const invalid = () => { throw new Error('Invalid dependency graph cache.'); };
const selectorError = message => {
    const error = new Error(message);
    error.code = 'INVALID_SELECTOR';
    return error;
};

function append(map, key, value) {
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(value);
}

function aggregate(nodes, edges) {
    const byId = new Map(nodes.map(n => [n.id, n]));
    const parents = new Map();
    for (const edge of edges) {
        // SDK getAncestors follows the first incoming contains edge, ordered by source ID.
        if (edge.kind === 'contains' && (!parents.has(edge.target) || edge.source < parents.get(edge.target))) {
            parents.set(edge.target, edge.source);
        }
    }
    const owners = new Map();
    const owner = id => {
        if (owners.has(id)) return owners.get(id);
        const seen = new Set();
        let current = id;
        while (current && !seen.has(current)) {
            seen.add(current);
            if (TYPES.has(byId.get(current)?.kind)) {
                owners.set(id, current);
                return current;
            }
            current = parents.get(current);
        }
        owners.set(id, null);
        return null;
    };
    const classPairs = new Map();
    const filePairs = new Map();
    const record = (map, source, target, index) => {
        const identity = JSON.stringify([source, target]);
        if (!map.has(identity)) map.set(identity, { source, target, edgeIndices: [] });
        map.get(identity).edgeIndices.push(index);
    };
    edges.forEach((edge, index) => {
        if (edge.kind === 'contains') return;
        const source = byId.get(edge.source);
        const target = byId.get(edge.target);
        if (!source || !target) return;
        if (source.filePath && target.filePath && source.filePath !== target.filePath) {
            record(filePairs, source.filePath, target.filePath, index);
        }
        if (CLASS_RELATIONS.has(edge.kind)) {
            const sourceType = owner(source.id);
            const targetType = owner(target.id);
            if (sourceType && targetType && sourceType !== targetType) record(classPairs, sourceType, targetType, index);
        }
    });
    return { classDependencies: [...classPairs.values()], fileDependencies: [...filePairs.values()] };
}

/** A read-only snapshot of SDK relationships and their precomputed class/file projections. */
export class DependencyGraph {
    #snapshot;
    #nodes;
    #calls = new Map();
    #classes = new Map();
    #files = new Map();

    static build(cg) {
        // Include all SDK node kinds: import/field/parameter nodes can carry dependencies too.
        const nodes = [...new Map(codegraph.NODE_KINDS.flatMap(kind => cg.getNodesByKind(kind))
            .map(n => [n.id, { id: n.id, kind: n.kind, name: n.name, filePath: n.filePath, startLine: n.startLine }])).values()];
        const edges = cg.getOutgoingEdgesFrom(nodes.map(n => n.id), [...RELATIONS]);
        return new DependencyGraph({ version: 1, nodes, edges, ...aggregate(nodes, edges) });
    }

    constructor(snapshot) {
        if (!snapshot || snapshot.version !== 1 || !Array.isArray(snapshot.nodes) || !Array.isArray(snapshot.edges)
            || !Array.isArray(snapshot.classDependencies) || !Array.isArray(snapshot.fileDependencies)) invalid();
        this.#nodes = new Map();
        for (const node of snapshot.nodes) {
            if (!node || typeof node.id !== 'string' || !node.id || this.#nodes.has(node.id)
                || !codegraph.NODE_KINDS.includes(node.kind) || typeof node.name !== 'string'
                || typeof node.filePath !== 'string' || !Number.isInteger(node.startLine)) invalid();
            this.#nodes.set(node.id, node);
        }
        snapshot.edges.forEach((edge, index) => {
            if (!edge || typeof edge.source !== 'string' || typeof edge.target !== 'string' || !RELATIONS.has(edge.kind)) invalid();
            if (edge.kind === 'calls') append(this.#calls, edge.source, index);
        });
        const validatePairs = (pairs, map, classLevel) => {
            const seen = new Set();
            for (const pair of pairs) {
                if (!pair || typeof pair.source !== 'string' || typeof pair.target !== 'string' || pair.source === pair.target
                    || !Array.isArray(pair.edgeIndices) || !pair.edgeIndices.length) invalid();
                const identity = JSON.stringify([pair.source, pair.target]);
                if (seen.has(identity)) invalid();
                seen.add(identity);
                if (classLevel && (!TYPES.has(this.#nodes.get(pair.source)?.kind) || !TYPES.has(this.#nodes.get(pair.target)?.kind))) invalid();
                for (const index of pair.edgeIndices) {
                    const edge = snapshot.edges[index];
                    if (!Number.isInteger(index) || !edge || edge.kind === 'contains'
                        || classLevel && !CLASS_RELATIONS.has(edge.kind)) invalid();
                    if (!classLevel && (this.#nodes.get(edge.source)?.filePath !== pair.source
                        || this.#nodes.get(edge.target)?.filePath !== pair.target)) invalid();
                }
                append(map, pair.source, pair.target);
            }
        };
        validatePairs(snapshot.classDependencies, this.#classes, true);
        validatePairs(snapshot.fileDependencies, this.#files, false);
        this.#snapshot = snapshot;
    }

    toJSON() { return this.#snapshot; }

    getCallees(node) {
        if (!FUNCTIONS.has(node.kind)) throw selectorError('The call entry must be a function or method.');
        const functions = new Map();
        const edges = (this.#calls.get(node.id) || []).map(index => this.#snapshot.edges[index]);
        for (const edge of edges) {
            const target = this.#nodes.get(edge.target);
            if (target && target.id !== node.id && FUNCTIONS.has(target.kind)) functions.set(target.id, target);
        }
        return {
            entry: toKey(node),
            functions: [...functions.values()].sort(compare).map(toKey),
            // Do not let a caller mutate the in-memory graph through returned SDK edge objects.
            edges: structuredClone(edges)
        };
    }

    getDependencies(entry, level) {
        if (!['class', 'file'].includes(level)) throw selectorError('level must be class or file.');
        if (level === 'class') {
            if (!TYPES.has(entry?.kind)) throw selectorError('Class dependency queries require a class, struct, or interface key.');
            return { entry: toKey(entry), dependencies: (this.#classes.get(entry.id) || [])
                .map(id => this.#nodes.get(id)).sort(compare).map(toKey) };
        }
        if (entry?.kind !== 'file') throw selectorError('File dependency queries require a file selector.');
        return {
            entry: { kind: 'file', filePath: entry.filePath },
            dependencies: [...(this.#files.get(entry.filePath) || [])].sort()
                .map(filePath => ({ kind: 'file', filePath }))
        };
    }
}
