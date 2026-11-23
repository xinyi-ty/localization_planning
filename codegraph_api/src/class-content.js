import fs from 'node:fs/promises';
import path from 'node:path';

const TYPE_KINDS = ['class', 'struct', 'interface'];
const compare = (a, b) => (a.filePath < b.filePath ? -1 : a.filePath > b.filePath ? 1 : 0)
    || a.startLine - b.startLine || a.startColumn - b.startColumn || a.id.localeCompare(b.id);

function ambiguity(message, candidates) {
    const error = new Error(message);
    error.code = 'AMBIGUOUS_NODE';
    error.candidates = [...candidates].sort(compare);
    throw error;
}

// getCode returns whole lines. Restrict assembled fragments to the indexed declaration,
// so two declarations on one line do not duplicate each other or include unrelated code.
async function fragment(cg, node) {
    const source = await cg.getCode(node.id);
    if (source == null) {
        const error = new Error(`The indexed source is unavailable: ${node.filePath}; sync the repository.`);
        error.code = 'SOURCE_NOT_FOUND';
        throw error;
    }
    const lines = source.split('\n');
    const last = lines.length - 1;
    if (Number.isInteger(node.endColumn)) {
        let end = node.endColumn;
        if (TYPE_KINDS.includes(node.kind) && lines[last][end] === ';') end++;
        lines[last] = lines[last].slice(0, end);
    }
    if (Number.isInteger(node.startColumn)) {
        const prefix = lines[0].slice(0, node.startColumn);
        // The SDK range can begin inside a same-line C++ template wrapper.
        if (node.language !== 'cpp' || !/^\s*(?:template\s*<[\s\S]*>\s*)+$/.test(prefix)) {
            lines[0] = lines[0].slice(node.startColumn);
        }
    }
    return lines.join('\n');
}

function contains(outer, inner) {
    if (outer.filePath !== inner.filePath) return false;
    const startsBefore = outer.startLine < inner.startLine
        || outer.startLine === inner.startLine && outer.startColumn <= inner.startColumn;
    const endsAfter = outer.endLine > inner.endLine
        || outer.endLine === inner.endLine && outer.endColumn >= inner.endColumn;
    return startsBefore && endsAfter;
}

async function assemble(cg, nodes) {
    const unique = [...new Map(nodes.map(n => [n.id, n])).values()].sort((a, b) =>
        Number(TYPE_KINDS.includes(b.kind)) - Number(TYPE_KINDS.includes(a.kind)) || compare(a, b));
    // Members already present in a class body must not be appended again.
    const outermost = unique.filter((n, i) => !unique.some((other, j) =>
        other.id !== n.id && contains(other, n) && (!contains(n, other) || j < i)));
    return (await Promise.all(outermost.map(n => fragment(cg, n)))).join('\n\n');
}

function declarationInfo(source, node) {
    const clean = source.replace(/\/\*[\s\S]*?\*\/|\/\/[^\n]*|@?"(?:""|\\.|[^"\\])*"|'(?:\\.|[^'\\])*'/g, ' ');
    const escaped = node.name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const declaration = new RegExp(`\\b(class|struct|interface)\\s+${escaped}\\b\\s*(?:<([^>{}]*)>)?`).exec(clean.split('{', 1)[0]);
    if (!declaration) return null;
    return {
        partial: /\bpartial\s*$/.test(clean.slice(0, declaration.index)),
        arity: declaration[2] ? declaration[2].split(',').length : 0
    };
}

// Project files, when present, prevent a global name match from joining separate apps.
// Without project files the indexed repository is the search scope, as with SDK lookup.
async function projectScope(projectRoot, filePath, cache) {
    const root = path.resolve(projectRoot);
    let dir = path.dirname(path.resolve(root, filePath));
    while (dir === root || dir.startsWith(root + path.sep)) {
        if (!cache.has(dir)) cache.set(dir, fs.readdir(dir));
        const names = (await cache.get(dir)).filter(name => name.endsWith('.csproj'));
        if (names.length > 1) return { ambiguous: true };
        if (names.length === 1) return { id: path.join(dir, names[0]) };
        if (dir === root) break;
        dir = path.dirname(dir);
    }
    return { id: root };
}

