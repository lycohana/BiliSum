type DependencyStatus = "preinstalled" | "installed" | "missing";

export type DependencyItem = {
  packageName: string;
  version?: string;
  status: DependencyStatus;
  purpose?: string;
  dependsOn?: string[];
  children?: DependencyItem[];
};

type KnowledgeRequirements = {
  required: string[];
  optional: string[];
  preinstalled: string[];
};

type Environment = {
  chromadbInstalled?: boolean;
  chromadbVersion?: string;
  sentenceTransformersInstalled?: boolean;
  sentenceTransformersVersion?: string;
  modelscopeInstalled?: boolean;
  funasrInstalled?: boolean;
  funasrVersion?: string;
  localAsrInstalled?: boolean;
  localAsrVersion?: string;
};

export function buildKnowledgeDependencyTree(
  requirements: KnowledgeRequirements,
  environment: Environment | null
): DependencyItem[] {
  const chromadbStatus: DependencyStatus = "preinstalled";
  const chromadb: DependencyItem = {
    packageName: "chromadb",
    version: environment?.chromadbVersion,
    status: chromadbStatus,
    purpose: "向量数据库",
    children: []
  };

  if (requirements.required.includes("sentence-transformers")) {
    const stStatus: DependencyStatus = environment?.sentenceTransformersInstalled ? "installed" : "missing";
    const sentenceTransformers: DependencyItem = {
      packageName: "sentence-transformers",
      version: environment?.sentenceTransformersVersion,
      status: stStatus,
      purpose: "向量模型",
      dependsOn: ["chromadb"],
      children: []
    };

    if (requirements.required.includes("modelscope")) {
      const msStatus: DependencyStatus = environment?.modelscopeInstalled ? "installed" : "missing";
      sentenceTransformers.children!.push({
        packageName: "modelscope",
        status: msStatus,
        purpose: "ModelScope 向量模型",
        dependsOn: ["sentence-transformers"]
      });
    }

    chromadb.children!.push(sentenceTransformers);
  }

  return [chromadb];
}

export function buildAsrDependencyTree(
  environment: Environment | null,
  asrType: "funasr" | "local"
): DependencyItem[] {
  if (asrType === "funasr") {
    const funasrStatus: DependencyStatus = environment?.funasrInstalled ? "installed" : "missing";
    return [{
      packageName: "funasr",
      version: environment?.funasrVersion,
      status: funasrStatus,
      purpose: "阿里开源语音识别引擎",
      children: []
    }];
  }

  if (asrType === "local") {
    const localAsrStatus: DependencyStatus = environment?.localAsrInstalled ? "installed" : "missing";
    return [{
      packageName: "faster-whisper",
      version: environment?.localAsrVersion,
      status: localAsrStatus,
      purpose: "本地语音识别（Whisper）",
      children: []
    }];
  }

  return [];
}
