type DependencyStatus = "preinstalled" | "installed" | "missing" | "broken";

export type DependencyItem = {
  packageName: string;
  version?: string;
  status: DependencyStatus;
  purpose?: string;
  error?: string;
  dependsOn?: string[];
  children?: DependencyItem[];
};

type KnowledgeRequirements = {
  required: string[];
  optional: string[];
  preinstalled: string[];
};

type Environment = {
  torchInstalled?: boolean;
  torchVersion?: string;
  torchError?: string;
  torchvisionInstalled?: boolean;
  torchvisionVersion?: string;
  torchvisionBroken?: boolean;
  torchvisionError?: string;
  torchaudioInstalled?: boolean;
  torchaudioVersion?: string;
  torchaudioBroken?: boolean;
  torchaudioError?: string;
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
  funasrBroken?: boolean;
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
  const preinstalled = new Set(requirements.preinstalled || []);
  const chromadbStatus: DependencyStatus = resolveStatus(
    environment?.chromadbInstalled,
    environment?.chromadbBroken,
    preinstalled.has("chromadb")
  );
  const chromadb: DependencyItem = {
    packageName: "chromadb",
    version: environment?.chromadbVersion,
    status: chromadbStatus,
    purpose: "向量数据库",
    error: environment?.chromadbError,
    children: []
  };

  if (requirements.required.includes("sentence-transformers")) {
    const stStatus: DependencyStatus = resolveStatus(
      environment?.sentenceTransformersInstalled,
      environment?.sentenceTransformersBroken,
      preinstalled.has("sentence-transformers")
    );
    const sentenceTransformers: DependencyItem = {
      packageName: "sentence-transformers",
      version: environment?.sentenceTransformersVersion,
      status: stStatus,
      purpose: "向量模型",
      error: environment?.sentenceTransformersError,
      dependsOn: ["chromadb"],
      children: []
    };

    if (requirements.required.includes("modelscope")) {
      const msStatus: DependencyStatus = resolveStatus(
        environment?.modelscopeInstalled,
        environment?.modelscopeBroken,
        preinstalled.has("modelscope")
      );
      sentenceTransformers.children!.push({
        packageName: "modelscope",
        version: environment?.modelscopeVersion,
        status: msStatus,
        purpose: "ModelScope 向量模型",
        error: environment?.modelscopeError,
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
      Boolean(environment?.funasrBroken || environment?.funasrError),
      false
    );
    return [{
      packageName: "funasr",
      version: environment?.funasrVersion,
      status: funasrStatus,
      purpose: "阿里开源语音识别引擎",
      error: environment?.funasrError,
      children: [
        {
          packageName: "torch",
          version: environment?.torchVersion,
          status: resolveStatus(
            environment?.torchInstalled,
            Boolean(environment?.torchError),
            false
          ),
          purpose: "深度学习运行时",
          error: environment?.torchError,
          dependsOn: ["funasr"]
        },
        {
          packageName: "torchvision",
          version: environment?.torchvisionVersion,
          status: resolveStatus(
            environment?.torchvisionInstalled,
            Boolean(environment?.torchvisionBroken || environment?.torchvisionError),
            false
          ),
          purpose: "Transformers 图像算子依赖",
          error: environment?.torchvisionError,
          dependsOn: ["torch"]
        },
        {
          packageName: "torchaudio",
          version: environment?.torchaudioVersion,
          status: resolveStatus(
            environment?.torchaudioInstalled,
            Boolean(environment?.torchaudioBroken || environment?.torchaudioError),
            false
          ),
          purpose: "音频算子依赖",
          error: environment?.torchaudioError,
          dependsOn: ["torch"]
        }
      ]
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
