import type { Node, Edge, IndexResult, SyncResult } from '@colbymchenry/codegraph';

/** Read a whole file by its project-relative path. */
export interface FileSelector {
    kind: 'file';
    filePath: string;
}

/** Exact symbol lookup; paths are project-relative and lines are 1-indexed. */
export interface NodeKey {
    name: string;
    filePath: string;
    startLine: number;
}

export interface InitializeOptions {
    /** Omit or pass null for all SDK-supported languages. */
    language?: string | string[] | null;
}

export interface CallResult {
    entry: NodeKey;
    /** Direct callees only (functions/methods), excluding entry; each appears once. */
    functions: NodeKey[];
    /** Direct calls edges, including self-recursive calls. */
    edges: Edge[];
}

export interface ContentResult {
    node: Node | null;
    filePath: string;
    /** C++ types and C# partial types join indexed declarations/implementations; node/filePath still identify the input. */
    source: string;
}

export interface LocationResult {
    key: NodeKey;
    filePath: string;
    /** Nearest enclosing class, struct, or interface recorded by the SDK; null if none is found. */
    container: NodeKey | null;
}

export interface DependencyResult<TEntry, TDependency> {
    entry: TEntry;
    dependencies: TDependency[];
}

export declare class CodeGraphRepository {
    constructor(projectRoot: string);
    /** Create the index; omit options.language to index all supported languages. */
    initialize(options?: InitializeOptions): Promise<IndexResult | SyncResult>;
    /** Uses the SDK's native sync, preserving the language selection. */
    sync(): Promise<SyncResult>;
    /** Read direct calls from the cached dependency graph; key identifies one function or method. */
    getCallees(key: NodeKey): Promise<CallResult>;
    /** Read a symbol or file; C++ type implementations and matching C# partial parts are assembled on each call. */
    getContent(input: NodeKey | FileSelector): Promise<ContentResult>;
    /** Returns keys for both functions and methods. */
    getAllFunctions(): Promise<NodeKey[]>;
    /** Find the file and nearest enclosing type of exactly one function or method. */
    getLocation(key: NodeKey): Promise<LocationResult>;
    /** Read precomputed class/file dependencies; class ownership uses SDK containment only. */
    getDependencies(level: 'class', input: NodeKey): Promise<DependencyResult<NodeKey, NodeKey>>;
    getDependencies(level: 'file', input: FileSelector): Promise<DependencyResult<FileSelector, FileSelector>>;
}

export function createRepository(projectRoot: string): CodeGraphRepository;
