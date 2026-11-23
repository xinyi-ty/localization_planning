import fs from 'node:fs/promises';
import path from 'node:path';
import { getClassContent } from './class-content.js';

function apiError(code, message) {
    const error = new Error(message);
    error.code = code;
    return error;
}

function normalizeFilePath(filePath) {
    if (typeof filePath !== 'string' || filePath.length === 0) {
        throw apiError('INVALID_SELECTOR', 'filePath must be a non-empty project-relative path.');
    }
    const normalized = path.posix.normalize(filePath.replace(/\\/g, '/'));
    if (path.posix.isAbsolute(normalized) || normalized === '..' || normalized.startsWith('../')) {
        throw apiError('INVALID_SELECTOR', 'filePath must stay inside the project root.');
    }
    return normalized;
}

function isFunction(node) {
    return node && (node.kind === 'function' || node.kind === 'method');
}

function toKey(node) {
    return { name: node.name, filePath: node.filePath, startLine: node.startLine };
}

export function getDependencies(graph, entry, level) {
    return graph.getDependencies(entry, level);
}

function compareNodes(left, right) {
    return left.filePath.localeCompare(right.filePath)
        || left.startLine - right.startLine
        || left.id.localeCompare(right.id);
}

export function getLocation(cg, node) {
    if (!isFunction(node)) {
        throw apiError('INVALID_SELECTOR', 'Location queries accept functions or methods.');
    }
    const container = cg.getAncestors(node.id).find((ancestor) =>
        ancestor.kind === 'class' || ancestor.kind === 'struct' || ancestor.kind === 'interface'
    ) || null;
    return { key: toKey(node), filePath: node.filePath, container: container ? toKey(container) : null };
}

export function getAllFunctions(cg) {
    return cg.getNodesByKind('function').concat(cg.getNodesByKind('method')).sort(compareNodes).map(toKey);
}

export function getCallees(graph, node) {
    return graph.getCallees(node);
}

export async function getContent(cg, projectRoot, input) {
    if (input && input.kind === 'file' && input.filePath) {
        const filePath = normalizeFilePath(input.filePath);
        const root = await fs.realpath(projectRoot);
        const absolutePath = await fs.realpath(path.join(root, filePath));
        const relativePath = path.relative(root, absolutePath);
        if (relativePath.startsWith('..' + path.sep) || path.isAbsolute(relativePath)) {
            throw apiError('INVALID_SELECTOR', 'The file resolves outside the project root.');
        }
        const fileNodes = cg.getNodesInFile(filePath);
        const fileNode = fileNodes.find(function (node) { return node.kind === 'file'; }) || null;
        return { node: fileNode, filePath: filePath, source: await fs.readFile(absolutePath, 'utf8') };
    }
    const node = input;
    if (!isFunction(node) && node.kind !== 'class' && node.kind !== 'struct' && node.kind !== 'interface') {
        throw apiError('INVALID_SELECTOR', 'Content queries accept functions, methods, classes, structs, interfaces, or files.');
    }
    const merged = await getClassContent(cg, projectRoot, node);
    const source = merged ?? await cg.getCode(node.id);
    if (source === null || source === undefined) {
        throw apiError('SOURCE_NOT_FOUND', 'The indexed symbol source is unavailable; sync the repository.');
    }
    return { node: node, filePath: node.filePath, source: source };
}