async function csharpContent(cg, projectRoot, node) {
    const snippets = new Map();
    const info = async n => {
        if (!snippets.has(n.id)) snippets.set(n.id, fragment(cg, n));
        return declarationInfo(await snippets.get(n.id), n);
    };
    const selected = await info(node);
    if (!selected?.partial) return null;
    const candidates = cg.getNodesByKind(node.kind).filter(n => n.language === 'csharp'
        && n.qualifiedName === node.qualifiedName);
    const directoryCache = new Map();
    const scope = await projectScope(projectRoot, node.filePath, directoryCache);
    if (scope.ambiguous) ambiguity('Multiple C# projects make partial ownership ambiguous.', candidates);
    const parentSignature = async n => {
        const parents = cg.getAncestors(n.id).filter(p => TYPE_KINDS.includes(p.kind));
        return JSON.stringify(await Promise.all(parents.map(async p => [p.qualifiedName, (await info(p))?.arity])));
    };
    const parent = await parentSignature(node);
    const parts = [node];
    for (const candidate of candidates) {
        if (candidate.id === node.id) continue;
        const candidateInfo = await info(candidate);
        if (!candidateInfo?.partial || candidateInfo.arity !== selected.arity) continue;
        const candidateScope = await projectScope(projectRoot, candidate.filePath, directoryCache);
        if (candidateScope.ambiguous) ambiguity('Multiple C# projects make partial ownership ambiguous.', [node, candidate]);
        if (candidateScope.id !== scope.id || await parentSignature(candidate) !== parent) continue;
        parts.push(candidate);
    }
    return assemble(cg, parts);
}

function methodScope(node) {
    const suffix = `::${node.name}`;
    return node.qualifiedName?.endsWith(suffix) ? node.qualifiedName.slice(0, -suffix.length) : null;
}

async function cppContent(cg, node) {
    const types = TYPE_KINDS.flatMap(kind => cg.getNodesByKind(kind)).filter(n => n.language === 'cpp');
    const methods = cg.getNodesByKind('method').filter(n => n.language === 'cpp');
    const typesByName = new Map();
    for (const type of types) {
        const entries = typesByName.get(type.qualifiedName) || [];
        entries.push(type);
        typesByName.set(type.qualifiedName, entries);
    }
    const visibility = new Map();
    const visibleFiles = filePath => {
        if (visibility.has(filePath)) return visibility.get(filePath);
        const seen = new Set();
        const pending = [filePath];
        while (pending.length) {
            const file = pending.pop();
            if (seen.has(file)) continue;
            seen.add(file);
            const nodes = cg.getNodesInFile(file);
            const edges = cg.getOutgoingEdgesFrom(nodes.map(n => n.id), ['imports']);
            const targets = cg.getNodesByIds(edges.map(e => e.target));
            for (const target of targets.values()) {
                if (typeof target.filePath === 'string' && target.filePath && target.filePath !== file) pending.push(target.filePath);
            }
        }
        visibility.set(filePath, seen);
        return seen;
    };
    const members = [node];
    const scopes = new Set([node.qualifiedName]);
    // Nested declarations are already in the source, but their out-of-line members belong in full class content too.
    for (const type of types) {
        if (cg.getAncestors(type.id).some(parent => parent.id === node.id)) scopes.add(type.qualifiedName);
    }
    for (const method of methods) {
        const scope = methodScope(method);
        if (!scopes.has(scope)) continue;
        const ancestors = cg.getAncestors(method.id);
        const owner = ancestors.find(n => TYPE_KINDS.includes(n.kind));
        if (owner) {
            if (owner.id === node.id || ancestors.some(n => n.id === node.id)) members.push(method);
            continue;
        }
        const candidates = typesByName.get(scope) || [];
        const visible = visibleFiles(method.filePath);
        const local = candidates.filter(n => n.filePath === method.filePath);
        const reachable = candidates.filter(n => visible.has(n.filePath));
        const matches = local.length ? local : reachable.length ? reachable : candidates;
        if (matches.length > 1) ambiguity(`Cannot identify the class for ${method.qualifiedName}.`, matches);
        const match = matches[0];
        if (match && (match.id === node.id || cg.getAncestors(match.id).some(n => n.id === node.id))) members.push(method);
    }
    return assemble(cg, members);
}

/** No persistent association cache: each class query scans the current SDK index. */
export async function getClassContent(cg, projectRoot, node) {
    if (!node.qualifiedName || !TYPE_KINDS.includes(node.kind)) return null;
    if (node.language === 'cpp') return cppContent(cg, node);
    if (node.language === 'csharp') return csharpContent(cg, projectRoot, node);
    return null;
}
