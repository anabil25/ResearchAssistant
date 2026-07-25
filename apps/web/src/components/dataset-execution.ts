export const MAX_INLINE_DATASET_BYTES = 100_000;
export const DATASET_CLASSIFICATION = "public_or_synthetic";

export type DatasetAssetMode = "sample" | "large" | "upload";
export type DatasetFileKind = "csv" | "json";

export interface DatasetFileLike {
  name: string;
  size: number;
  type: string;
}

export type DatasetFileAssessment =
  | { kind: DatasetFileKind; error: null }
  | { kind: null; error: string };

export function assessDatasetFile(
  file: DatasetFileLike,
): DatasetFileAssessment {
  const extension = file.name.split(".").pop()?.toLowerCase();
  const kind =
    extension === "csv"
      ? "csv"
      : extension === "json"
        ? "json"
        : file.type === "text/csv"
          ? "csv"
          : file.type === "application/json"
            ? "json"
            : null;
  if (!kind) {
    return {
      kind: null,
      error: "Only .csv or .json files are supported here.",
    };
  }
  if (file.size > MAX_INLINE_DATASET_BYTES) {
    return {
      kind: null,
      error:
        "Direct Code Interpreter input is limited to 100 KB in this workspace. Larger assets need the estimate-and-approve path.",
    };
  }
  return { kind, error: null };
}

export function datasetRunDisabled(options: {
  running: boolean;
  planApproved: boolean;
  assetMode: DatasetAssetMode;
  uploadedFile: DatasetFileLike | null;
  fileKind: DatasetFileKind | null;
  csvText: string | null;
}): boolean {
  if (options.running || !options.planApproved) return true;
  if (options.assetMode !== "upload") return false;
  return (
    !options.uploadedFile ||
    options.fileKind !== "csv" ||
    !options.csvText?.trim()
  );
}

export function buildDatasetRunInputs(options: {
  assetMode: DatasetAssetMode;
  uploadedFile: DatasetFileLike | null;
  fileKind: DatasetFileKind | null;
  csvText: string | null;
  planApproved: boolean;
}): Record<string, unknown> {
  const common = {
    compute_adapter_configured: true,
    analysis_approved: options.planApproved,
    data_classification: DATASET_CLASSIFICATION,
  };
  if (options.assetMode === "large") {
    return {
      ...common,
      filename: "clinical-events-archive.parquet",
      estimated_bytes: 1_200_000_000_000,
    };
  }
  if (options.assetMode === "sample") {
    return {
      ...common,
      filename: "pilot-outcomes.csv",
      estimated_bytes: 4_000_000,
    };
  }
  if (
    !options.uploadedFile ||
    options.fileKind !== "csv" ||
    !options.csvText?.trim()
  ) {
    throw new Error("A loaded CSV file is required for Code Interpreter analysis.");
  }
  return {
    ...common,
    filename: options.uploadedFile.name,
    estimated_bytes: options.uploadedFile.size,
    csv_text: options.csvText,
  };
}
