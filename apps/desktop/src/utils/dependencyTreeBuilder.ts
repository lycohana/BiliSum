type DependencyStatus = "preinstalled" | "installed" | "missing" | "broken";

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
  chromadbBroken?: boolean;
  chromadbError?: string;
  sentenceTransformersInstalled?: boolean;
  sentenceTransformersVersion?: string;
  sentenceTransformersBroken?: boolean;
  sentenceTransformersError?: string;
  modelscopeInstalled?: boolean;
  modelscopeVersion?: string;
  modelscopeBroken?: boolean;
  modelscopeError?: string;
  funasrInstalled?: boolean;
  funasrVersion?: string;
  funasrError?: string;
  localAsrInstalled?: boolean;
  localAsrVersion?: string;
};

function resolveStatus(
  installed: boolean | undefined,
  broken: boolean | undefined,
  preinstalled: boolean
): DependencyStatus {
  if (broken) return "broken";
  if (installed) return "installed";
  if (preinstalled) return "preinstalled";
  return "missing";
}

export function buildKnowledgeDependencyTree(
  requirements: KnowledgeRequirements,
  environment: Environment | null
): DependencyItem[] {
  const chromadbStatus: DependencyStatus = environment?.chromadbBroken
    ? "broken"
    : environment?.chromadbInstalled
      ? "installed"
      : "preinstalled";
  const chromadb: DependencyItem = {
    packageName: "chromadb",
    version: environment?.chromadbVersion,
    status: chromadbStatus,
    purpose: "向量数据库",
    children: []
  };

  if (requirements.required.includes("sentence-transformers")) {
    const stStatus: DependencyStatus = resolveStatus(
      environment?.sentenceTransformersInstalled,
      environment?.sentenceTransformersBroken,
      false
    );
    const sentenceTransformers: DependencyItem = {
      packageName: "sentence-transformers",
      version: environment?.sentenceTransformersVersion,
      status: stStatus,
      purpose: "向量模型",
      dependsOn: ["chromadb"],
      children: []
    };

    if (requirements.required.includes("modelscope")) {
      const msStatus: DependencyStatus = resolveStatus(
        environment?.modelscopeInstalled,
        environment?.modelscopeBroken,
        false
      );
      sentenceTransformers.children!.push({
        packageName: "modelscope",
        version: environment?.modelscopeVersion,
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
    const funasrStatus: DependencyStatus = resolveStatus(
      environment?.funasrInstalled,
      false,
      false
    );
    return [{
      packageName: "funasr",
      version: environment?.funasrVersion,
      status: funasrStatus,
      purpose: "阿里开源语音识别引擎",
      children: []
    }];
  }

  if (asrType === "local") {
    const localAsrStatus: DependencyStatus = resolveStatus(
      environment?.localAsrInstalled,
      false,
      false
    );
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
